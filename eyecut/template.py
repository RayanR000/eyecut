"""Find a draft CapCut itself created, to compile against.

`capcut compile` builds from a template bundled with the CLI, and that template
declares CapCut 6.5.0. CapCut 9.x lists the resulting draft at 00:00 and then
refuses to open it:

    Current project is from an unusual path and cannot be used currently.

The message names the path; the path is not the cause. The bundled template
predates schema markers the current app requires (capcut-cli issue #67, which
prints this warning on every compile -- eyecut used to discard it). Passing
`--template <a draft CapCut made>` is the documented fix, and the only one:
no amount of repair to the generated draft afterwards makes 6.5.0 acceptable
[proven -- five CapCut launches, three failed sidecar repairs before the CLI's
own warning turned out to say exactly this].
"""
from __future__ import annotations

import json
from pathlib import Path

# What the bundled template declares. Any draft still saying this is unusable.
BUNDLED_APP_VERSION = "6.5.0"
# CapCut 9.1 writes 360000 here; the bundled template writes 7.
MIN_NATIVE_SCHEMA = 1000


class NoTemplate(RuntimeError):
    """No CapCut-authored draft to compile against. Carries what to do about it."""


def is_native(draft_dir: Path) -> bool:
    """True if this draft was written by CapCut rather than by the CLI template."""
    info = Path(draft_dir) / "draft_info.json"
    if not info.is_file():
        return False
    try:
        data = json.loads(info.read_text())
    except (ValueError, OSError):
        return False
    platform = data.get("platform") or {}
    if platform.get("app_version", BUNDLED_APP_VERSION) == BUNDLED_APP_VERSION:
        return False
    return int(data.get("version") or 0) >= MIN_NATIVE_SCHEMA


def find_template(drafts_dir: Path | str) -> Path:
    """Pick a CapCut-authored draft in `drafts_dir` to use as a compile template.

    Prefers an empty one: compile copies the template's timeline and its
    `draft_materials`, so a template with clips in it hands the new draft media
    it does not use. An empty project is what the CLI's own workaround note
    tells you to make. Registered media is the second tie-break -- a draft whose
    timeline CapCut emptied still carries its media list, and that list is what
    shows up in the new draft's media panel as "Media lost".
    """
    drafts_dir = Path(drafts_dir)
    candidates = []
    for entry in drafts_dir.iterdir():
        if not entry.is_dir() or not is_native(entry):
            continue
        data = json.loads((entry / "draft_info.json").read_text())
        tracks = len(data.get("tracks") or [])
        try:
            meta = json.loads((entry / "draft_meta_info.json").read_text())
        except (ValueError, OSError):
            meta = {}
        media = sum(len(g.get("value") or []) for g in (meta.get("draft_materials") or []))
        candidates.append((tracks, media, -entry.stat().st_mtime, entry))
    if not candidates:
        raise NoTemplate(
            f"no CapCut-authored draft in {drafts_dir} to use as a compile template. "
            "capcut-cli's bundled template declares CapCut 6.5.0, which CapCut 9.x "
            "refuses to open ('unusual path'). Create one empty project in CapCut, "
            "close it, and re-run.")
    return min(candidates)[3]
