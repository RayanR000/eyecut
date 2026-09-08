#!/usr/bin/env python3
"""Build the drafts that prove the new coverage, for opening in CapCut.

    python3 scripts/verify_coverage.py --footage footage/clip.mp4

Every key added for complete capcut-cli coverage that is still worth looking at is
exercised across three drafts, each one clustered so a single open of CapCut
answers for several keys at once.

There was a fourth, for `sfx` and `cover`. Both are refused now -- the app kept
the sfx track only after eyecut repaired the material shape, and then rewrote it
to `type: "none"`, silent -- so the draft had nothing left to show.
Nothing here asserts: the assertions live in the test suite, and they check the
files. **The app is the standard** -- a draft that lints clean can still be wrong,
and file-level evidence has been misread three times, all on masks: once
concluding they worked when they were unverified, once concluding they were
broken from a screenshot of a mask selected for editing, and once concluding they
worked off the app's own Mask panel -- which was wrong, and only an export
settled it.

So this script's output is a checklist. Open each draft, look, and record what you
see against the "look for" lines it prints.

Safety: writes only into drafts it creates, never deletes, and refuses to run
while CapCut is open (write_draft enforces the last one).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eyecut.draft import write_draft  # noqa: E402
from eyecut.ops import MIX_MODES, MIX_MODES_UNSHIPPED  # noqa: E402
from eyecut.timeline import DRAFT_STORE  # noqa: E402


#: how long each clip in a verification draft runs. Short on purpose: these are
#: built to be WATCHED, and a checklist of six items is tedious against
#: two-minute shots. The in-points still spread across the whole source, so each
#: clip is visibly different footage.
SHOT = 3.0

#: What CapCut's Blend panel prints, where it differs from the CLI slug. Only
#: `lighten` does: the app calls that mode Brighten [proven, read off the panel].
BLEND_PANEL_NAMES = {"lighten": "Brighten"}



def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def compositing(source: Path, span: float) -> tuple[dict, list[str]]:
    """Crop, rotation -- and the two-video-track overlay they mostly exist to
    serve.

    `mix`, `chroma`, `bgBlur` and `opacity` were the point of this draft once.
    All four are refused by `validate_spec` now. `opacity` was the last to go and
    the hardest to see: this draft is what caught it, by being exported and
    measured rather than looked at.
    """
    shot = SHOT
    spec = {"name": "eyecut-verify-compositing", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": 0},
            {"path": str(source), "start": shot, "duration": shot,
             "sourceStart": span / 3, "crop": {"ratio": "9:16"}},
            {"path": str(source), "start": shot * 2, "duration": shot,
             "sourceStart": span / 2, "crop": {"rect": [0.25, 0.25, 0.5, 0.5]}}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": span / 4,
             "scale": 0.5},
            {"path": str(source), "start": shot, "duration": shot,
             "sourceStart": span / 5, "scale": 0.5, "rotation": 15},
            {"path": str(source), "start": shot * 2, "duration": shot,
             "sourceStart": span / 6, "scale": 0.5}]}]}
    return spec, [
        "clip 2: the base is a 9:16 slice and the overlay over it is tilted 15 "
        "degrees -- opaque, since `opacity` is refused",
        "clip 3: the base is cropped to its middle quarter",
        "throughout: the overlay sits OVER the base, never appended after it",
    ]


def text(source: Path, span: float) -> tuple[dict, list[str]]:
    """textRanges, over the textStyle that was already proven.

    `bubble` was the other half of this draft. It is refused now, for the same
    store-asset reason as `sfx`: the id reaches the file and the app has nothing
    local to draw.
    """
    spec = {"name": "eyecut-verify-text", "tracks": [
        {"type": "video", "items": [
            {"path": str(source), "start": 0, "duration": SHOT * 2,
             "sourceStart": 0}]},
        {"type": "text", "name": "captions", "items": [
            {"text": "GOLD and white", "start": 0, "duration": SHOT, "fontSize": 12,
             "textRanges": [{"start": 0, "end": 4, "font_color": "#FFD700",
                             "bold": True}]},
            {"text": "styled too", "start": SHOT, "duration": SHOT, "fontSize": 12,
             "textStyle": {"borderWidth": 0.08, "borderColor": "#000000",
                           "shadow": True, "shadowAlpha": 0.6}}]}]}
    return spec, [
        "caption 1: 'GOLD' is gold and bold, 'and white' is not -- one segment, "
        "two styles",
        "caption 2: the border and shadow are both there",
    ]


def chroma(source: Path, span: float) -> tuple[dict, list[str]]:
    """The first refusal to be reversed, so it needs the app more than the rest.

    `capcut chroma` puts the right material in the right place under field names
    CapCut does not read; `repair_chroma_materials` rewrites it into the shape
    captured from a key applied by hand in the app. The two overlay clips are the
    same shot, and only the second is keyed -- so anything the key removes shows
    as the base reading through where the control is solid.
    """
    shot = SHOT
    spec = {"name": "eyecut-verify-chroma", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": i * shot, "duration": shot,
             "sourceStart": span / 3} for i in range(2)]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": 0, "duration": shot,
             "sourceStart": span / 5, "scale": 0.6},
            {"path": str(source), "start": shot, "duration": shot,
             "sourceStart": span / 5, "scale": 0.6,
             "chroma": {"color": "#0d1618", "intensity": 0.6}}]}]}
    return spec, [
        "clip 1 (control): the overlay is solid, hiding the base behind it",
        "clip 2: the same shot with its dark blue punched out -- the base reads "
        "through the holes. Identical to clip 1 means the key did nothing",
        "select clip 2: Remove BG > Chroma key must be TICKED, with the colour "
        "and a non-zero Intensity. Unticked means `check_flag` bit 32 was lost, "
        "which is the half of this that no amount of correct material fixes",
    ]


def mix(source: Path, span: float) -> tuple[dict, list[str]]:
    """Every blend mode CapCut ships a shader for, one clip each.

    The slugs map onto the app's internal `nameId`s. CapCut names the selected
    mode in its own Blend panel, so checking each clip validates the whole table
    at once -- and all nine named themselves back correctly [proven], which is
    what promoted the inferred half of the mapping (`glare_pc` Hard Light,
    `darken_color` Color Burn, `dark_en`/`bright_en` Darken/Lighten) to measured.

    `lighten` is the one slug whose label differs: the app calls that mode
    **Brighten**, so that is what the checklist below asks for.
    """
    modes = [m for m in MIX_MODES if m not in MIX_MODES_UNSHIPPED and m != "normal"]
    shot = 2.0
    spec = {"name": "eyecut-verify-mix", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": i * shot, "duration": shot,
             "sourceStart": span / 3} for i in range(len(modes))]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": i * shot, "duration": shot,
             "sourceStart": span / 5, "scale": 0.6, "mix": mode}
            for i, mode in enumerate(modes)]}]}
    return spec, [
        "every clip composites -- a mode that does nothing is one whose shader "
        "id is wrong",
        *(f"at {i * shot:.0f}s, Video > Basic > Blend reads Mode: "
          f"{mode.replace('-', ' ').title()}" for i, mode in enumerate(modes)),
    ]


def regression(source: Path, span: float) -> tuple[dict, list[str]]:
    """The bug this work started from: an item key on the base of a two-track spec.

    Keying segments on (type, position) alone put it on the overlay instead --
    silently, since the counts agreed. Worth an eye even though a test covers it,
    because this is the class of failure the files report as clean.

    It was found with a mask, which is refused now: a mask reaches its segment and
    masks nothing, measured on an export. So the draft carries a blend mode
    instead, which lands the same way and can actually be seen.
    """
    shot = SHOT * 2
    spec = {"name": "eyecut-verify-regression", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": 0,
             "mix": "screen"}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": shot / 4, "duration": shot / 2,
             "sourceStart": span / 3, "scale": 0.4, "x": 0.5, "y": 0.5}]}]}
    return spec, [
        "select the BASE clip: Video > Basic > Blend reads Mode: Screen",
        "select the small overlay: Blend is unticked -- it asked for nothing",
    ]


def store_assets(source: Path, span: float) -> tuple[dict, list[str]]:
    """Animation, filter, effect, and transition — all store-asset references.

    These cannot be verified visually in a proxy because the proxy has no access
    to CapCut's bundled shaders, LUTs, or animation resources. Instead they are
    checked structurally: does the right material land on the right segment with
    the right slug, resource_id, and timeline span.
    """
    shot = SHOT
    spec = {"name": "eyecut-verify-store-assets", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": shot * 4,
             "sourceStart": 0, "ref": "base-clip"},
            {"path": str(source), "start": shot * 4, "duration": shot * 2,
             "sourceStart": span / 3,
             "anim": {"intro": "fade-in", "introDuration": 0.5}}]},
        {"type": "text", "name": "captions", "items": [
            {"text": "anim test", "start": 0, "duration": shot, "fontSize": 10,
             "anim": {"intro": "typewriter", "introDuration": 0.8}},
            {"text": "no anim", "start": shot, "duration": shot, "fontSize": 10}]},
    ], "operations": [
        {"op": "filter", "slug": "vintage", "start": 0, "duration": shot * 2},
        {"op": "effect", "slug": "blur", "start": shot * 2, "duration": shot * 2},
        {"op": "transition", "slug": "mix", "target": "base-clip"},
    ]}
    return spec, [
        "clip 1 (0-12s base): has a Vintage filter for the first 6s and a Blur "
        "effect from 6-12s",
        "clip 2 (12-18s): fades in (image-anim intro: fade-in, 0.5s)",
        "caption 1 (0-3s): Typewriter text animation intro",
        "caption 2 (3-6s): no animation (control)",
        "the cut between base clip 1 and clip 2 has a Mix transition",
    ]


def check_draft(draft_path: Path, spec: dict) -> list[str]:
    """Validate store-asset materials in a built draft against what was asked.

    Returns a list of failures; empty means all checks passed.
    """
    data = json.loads((draft_path / "draft_info.json").read_text())
    failures = []
    materials = data.get("materials", {})

    # --- animations ---
    anim_materials = materials.get("material_animations", [])
    for track in spec.get("tracks", []):
        for item in track.get("items", []):
            anim = item.get("anim")
            if not anim:
                continue
            label = item.get("text") or item.get("ref") or item.get("path", "")[-20:]
            # Find the segment for this item, then check its animation material
            found_any = False
            for a in anim_materials:
                for entry in a.get("animations", []):
                    slug = anim.get("intro") or anim.get("outro") or anim.get("combo")
                    if not slug:
                        continue
                    if entry.get("name", "").lower().replace(" ", "-") == slug.replace("-", "-"):
                        found_any = True
                    elif entry.get("id") and slug.replace("-", "_") in (
                            entry.get("name", "").lower().replace(" ", "_")):
                        found_any = True
            if not found_any and (anim.get("intro") or anim.get("outro") or anim.get("combo")):
                slug = anim.get("intro") or anim.get("outro") or anim.get("combo")
                names = [e.get("name", "") for a in anim_materials
                         for e in a.get("animations", [])]
                failures.append(f"anim '{slug}' on '{label}': not found in "
                                f"material_animations (have: {names})")

    # --- filter / effect (operations that create their own tracks) ---
    video_effects = {m["id"]: m for m in materials.get("video_effects", [])}
    for op in spec.get("operations", []):
        if op["op"] not in ("filter", "effect"):
            continue
        slug = op["slug"]
        op_start = op["start"]
        op_dur = op["duration"]
        track_type = op["op"]
        matching_tracks = [t for t in data.get("tracks", [])
                           if t.get("type") == track_type]
        found = False
        for et in matching_tracks:
            for seg in et.get("segments", []):
                seg_start = seg["target_timerange"]["start"] / 1e6
                seg_dur = seg["target_timerange"]["duration"] / 1e6
                if abs(seg_start - op_start) < 0.1 and abs(seg_dur - op_dur) < 0.1:
                    mid = seg.get("material_id")
                    mat = video_effects.get(mid)
                    if mat:
                        ename = (mat.get("name") or "").lower().replace(" ", "-")
                        if ename == slug or slug in ename:
                            found = True
                        else:
                            failures.append(
                                f"{op['op']} at {op_start}s: material name "
                                f"'{mat.get('name')}' does not match slug '{slug}'")
                            found = True
                    else:
                        failures.append(
                            f"{op['op']} at {op_start}s: segment found but "
                            f"material_id '{mid}' not in video_effects")
                        found = True
        if not found:
            failures.append(f"{op['op']} '{slug}' at {op_start}-{op_start+op_dur}s: "
                            f"no matching segment on a '{track_type}' track")

    # --- transition ---
    for op in spec.get("operations", []):
        if op["op"] != "transition":
            continue
        slug = op["slug"]
        trans_mats = materials.get("transitions", [])
        found = False
        for t in trans_mats:
            tname = (t.get("name") or "").lower().replace(" ", "-")
            if tname == slug or slug in tname:
                found = True
                if not t.get("effect_id"):
                    failures.append(f"transition '{slug}': material exists but "
                                    f"has no effect_id")
        if not found:
            failures.append(f"transition '{slug}': not found in "
                            f"materials.transitions (have: "
                            f"{[t.get('name') for t in trans_mats]})")

    # --- textStyle (border, shadow) ---
    text_mats = {m["id"]: m for m in materials.get("texts", [])}
    text_tracks = [t for t in data.get("tracks", []) if t.get("type") == "text"]
    text_segs = [s for t in text_tracks for s in t.get("segments", [])]
    for track in spec.get("tracks", []):
        if track.get("type") != "text":
            continue
        for item in track.get("items", []):
            style = item.get("textStyle")
            if not style:
                continue
            label = item.get("text", "?")
            start_us = round(item["start"] * 1e6)
            seg = next((s for s in text_segs
                        if abs(s["target_timerange"]["start"] - start_us) < 1000),
                       None)
            if seg is None:
                failures.append(f"textStyle on '{label}': no matching text segment")
                continue
            mat = text_mats.get(seg.get("material_id"))
            if mat is None:
                failures.append(f"textStyle on '{label}': text material not found")
                continue
            if style.get("borderWidth") and not mat.get("has_border"):
                failures.append(f"textStyle on '{label}': borderWidth={style['borderWidth']} "
                                f"but has_border is not set")
            if style.get("borderColor") and mat.get("border_color", "").lower() != \
                    style["borderColor"].lower():
                failures.append(f"textStyle on '{label}': borderColor mismatch — "
                                f"spec={style['borderColor']}, "
                                f"draft={mat.get('border_color')}")
            if style.get("shadow") and not mat.get("has_shadow"):
                failures.append(f"textStyle on '{label}': shadow=True "
                                f"but has_shadow is not set")
            if style.get("shadowAlpha") is not None:
                draft_alpha = mat.get("shadow_alpha", 0)
                if abs(draft_alpha - style["shadowAlpha"]) > 0.01:
                    failures.append(f"textStyle on '{label}': shadowAlpha mismatch — "
                                    f"spec={style['shadowAlpha']}, draft={draft_alpha}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--footage", required=True, type=Path,
                        help="a video file at least ~12s long")
    parser.add_argument("--drafts", type=Path, default=None,
                        help="drafts store (default: this OS's CapCut store)")
    parser.add_argument("--suffix", default="",
                        help="append to each draft name, so a rebuild lands "
                             "beside the old one (nothing is ever deleted)")
    parser.add_argument("--only", action="append", default=None,
                        help="build one draft by name (compositing/text/"
                             "chroma/mix/regression/store-assets); repeatable")
    args = parser.parse_args()

    source = args.footage.resolve()
    if not source.is_file():
        parser.error(f"no such file: {source}")
    span = _probe_duration(source)
    if span < 10:
        parser.error(f"{source.name} is {span:.1f}s; the drafts need ~12s to cut from")

    store = (args.drafts or DRAFT_STORE).resolve()
    builders = {"compositing": lambda: compositing(source, span),
                "text": lambda: text(source, span),
                "chroma": lambda: chroma(source, span),
                "mix": lambda: mix(source, span),
                "regression": lambda: regression(source, span),
                "store-assets": lambda: store_assets(source, span)}
    wanted = args.only or list(builders)

    failed = False
    for name in wanted:
        if name not in builders:
            parser.error(f"unknown draft {name!r}. One of: {', '.join(builders)}")
        spec, checklist = builders[name]()
        spec["name"] += args.suffix
        target = store / spec["name"]
        if target.exists():
            # never delete: an existing draft is the user's, and a half-built one
            # is still evidence. Say so and move on.
            print(f"! {spec['name']} already exists -- remove it in CapCut to rebuild")
            continue
        draft = write_draft(spec, target, [])
        print(f"\n=== {spec['name']} ===")
        print(f"    {draft.path}")
        for warning in draft.warnings:
            failed = True
            print(f"    WARNING {warning}")
        # JSON-level structural checks for store assets
        json_failures = check_draft(target, spec)
        if json_failures:
            for f in json_failures:
                failed = True
                print(f"    FAIL {f}")
        elif any(item.get("anim") or item.get("textStyle")
                 for track in spec.get("tracks", [])
                 for item in track.get("items", [])) or \
             any(op["op"] in ("filter", "effect", "transition")
                 for op in spec.get("operations", [])):
            print("    JSON checks: all store-asset materials verified")
        print("    look for:")
        for line in checklist:
            print(f"      [ ] {line}")

    print("\nOpen each in CapCut and record what you see. A draft that lints clean "
          "can still be wrong; the app is the standard.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
