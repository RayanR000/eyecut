"""Register media in `draft_meta_info.json` -> `draft_materials`.

This is the piece no other CapCut generator has. Without these entries CapCut 9.x
prompts to relink every clip, which makes a generated draft worse than useless:
`capcut-cli` 0.21.1 puts the write deliberately out of scope for want of a captured
entry shape, and VectCutAPI ships a template with every group empty.

The entry shape is copied from CapCut-authored drafts -- see
`tests/fixtures/draft_materials_entries.json`. Quirks are verbatim and deliberate:
`file_Path` has a capital P, `metetype` is misspelled, durations are integer
microseconds, and every real entry sits in the `type: 0` group whatever its
metetype. Do not tidy any of that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# CapCut's own groups. Only 0 is ever populated, but the others must exist.
GROUP_TYPES = (0, 1, 2, 3, 6, 7)
PHOTO_DURATION_US = 5_000_000          # what CapCut gives a still on import
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class MediaProbe:
    # A draft-relative path stays a str: Path("./assets/x") normalizes to
    # "assets/x", and CapCut's own drafts write the "./" [see is_draft_relative].
    path: Path | str
    metetype: str          # "video" | "music" | "photo"
    width: int
    height: int
    duration_us: int


@dataclass
class Registration:
    meta_path: Path
    added: list[str] = field(default_factory=list)
    already_registered: list[str] = field(default_factory=list)
    backup: Path | None = None


def capcut_is_running() -> bool:
    """CapCut caches draft_meta_info.json in memory and overwrites it on quit, so
    anything written while it is open is silently lost.

    Match the process *name* only. `pgrep -f` matches full command lines, which
    made every ffmpeg call on a file under ~/Movies/CapCut/ look like the running
    app -- a false positive that blocks legitimate writes.
    """
    for name in ("CapCut", "CapCut.exe", "CapCutWeb"):
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        if out.returncode == 0 and out.stdout.strip():
            return True
    return False


def is_draft_relative(path: Path | str) -> bool:
    """CapCut registers media it holds as `./assets/...`, relative to the draft.

    This is how CapCut's own drafts do it, and matching it is what stops the
    relink dialog: registering the same file by its original absolute path
    leaves the media panel reporting "Media lost" even though the file is there,
    because the timeline refers to the copy inside the draft, not the original
    [proven, probes D vs E].
    """
    return str(path).startswith("./")


def probe(path: Path) -> MediaProbe:
    """ffprobe -> MediaProbe. Raises if the file is unreadable."""
    path = Path(path)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    is_image = path.suffix.lower() in IMAGE_SUFFIXES
    if is_image:
        metetype, duration_us = "photo", PHOTO_DURATION_US
    elif video is not None:
        metetype = "video"
        duration_us = int(round(float(data["format"]["duration"]) * 1_000_000))
    elif audio is not None:
        metetype = "music"
        duration_us = int(round(float(data["format"]["duration"]) * 1_000_000))
    else:
        raise RuntimeError(f"no video or audio stream in {path}")

    return MediaProbe(
        path=path,
        metetype=metetype,
        width=int(video["width"]) if video else 0,
        height=int(video["height"]) if video else 0,
        duration_us=duration_us,
    )


def entry_for(p: MediaProbe) -> dict:
    """One `draft_materials` entry, field for field as CapCut writes it."""
    now = int(time.time())
    # A still has a duration but no roughcut range; time-based media has both.
    roughcut = ({"duration": -1, "start": -1} if p.metetype == "photo"
                else {"duration": p.duration_us, "start": 0})
    return {
        "ai_group_type": "",
        "create_time": now,
        "duration": int(p.duration_us),
        "enter_from": 0,
        "extra_info": Path(str(p.path)).name,
        "file_Path": str(p.path),
        "height": int(p.height),
        "id": str(uuid.uuid4()),
        "import_time": now,
        "import_time_ms": int(time.time() * 1_000_000),
        "item_source": 1,
        "material_color_tag": "",
        "md5": "",
        "metetype": p.metetype,
        "roughcut_time_range": roughcut,
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0,
        "width": int(p.width),
    }


def groups_of(meta: dict) -> list[dict]:
    """Return draft_materials with every CapCut group present, order preserved."""
    existing = meta.get("draft_materials") or []
    by_type = {g["type"]: g for g in existing if isinstance(g, dict) and "type" in g}
    for t in GROUP_TYPES:
        by_type.setdefault(t, {"type": t, "value": []})
    ordered = [by_type[t] for t in GROUP_TYPES]
    # Keep any group CapCut invents that we don't know about.
    ordered += [g for g in existing if g.get("type") not in GROUP_TYPES]
    meta["draft_materials"] = ordered
    return ordered


def write_meta(meta_path: Path, meta: dict) -> Path:
    """Back up, then replace `meta_path` atomically. Returns the backup path.

    Copy, never move — nothing is ever deleted. `os.replace` is atomic only
    within one directory, so the temp file is a sibling.
    """
    backup = meta_path.with_suffix(".json.bak")
    shutil.copy2(meta_path, backup)
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=4, ensure_ascii=False) + "\n")
    os.replace(tmp, meta_path)
    return backup


def timeline_duration_us(draft_info_path: Path | str) -> int:
    """The timeline duration capcut-cli wrote, as integer microseconds."""
    return int(json.loads(Path(draft_info_path).read_text())["duration"])


def set_timeline_duration(meta_path: Path | str, duration_us: int, *,
                          force: bool = False) -> Path:
    """Mirror the timeline duration into `draft_meta_info.json` → `tm_duration`.

    CapCut's project list reads the duration from the *meta* file, not from
    draft_info.json. `capcut-cli compile` writes the timeline but leaves
    tm_duration at 0, and a 0 there is what lists a generated draft as 00:00
    — the symptom usually blamed on a stale bundled template. It is not the
    template: a draft compiled from a captured CapCut 9.1 template lists as
    00:00 just the same until this field is set [proven].
    """
    meta_path = Path(meta_path)
    if not isinstance(duration_us, int) or isinstance(duration_us, bool):
        raise ValueError(f"duration must be integer microseconds, got {duration_us!r}")
    if not force and capcut_is_running():
        raise RuntimeError("CapCut is running — it overwrites draft_meta_info.json on quit. "
                           "Quit CapCut and re-run.")
    meta = json.loads(meta_path.read_text())
    meta["tm_duration"] = duration_us
    return write_meta(meta_path, meta)


def register_media(meta_path: Path | str, probes: list[MediaProbe]) -> Registration:
    """Add entries for `probes` to a draft's draft_meta_info.json.

    Backs the file up first, writes atomically, never removes an existing entry,
    and skips paths already registered so it is safe to re-run.
    """
    meta_path = Path(meta_path)
    for p in probes:
        if not is_draft_relative(p.path) and not Path(p.path).is_absolute():
            raise ValueError(
                f"media path must be draft-relative ('./assets/...') or absolute: {p.path}")
    if capcut_is_running():
        raise RuntimeError("CapCut is running — it overwrites draft_meta_info.json on quit. "
                           "Quit CapCut and re-run.")

    meta = json.loads(meta_path.read_text())
    zero = next(g for g in groups_of(meta) if g["type"] == 0)
    zero.setdefault("value", [])
    known = {e.get("file_Path") for e in zero["value"]}

    result = Registration(meta_path=meta_path)
    for p in probes:
        path_str = str(p.path)
        if path_str in known:
            result.already_registered.append(path_str)
            continue
        zero["value"].append(entry_for(p))
        known.add(path_str)
        result.added.append(path_str)

    result.backup = write_meta(meta_path, meta)
    return result


def main(argv=None):
    """python3 -m eyecut.media <draft_meta_info.json> <media>...

    Probes each file and registers it. Prints what it added; the real check is
    whether CapCut then opens the project without a relink prompt.
    """
    import argparse

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("meta_path")
    ap.add_argument("media", nargs="+")
    ap.add_argument("--dry-run", action="store_true", help="print entries, write nothing")
    args = ap.parse_args(argv)

    found = [probe(Path(m).resolve()) for m in args.media]
    if args.dry_run:
        print(json.dumps([entry_for(p) for p in found], indent=2))
        return 0

    result = register_media(args.meta_path, found)
    for path in result.added:
        print(f"registered {path}")
    for path in result.already_registered:
        print(f"already registered {path}")
    print(f"backup at {result.backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
