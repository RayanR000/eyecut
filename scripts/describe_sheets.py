#!/usr/bin/env python3
"""Stage-1 vision pass: read the contact sheets `scan_experiment.py` produced.

One request per sheet. Each sheet is a tile grid, one row per shot, three frames
left-to-right across the row -- so the model sees motion within a shot, and the
prompt tells it which shot index each row belongs to. Descriptions are merged
back into shots.json in place.

    python3 scripts/describe_sheets.py scratch/tos

Resumable: shots that already carry a `description` are skipped, and a sheet is
only sent if at least one of its shots is missing one. Re-run after a failure and
it picks up where it stopped; pass --force to redo everything.
"""
import argparse, base64, json, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

MODEL = "claude-opus-5"

SHOT_FIELDS = {
    "index": {"type": "integer", "description": "the shot index labelled for this row"},
    "description": {"type": "string", "description": "one sentence: what happens in this shot"},
    "subject": {"type": "string", "description": "the main subject, e.g. 'woman on horseback', 'empty corridor', 'title card'"},
    "composition": {"type": "string", "description": "shot size and camera, e.g. 'wide static', 'medium tracking left', 'close-up push-in'"},
    "lighting": {"type": "string", "description": "light and palette, e.g. 'cold overcast blue', 'warm firelight, crushed blacks'"},
    "readable_at_speed": {"type": "boolean", "description": "true if the shot still reads when cut to well under a second -- a clear single subject, high contrast, little fine detail"},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": SHOT_FIELDS,
                "required": list(SHOT_FIELDS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}

SYSTEM = """You are describing shots from a film so a video editor can pick clips \
from your descriptions alone, without rewatching the footage. Write for retrieval: \
concrete nouns, visible facts, no plot inference and no praise. If a row is a title \
card, credits, black, or a transition artifact, say so plainly -- those are as useful \
to identify as good shots are. Describe every row you are given, in order."""


def sheet_prompt(indices, sheet_name):
    rows = "\n".join(f"  row {i + 1} -> shot {idx}" for i, idx in enumerate(indices))
    return (
        f"Contact sheet {sheet_name}: {len(indices)} rows, one shot per row, three frames "
        f"per row sampled left-to-right at 20%, 50% and 80% through the shot.\n\n"
        f"Row order, top to bottom:\n{rows}\n\n"
        f"Describe each shot. Use the three frames of a row together -- they tell you "
        f"whether the camera or the subject moves. Return exactly {len(indices)} entries, "
        f"with the shot index from the list above."
    )


def describe_sheet(client, sheet_path: Path, indices):
    data = base64.standard_b64encode(sheet_path.read_bytes()).decode()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": sheet_prompt(indices, sheet_path.name)},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"{sheet_path.name}: refused ({response.stop_details})")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["shots"], response.usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scandir", help="directory holding shots.json and sheets/ (e.g. scratch/tos)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, help="only send the first N sheets")
    ap.add_argument("--force", action="store_true", help="re-describe shots that already have descriptions")
    args = ap.parse_args()

    scandir = Path(args.scandir)
    shots_json = scandir / "shots.json"
    doc = json.loads(shots_json.read_text())
    by_index = {rec["index"]: rec for rec in doc["shots"]}

    todo = []
    for sheet in doc.get("sheets", []):
        if "error" in sheet:
            continue
        indices = [i for i in sheet["shots"] if i in by_index]
        if not args.force:
            indices = [i for i in indices if not by_index[i].get("description")]
        if indices:
            todo.append((scandir / "sheets" / sheet["sheet"], sheet["shots"], indices))
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("nothing to describe -- every shot already has a description")
        return

    client = anthropic.Anthropic()
    lock = threading.Lock()
    totals = {"in": 0, "out": 0, "described": 0, "missing": 0}

    def work(item):
        sheet_path, all_indices, _ = item
        # Always send the whole sheet: the image is the whole grid, and row->shot
        # alignment only holds if the model sees every row.
        entries, usage = describe_sheet(client, sheet_path, all_indices)
        with lock:
            seen = set()
            for e in entries:
                rec = by_index.get(e["index"])
                if rec is None or e["index"] not in all_indices:
                    continue
                seen.add(e["index"])
                rec.update({k: e[k] for k in SHOT_FIELDS if k != "index"})
            missing = [i for i in all_indices if i not in seen]
            totals["in"] += usage.input_tokens
            totals["out"] += usage.output_tokens
            totals["described"] += len(seen)
            totals["missing"] += len(missing)
            shots_json.write_text(json.dumps(doc, indent=2) + "\n")
            note = f"  MISSING {missing}" if missing else ""
            print(f"{sheet_path.name}: {len(seen)}/{len(all_indices)} shots{note}", flush=True)

    failures = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for item, fut in [(i, pool.submit(work, i)) for i in todo]:
            try:
                fut.result()
            except Exception as exc:
                failures.append(f"{item[0].name}: {exc}")

    cost = totals["in"] / 1e6 * 5 + totals["out"] / 1e6 * 25
    print(f"\n{totals['described']} shots described, {totals['missing']} rows unanswered")
    print(f"{totals['in']} in / {totals['out']} out tokens -- about ${cost:.2f}")
    print(f"written to {shots_json}")
    for f in failures:
        print(f"FAILED {f}", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
