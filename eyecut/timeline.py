"""Edit an existing CapCut draft's timeline in place, with the rules enforced.

`eyecut.draft` builds a draft from nothing via `capcut-cli compile`. This module
is the other half: opening a draft CapCut already owns, rewriting its segments,
and refusing to save anything that would corrupt it.

Every check in `Timeline.check()` is here because it was a real bug that cost real
work. They are assertions rather than documentation because prose in a notes file
only helps the reader who remembers to open it:

* **Array order is timeline order.** CapCut lays out the main video track by the
  order of `tracks[].segments`, *not* by `target_timerange.start`. Write segments
  out of order and every clip silently re-times when the project opens.
* **Boundaries must land on whole project frames.** Dividing a span into N equal
  parts gives boundaries a fraction of a frame apart, and CapCut re-quantizes on
  save -- so a run of "identical" clips opens as an uneven mix of 1, 2 and 3 frame
  holds. Compute the exact musical position, then `snap_us` it.
* **Per-segment materials must be pruned.** Each segment owns entries in seven
  other `materials` lists. Replace the segments without dropping the old entries
  and the file doubles in size every rebuild; one edit shed 6,286 orphans.
* **`materials.videos` holds one entry per file, not per segment.** CapCut's own
  re-save expands it to one per segment, which was most of a 6.3MB draft; 990
  entries collapsed to 4 took it to 3.1MB.
* **CapCut must not be running.** It reads the draft on open, holds it in memory
  for the whole app session, and flushes it back on quit -- so a write made while
  it is open is invisible and then destroyed. Only quitting releases it.

Typical use:

    tl = Timeline("GOJO_BURST")
    tl.replace_segments(my_new_segments)
    tl.save(backup_tag="preburst")     # raises unless every check passes
"""
from __future__ import annotations

import copy
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from eyecut import media
from eyecut.draft import mirror_timeline_files

DRAFT_STORE = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"

# The seven `materials` lists that carry one entry per *segment*. A segment
# references each by id; drop the segment without dropping these and the entry
# becomes an orphan that CapCut keeps but never uses.
SEGMENT_MATERIALS = (
    "speeds", "placeholder_infos", "video_effects", "canvases",
    "sound_channel_mappings", "material_colors", "vocal_separations",
)

US = 1_000_000


class TimelineError(RuntimeError):
    """A draft could not be opened, or a save was refused by `check()`."""


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    fatal: bool = True

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def snap_us(seconds: float, fps: float) -> int:
    """Microseconds of the nearest whole project frame.

    Round to a frame *index* first and only then convert to microseconds. Going
    straight to microseconds and rounding there leaves boundaries fractionally
    off the grid, which CapCut then quantizes its own way. Snapping this way held
    every cut within half a frame of its intended musical position; laying blocks
    out by dividing the span evenly drifted 26ms on average.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return int(round(round(seconds * fps) * US / fps))


def on_grid(us: int, fps: float, tol_us: int = 1) -> bool:
    """Is this microsecond position a whole project frame?"""
    return abs(us - snap_us(us / US, fps)) <= tol_us


def uid() -> str:
    """CapCut writes segment and material ids uppercase."""
    return str(uuid.uuid4()).upper()


class Timeline:
    """An open `draft_info.json`, guarded on entry and validated on exit."""

    def __init__(self, project: str | Path, *, store: Path | str | None = None,
                 allow_capcut_running: bool = False):
        store = Path(store) if store else DRAFT_STORE
        self.dir = Path(project) if Path(project).is_absolute() else store / str(project)
        self.path = self.dir / "draft_info.json"

        if not self.path.exists():
            raise TimelineError(f"no draft_info.json at {self.dir}")
        if not allow_capcut_running:
            if media.capcut_is_running():
                raise TimelineError(
                    "CapCut is running. It flushes its in-memory copy of this draft on quit, "
                    "so this write would be silently discarded. Quit CapCut (⌘Q — closing the "
                    "project is not enough) and re-run."
                )
            if (self.dir / ".locked").exists():
                raise TimelineError(
                    f"{self.dir.name} is marked open by CapCut (.locked present). "
                    "Quit CapCut and re-run."
                )
        self.d = json.loads(self.path.read_text())

    # ---- reading -----------------------------------------------------------

    @property
    def fps(self) -> float:
        return float(self.d.get("fps") or 30.0)

    @property
    def duration_us(self) -> int:
        return int(self.d.get("duration") or 0)

    def track(self, kind: str = "video", index: int = 0) -> dict:
        tracks = [t for t in self.d["tracks"] if t.get("type") == kind]
        if not tracks:
            raise TimelineError(f"draft has no {kind} track")
        return tracks[index]

    @property
    def segments(self) -> list[dict]:
        """The main video track's segments, in array order (= timeline order)."""
        return self.track("video")["segments"]

    def source_name(self, segment: dict) -> str:
        """Filename of the media a segment plays."""
        for m in self.d["materials"].get("videos", []):
            if m.get("id") == segment.get("material_id"):
                return m.get("material_name") or m.get("name") or ""
        return ""

    # ---- writing -----------------------------------------------------------

    def replace_segments(self, segments: list[dict], kind: str = "video") -> None:
        """Swap in a new segment list and clean up after the old one."""
        self.track(kind)["segments"] = list(segments)
        self.sort_segments(kind)
        self.prune_orphans()

    def sort_segments(self, kind: str = "video") -> None:
        """Put segments in start-time order, which is the order CapCut plays them."""
        t = self.track(kind)
        t["segments"] = sorted(t["segments"],
                               key=lambda s: s["target_timerange"]["start"])

    def prune_orphans(self) -> int:
        """Drop `materials` entries no surviving segment references.

        Returns how many went. Every rebuild must call this or the draft grows by
        seven entries per replaced clip forever.
        """
        used: set[str] = set()
        for tr in self.d["tracks"]:
            for s in tr.get("segments", []):
                used.add(s.get("material_id"))
                for key in ("extra_material_refs", "material_refs"):
                    used.update(s.get(key) or [])
        removed = 0
        for key in SEGMENT_MATERIALS:
            entries = self.d["materials"].get(key)
            if not isinstance(entries, list):
                continue
            keep = [m for m in entries
                    if not isinstance(m, dict) or m.get("id") in used]
            removed += len(entries) - len(keep)
            self.d["materials"][key] = keep
        return removed

    def collapse_videos(self) -> int:
        """Reduce `materials.videos` to one entry per distinct source file.

        CapCut re-saves expand this list to one entry per segment. Segments are
        repointed at the surviving entry for their file. Returns how many entries
        were removed.
        """
        videos = self.d["materials"].get("videos") or []
        first: dict[str, dict] = {}
        remap: dict[str, str] = {}
        for m in videos:
            key = m.get("material_name") or m.get("name") or m.get("path") or m.get("id")
            if key in first:
                remap[m["id"]] = first[key]["id"]
            else:
                first[key] = m
        if not remap:
            return 0
        for tr in self.d["tracks"]:
            for s in tr.get("segments", []):
                if s.get("material_id") in remap:
                    s["material_id"] = remap[s["material_id"]]
        self.d["materials"]["videos"] = list(first.values())
        return len(remap)

    # ---- validation --------------------------------------------------------

    def check(self, *, allow_gaps: bool = True) -> list[Problem]:
        """Every rule this module knows, as a list of problems (empty = clean)."""
        out: list[Problem] = []
        fps = self.fps
        segs = self.segments

        if fps <= 0:
            out.append(Problem("fps", f"project fps is {fps}"))
            return out

        # 1. array order is playback order
        starts = [s["target_timerange"]["start"] for s in segs]
        if starts != sorted(starts):
            bad = next(i for i in range(1, len(starts)) if starts[i] < starts[i - 1])
            out.append(Problem(
                "unsorted",
                f"segments are not in start-time order (index {bad} starts at "
                f"{starts[bad]}us, after {starts[bad-1]}us). CapCut plays the array "
                f"in order, so every clip from here on would re-time. Call sort_segments()."
            ))

        # 2. no overlaps
        for i in range(1, len(segs)):
            prev, cur = segs[i - 1]["target_timerange"], segs[i]["target_timerange"]
            end = prev["start"] + prev["duration"]
            if cur["start"] < end:
                out.append(Problem(
                    "overlap",
                    f"segment {i} starts at {cur['start']}us but {i-1} runs to {end}us "
                    f"({end - cur['start']}us of overlap)"))
                break

        # 3. gaps in the main track play as black
        if not allow_gaps:
            for i in range(1, len(segs)):
                prev, cur = segs[i - 1]["target_timerange"], segs[i]["target_timerange"]
                end = prev["start"] + prev["duration"]
                if cur["start"] > end + 1:
                    out.append(Problem(
                        "gap", f"{cur['start'] - end}us of empty track before segment {i} "
                               f"(plays as black)"))
                    break

        # 4. frame grid
        off = []
        for i, s in enumerate(segs):
            tr = s["target_timerange"]
            for label, us in (("start", tr["start"]), ("end", tr["start"] + tr["duration"])):
                if not on_grid(us, fps):
                    off.append(f"segment {i} {label}={us}us")
        if off:
            out.append(Problem(
                "off-grid",
                f"{len(off)} boundaries are not on the {fps:g}fps frame grid "
                f"(first: {off[0]}). CapCut re-quantizes these on save, which turns a "
                f"run of equal-length clips into an uneven mix. Use snap_us()."))

        # 5. orphaned per-segment materials
        before = json.dumps(self.d["materials"], sort_keys=True)
        probe = copy.deepcopy(self)
        n = probe.prune_orphans()
        del before, probe
        if n:
            out.append(Problem(
                "orphans",
                f"{n} unreferenced per-segment material entries. They bloat the draft "
                f"on every rebuild. Call prune_orphans()."))

        # 6. one materials.videos entry per file
        videos = self.d["materials"].get("videos") or []
        names = [m.get("material_name") or m.get("name") for m in videos]
        if len(names) != len(set(names)):
            out.append(Problem(
                "duplicate-videos",
                f"materials.videos has {len(names)} entries for {len(set(names))} distinct "
                f"files. Call collapse_videos().", fatal=False))

        # 7. every segment resolves to a material
        known = {m.get("id") for m in videos}
        missing = [i for i, s in enumerate(segs) if s.get("material_id") not in known]
        if missing:
            out.append(Problem(
                "dangling-material",
                f"{len(missing)} segments reference a material that is not in "
                f"materials.videos (first: index {missing[0]})"))

        # 8. source ranges inside the media
        dur = {m.get("id"): m.get("duration") for m in videos if m.get("duration")}
        for i, s in enumerate(segs):
            st = s.get("source_timerange") or {}
            limit = dur.get(s.get("material_id"))
            if limit and st and st["start"] + st["duration"] > limit + 1:
                out.append(Problem(
                    "source-overrun",
                    f"segment {i} reads to {st['start'] + st['duration']}us of a "
                    f"{limit}us source ({self.source_name(s) or 'unknown file'})"))
                break

        return out

    # ---- saving ------------------------------------------------------------

    def save(self, *, backup_tag: str | None = None, allow_gaps: bool = True,
             force: bool = False) -> list[Problem]:
        """Validate, back up, write. Raises `TimelineError` on any fatal problem.

        `backup_tag` writes `draft_info.json.<tag>_bak` next to the draft first;
        CapCut ignores files it does not recognise, and having the pre-change state
        on disk is what makes a rebuild script safe to re-run.

        Returns the non-fatal problems so a caller can report them.
        """
        problems = self.check(allow_gaps=allow_gaps)
        fatal = [p for p in problems if p.fatal]
        if fatal and not force:
            raise TimelineError(
                f"refusing to save {self.dir.name} — {len(fatal)} problem(s):\n  "
                + "\n  ".join(str(p) for p in fatal))

        if not force and media.capcut_is_running():
            raise TimelineError("CapCut started while this draft was open in memory; "
                                "quit it and re-run.")

        if backup_tag:
            bak = self.path.with_name(f"draft_info.json.{backup_tag}_bak")
            if not bak.exists():
                shutil.copy2(self.path, bak)

        self.path.write_text(json.dumps(self.d, ensure_ascii=False))
        # A CapCut 9.x draft keeps its timeline in four files, and reads the copy
        # under Timelines/<main_timeline_id>/ -- writing only this one loses the
        # whole edit without an error [proven].
        mirror_timeline_files(self.dir)
        return [p for p in problems if not p.fatal]

    def restore(self, backup_tag: str) -> None:
        """Copy a `.<tag>_bak` back over the draft.

        This is what makes a build script idempotent: restore first, then apply,
        so re-running produces the same result instead of stacking changes.
        """
        bak = self.path.with_name(f"draft_info.json.{backup_tag}_bak")
        if not bak.exists():
            raise TimelineError(f"no backup {bak.name} next to {self.dir.name}")
        shutil.copy2(bak, self.path)
        mirror_timeline_files(self.dir)   # a restore CapCut ignores is not a restore
        self.d = json.loads(self.path.read_text())

    def __deepcopy__(self, memo):
        new = object.__new__(Timeline)
        new.dir, new.path = self.dir, self.path
        new.d = copy.deepcopy(self.d, memo)
        return new
