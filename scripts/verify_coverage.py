#!/usr/bin/env python3
"""Build the drafts that prove the new coverage, for opening in CapCut.

    python3 scripts/verify_coverage.py --footage footage/clip.mp4

Every key added for complete capcut-cli coverage is exercised across four drafts,
each one clustered so a single open of CapCut answers for several keys at once.
Nothing here asserts: the assertions live in the test suite, and they check the
files. **The app is the standard** -- a draft that lints clean can still be wrong,
and file-level evidence has been misread twice, once concluding masks worked when
they were unverified and once concluding they were broken from a screenshot of a
mask selected for editing.

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
from eyecut.timeline import DRAFT_STORE  # noqa: E402


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _cover_frame(source: Path, at: float, out: Path) -> Path:
    """A still from the footage, so the cover draft has something real to show."""
    subprocess.run(["ffmpeg", "-y", "-ss", str(at), "-i", str(source),
                    "-frames:v", "1", str(out)], capture_output=True, check=True)
    return out


def compositing(source: Path, span: float) -> tuple[dict, list[str]]:
    """Blend mode, chroma key, background blur, crop, opacity, rotation -- and the
    two-video-track overlay they mostly exist to serve."""
    shot = span / 6
    spec = {"name": "eyecut-verify-compositing", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": 0},
            {"path": str(source), "start": shot, "duration": shot,
             "sourceStart": span / 3, "bgBlur": 3, "crop": {"ratio": "9:16"}},
            {"path": str(source), "start": shot * 2, "duration": shot,
             "sourceStart": span / 2, "crop": {"rect": [0.25, 0.25, 0.5, 0.5]}}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": span / 4,
             "scale": 0.5, "mix": "screen"},
            {"path": str(source), "start": shot, "duration": shot,
             "sourceStart": span / 5, "scale": 0.5, "opacity": 0.4, "rotation": 15},
            {"path": str(source), "start": shot * 2, "duration": shot,
             "sourceStart": span / 6, "scale": 0.5,
             "chroma": {"color": "#00FF00", "intensity": 0.6}}]}]}
    return spec, [
        "clip 1: the overlay is BRIGHTER where it covers the base (screen blend), "
        "not simply on top of it",
        "clip 2: the base is a 9:16 slice with a blurred fill behind it; the "
        "overlay is see-through and tilted 15 degrees",
        "clip 3: the base is cropped to its middle quarter; the overlay has any "
        "green keyed out",
        "throughout: the overlay sits OVER the base, never appended after it",
    ]


def text(source: Path, span: float) -> tuple[dict, list[str]]:
    """textRanges and bubble, over the textStyle that was already proven."""
    spec = {"name": "eyecut-verify-text", "tracks": [
        {"type": "video", "items": [
            {"path": str(source), "start": 0, "duration": min(span, 9),
             "sourceStart": 0}]},
        {"type": "text", "name": "captions", "items": [
            {"text": "GOLD and white", "start": 0, "duration": 3, "fontSize": 12,
             "textRanges": [{"start": 0, "end": 4, "font_color": "#FFD700",
                             "bold": True}]},
            {"text": "in a bubble", "start": 3, "duration": 3, "fontSize": 12,
             "bubble": "cloud"},
            {"text": "styled too", "start": 6, "duration": 3, "fontSize": 12,
             "bubble": "rounded",
             "textStyle": {"borderWidth": 0.08, "borderColor": "#000000",
                           "shadow": True, "shadowAlpha": 0.6}}]}]}
    return spec, [
        "caption 1: 'GOLD' is gold and bold, 'and white' is not -- one segment, "
        "two styles",
        "caption 2: the words sit inside a cloud-shaped bubble",
        "caption 3: a rounded bubble AND the border/shadow, both at once",
    ]


def tracks(source: Path, span: float, cover: Path) -> tuple[dict, list[str]]:
    """The tracks compile does not build, and the draft's thumbnail.

    No sticker: `add-sticker` takes a raw resource id and there is no
    `capcut enums --stickers` to get one from. Place a sticker by hand, run
    `capcut harvest-enums`, then add it here with its id.
    """
    catalogue = json.loads(subprocess.run(
        ["capcut", "enums", "--audio-effects"],
        capture_output=True, text=True, check=True).stdout)
    slug = catalogue[0]["slug"]
    spec = {"name": "eyecut-verify-tracks",
            "cover": {"path": str(cover), "time": 1},
            "tracks": [
                {"type": "video", "items": [
                    {"path": str(source), "start": 0, "duration": min(span, 8),
                     "sourceStart": 0}]},
                {"type": "sfx", "name": "hits", "items": [
                    {"slug": slug, "start": 1, "duration": 2, "volume": 0.6},
                    {"slug": slug, "start": 4, "duration": 2, "volume": 0.3}]}]}
    return spec, [
        f"an audio track named 'hits' with two {slug} effects, at 1s and 4s",
        "the second is audibly quieter than the first",
        "the draft's thumbnail in the project list is the frame from 1s, not "
        "the first frame",
    ]


def regression(source: Path, span: float) -> tuple[dict, list[str]]:
    """The bug this work started from: a mask on the base of a two-track spec.

    Keying segments on (type, position) alone put it on the overlay instead --
    silently, since the counts agreed. Worth an eye even though a test covers it,
    because this is the class of failure the files report as clean.
    """
    shot = min(span, 8)
    spec = {"name": "eyecut-verify-regression", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": shot, "sourceStart": 0,
             "mask": {"slug": "circle", "size": 0.5}}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": shot / 4, "duration": shot / 2,
             "sourceStart": span / 3, "scale": 0.4, "x": 0.5, "y": 0.5}]}]}
    return spec, [
        "the BASE clip is the one inside a circle",
        "the small overlay in the corner is a full rectangle, unmasked",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--footage", required=True, type=Path,
                        help="a video file at least ~12s long")
    parser.add_argument("--drafts", type=Path, default=None,
                        help="drafts store (default: this OS's CapCut store)")
    parser.add_argument("--only", action="append", default=None,
                        help="build one draft by name (compositing/text/tracks/"
                             "regression); repeatable")
    args = parser.parse_args()

    source = args.footage.resolve()
    if not source.is_file():
        parser.error(f"no such file: {source}")
    span = _probe_duration(source)
    if span < 10:
        parser.error(f"{source.name} is {span:.1f}s; the drafts need ~12s to cut from")

    store = (args.drafts or DRAFT_STORE).resolve()
    scratch = Path(__file__).resolve().parent.parent / "scratch" / "verify"
    scratch.mkdir(parents=True, exist_ok=True)
    cover = _cover_frame(source, min(1.0, span / 4), scratch / "cover.png")

    builders = {"compositing": lambda: compositing(source, span),
                "text": lambda: text(source, span),
                "tracks": lambda: tracks(source, span, cover),
                "regression": lambda: regression(source, span)}
    wanted = args.only or list(builders)

    failed = False
    for name in wanted:
        if name not in builders:
            parser.error(f"unknown draft {name!r}. One of: {', '.join(builders)}")
        spec, checklist = builders[name]()
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
        print("    look for:")
        for line in checklist:
            print(f"      [ ] {line}")

    print("\nOpen each in CapCut and record what you see. A draft that lints clean "
          "can still be wrong; the app is the standard.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
