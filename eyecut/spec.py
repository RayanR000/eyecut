"""Validate a compile spec before `capcut compile` sees it.

`compile` validates a lot on its own, and where it does, this stays out of the
way: a duplicated check is a check that drifts out of step with upstream. What is
here is the complement -- the mistakes compile accepts silently, or rejects only
after `initDraft` has already seeded a directory, leaving an orphan folder behind.

The vocabulary below is compile's, not ours. eyecut renames nothing and wraps
nothing, so a feature capcut-cli gains arrives here for free; the cost is that
these constants have to be re-checked when capcut-cli updates. They come from
`dist/decorators.js` (PROPERTY_MAP, EASING_PROFILES) and `dist/compile.js`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from eyecut.ops import (ITEM_OPS, MEDIA_TRACKS, TRACK_OPS_BY_TYPE,
                        TRACK_TYPES, SpecError)

US = 1_000_000

__all__ = ["SpecError", "validate_spec", "US"]


# dist/decorators.js PROPERTY_MAP
KEYFRAME_PROPERTIES = ("position_x", "position_y", "rotation", "scale_x", "scale_y",
                       "uniform_scale", "alpha", "saturation", "contrast",
                       "brightness", "volume")
# dist/decorators.js EASING_PROFILES, plus "linear". Hyphens, not underscores.
EASINGS = ("linear", "ease-in", "ease-out", "ease-in-out")
# dist/compile.js, the op whitelist
OPERATIONS = ("transition", "filter", "effect", "keyframe", "audio-fade",
              "text-style", "text-ranges", "template", "captions")
# these carry a `target` that must name a declared item ref
TARGETED = ("transition", "keyframe", "audio-fade", "text-style", "text-ranges")
# ops that only mean anything on an audio segment
AUDIO_ONLY = ("audio-fade",)
# these apply to a stretch of TIMELINE rather than to one item: `start` +
# `duration` + `slug`, and no target. They get a track of their own in the draft.
SPANNING = ("filter", "effect")
# ops that name a file of their own rather than a declared item
FILE_OPS = {"captions": "an .srt file", "template": "a saved-template .json"}

# `{"op": "text-style", "bold": true}` dies inside capcut-cli 0.21.1 with
# "Cannot read properties of undefined (reading 'alpha')". Refused here with an
# explanation rather than passed through to crash. Only the compile OPERATION is
# broken, so the fix is not to do without: the `textStyle` item key above applies
# the same styling afterwards. Drop this when upstream fixes it -- the test that
# pins it says the same.
BROKEN_UPSTREAM = {"text-style": "capcut-cli 0.21.1 crashes on it "
                                 "(\"Cannot read properties of undefined (reading 'alpha')\"). "
                                 "Set `textStyle` on the text item instead — the same "
                                 "shadow, border and background box, applied after the "
                                 "compile with the `capcut text-style` command, which works."}


def _items(spec: dict) -> list[tuple[dict, dict]]:
    """(track, item) for every item in the spec."""
    return [(track, item)
            for track in spec.get("tracks") or []
            for item in track.get("items") or []]


def _refs(spec: dict) -> dict[str, str]:
    """ref -> the type of the track it was declared on."""
    return {item["ref"]: track.get("type", "video")
            for track, item in _items(spec) if item.get("ref")}


def _check_overlaps(spec: dict) -> None:
    """Items overlapping on one track — *after* compile merges tracks.

    Two tracks of the same type collapse into one unless each carries a distinct
    `name`: compile keys the built track on (type, name), and an unnamed track
    takes the default. So a spec that reads as a base track plus an overlay
    silently becomes one track with segments on top of each other — the main-track
    corruption `eyecut.timeline` exists to prevent, and `capcut lint` calls it
    clean [proven].

    Tracks of different types overlap by design: an audio bed runs under every
    video segment, and captions run over them.
    """
    merged: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    for index, track in enumerate(spec.get("tracks") or []):
        track_type = track.get("type", "video")
        key = (track_type, track.get("name") or "")
        for item in track.get("items") or []:
            start, duration = item.get("start"), item.get("duration")
            if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
                continue
            merged.setdefault(key, []).append((start, start + duration, index))

    for (track_type, name), placed in merged.items():
        placed.sort()
        for (a_start, a_end, a_track), (b_start, b_end, b_track) in zip(placed, placed[1:]):
            if b_start >= a_end - 1e-9:
                continue
            if a_track != b_track:
                raise SpecError(
                    f"tracks[{a_track}] and tracks[{b_track}] are both {track_type} "
                    f"tracks named {name!r}, so compile merges them into one — and "
                    f"{a_start:g}–{a_end:g}s then overlaps {b_start:g}–{b_end:g}s. "
                    f"Give each track a distinct `name` to keep them apart (that is "
                    f"how an overlay or picture-in-picture is built).")
            raise SpecError(
                f"tracks[{a_track}] ({track_type}): items overlap — "
                f"{a_start:g}–{a_end:g}s and {b_start:g}–{b_end:g}s. `start` is the "
                f"TIMELINE position; the in-point into the source file is "
                f"`sourceStart`.")


def _check_keyframe(op: dict, where: str) -> None:
    if "from" in op or "to" in op:
        raise SpecError(
            f"{where}: keyframes use one operation per point, each with `time` and "
            f"`value` — not `from`/`to`. compile accepts from/to silently and writes "
            f"`time_offset: null, values: [null]`, an animation that does nothing.")
    for field in ("time", "value"):
        if not isinstance(op.get(field), (int, float)):
            raise SpecError(f"{where}: keyframe needs `{field}` (a number); "
                            f"one operation per keyframe, at least two to animate.")
    prop = op.get("property")
    if prop not in KEYFRAME_PROPERTIES:
        hint = " (a whole-frame zoom is `uniform_scale`)" if prop == "scale" else ""
        raise SpecError(f"{where}: unknown keyframe property {prop!r}{hint}. "
                        f"One of: {', '.join(KEYFRAME_PROPERTIES)}")
    easing = op.get("easing")
    if easing is not None and easing not in EASINGS:
        raise SpecError(f"{where}: unknown easing {easing!r} — hyphens, not "
                        f"underscores. One of: {', '.join(EASINGS)}")


def _check_path(value: Any, what: str, where: str) -> None:
    """Paths must be absolute.

    compile resolves a relative path against the *spec file*, and eyecut writes the
    spec into the drafts store -- so `footage/a.mp4` resolves inside
    `~/Movies/CapCut/.../com.lveditor.draft/` and the error names a path the caller
    never wrote [proven].
    """
    if not isinstance(value, str) or not value:
        raise SpecError(f"{where}: {what} is required")
    if not Path(value).is_absolute():
        raise SpecError(f"{where}: {what} must be an absolute path — compile resolves "
                        f"a relative one against the spec file, which eyecut writes "
                        f"into the drafts store, not your working directory "
                        f"(got {value!r})")


def _check_file_op(op: dict, where: str) -> None:
    """`captions` and `template` carry a file of their own, not a `target`."""
    _check_path(op.get("path"), FILE_OPS[op["op"]], where)
    if op["op"] == "template":
        for field in ("start", "duration"):
            if not isinstance(op.get(field), (int, float)):
                raise SpecError(f"{where}: `template` needs `{field}` (seconds)")
        if op["duration"] <= 0:
            raise SpecError(f"{where}: `duration` must be > 0")


def _check_span(op: dict, where: str) -> None:
    """`filter` and `effect` cover a timeline range. Every field here is one
    compile accepts missing or out of range, writing something that never
    reaches the screen."""
    if "target" in op:
        raise SpecError(f"{where}: `{op['op']}` applies to a span of timeline, not to "
                        f"one item — it takes `start` and `duration`, not `target`.")
    if not isinstance(op.get("slug"), str) or not op["slug"]:
        raise SpecError(f"{where}: `{op['op']}` needs a `slug`. "
                        f"List them with `capcut enums --scene-effects` "
                        f"(345), `--filters` (10), `--transitions` (116).")
    for field in ("start", "duration"):
        value = op.get(field)
        if not isinstance(value, (int, float)):
            raise SpecError(
                f"{where}: `{op['op']}` needs `{field}` (seconds). Without a duration "
                f"compile writes `target_timerange.duration: null`, which nulls the "
                f"whole draft's duration and breaks reading it back.")
    if op["duration"] <= 0:
        raise SpecError(f"{where}: `duration` must be > 0")
    intensity = op.get("intensity")
    if intensity is not None and not 0 <= intensity <= 1:
        raise SpecError(f"{where}: `intensity` {intensity} is outside 0–1. It is "
                        f"written verbatim, so 5.0 lands in the draft as five times "
                        f"what the CapCut UI can express.")


def _check_cover(cover: Any) -> None:
    """The draft's thumbnail. A top-level key rather than an item one: it names a
    frame of the finished edit, not anything on a track."""
    if cover is None:
        return
    if isinstance(cover, str):
        cover = {"path": cover}
    if not isinstance(cover, dict):
        raise SpecError(f"spec.cover must be a path or {{path, time}}, got {cover!r}")
    unknown = set(cover) - {"path", "time"}
    if unknown:
        raise SpecError(f"spec.cover: unknown key {sorted(unknown)[0]!r}. "
                        f"One of: path, time")
    _check_path(cover.get("path"), "`cover.path`", "spec.cover")
    time = cover.get("time")
    if time is not None and (not isinstance(time, (int, float))
                             or isinstance(time, bool) or time < 0):
        raise SpecError(f"spec.cover: `time` is seconds into the timeline, "
                        f"got {time!r}")


def _check_item_ops(item: dict, track_type: str, where: str) -> None:
    """Validate every post-compile item key the item carries.

    Driven by `eyecut.ops.ITEM_OPS`, the same table `apply_item_ops` walks, so a
    key can never be applicable in one and unknown in the other.

    Applicability is checked here as well as shape, because the CLI behind each
    key needs the segment kind it was built for: `capcut mask` wants a visual
    segment, `capcut text-style` a text one. Passed the wrong kind it exits
    non-zero *after* the draft exists, and nothing downstream re-reads it -- the
    draft simply opens looking untouched.
    """
    for op in ITEM_OPS:
        value = item.get(op.key)
        if value is None:
            continue
        if track_type not in op.tracks:
            raise SpecError(
                f"{where}: `{op.key}` only applies to "
                f"{' and '.join(op.tracks)} items, not {track_type} — it is "
                f"applied with `capcut {op.command if isinstance(op.command, str) else op.key}`, "
                f"which needs that kind of segment")
        if op.validate is not None:
            op.validate(value, track_type, where)


def _check_compile_fields(item: dict, where: str) -> None:
    """`opacity` and `rotation` are compile's own item fields, not eyecut's.

    They reach the draft through the compile itself, so there is no CLI call to
    fail on them -- an out-of-range opacity is simply written verbatim, the way
    `intensity` is. Checking the shape here is the only place it can be caught.
    """
    opacity = item.get("opacity")
    if opacity is not None:
        if not isinstance(opacity, (int, float)) or isinstance(opacity, bool):
            raise SpecError(f"{where}: `opacity` must be a number, got {opacity!r}")
        if not 0 <= opacity <= 1:
            raise SpecError(f"{where}: `opacity` {opacity} is outside 0–1. It is "
                            f"written verbatim, so anything else lands in the draft "
                            f"as a value the CapCut UI cannot express.")
    rotation = item.get("rotation")
    if rotation is not None and (not isinstance(rotation, (int, float))
                                 or isinstance(rotation, bool)):
        raise SpecError(f"{where}: `rotation` must be a number of degrees, "
                        f"got {rotation!r}")


def validate_spec(spec: dict[str, Any]) -> None:
    """Raise `SpecError` if `spec` would not build what it appears to say."""
    if not isinstance(spec, dict):
        raise SpecError(f"spec must be a dict, got {type(spec).__name__}")
    if not spec.get("tracks"):
        raise SpecError("spec.tracks is required and must hold at least one track")

    if not any(track.get("type", "video") not in TRACK_OPS_BY_TYPE
               for track in spec["tracks"]):
        raise SpecError(
            f"spec.tracks holds only {'/'.join(TRACK_OPS_BY_TYPE)} tracks, which "
            f"eyecut builds after the compile — compile itself needs at least one "
            f"video, audio or text track to build a timeline from.")
    _check_cover(spec.get("cover"))
    _check_overlaps(spec)
    for track_index, track in enumerate(spec.get("tracks") or []):
        track_type = track.get("type", "video")
        if track_type not in TRACK_TYPES:
            raise SpecError(f"tracks[{track_index}]: unknown track type "
                            f"{track_type!r}. One of: {', '.join(TRACK_TYPES)}")
        built_here = TRACK_OPS_BY_TYPE.get(track_type)
        for item_index, item in enumerate(track.get("items") or []):
            where = f"tracks[{track_index}].items[{item_index}]"
            if built_here is not None:
                # compile does not build these tracks, so their items are not
                # compile's vocabulary either: the op owns the whole shape
                built_here.validate(item, where)
                continue
            if track_type in MEDIA_TRACKS:
                _check_path(item.get("path"), "`path`", where)
            _check_item_ops(item, track_type, where)
            _check_compile_fields(item, where)
    refs = _refs(spec)

    for index, op in enumerate(spec.get("operations") or []):
        where = f"operations[{index}]"
        name = op.get("op") if isinstance(op, dict) else None
        if name not in OPERATIONS:
            raise SpecError(f"{where}: unknown op {name!r}. "
                            f"One of: {', '.join(OPERATIONS)}")
        if name in BROKEN_UPSTREAM:
            raise SpecError(f"{where}: `{name}` is unusable — {BROKEN_UPSTREAM[name]}")
        if name in TARGETED:
            target = op.get("target")
            if target not in refs:
                known = ", ".join(sorted(refs)) or "none declared"
                raise SpecError(f"{where}: target {target!r} is not a declared item "
                                f"`ref` (known refs: {known})")
            if name in AUDIO_ONLY and refs[target] != "audio":
                raise SpecError(f"{where}: `{name}` only applies to audio segments, "
                                f"but {target!r} is on a {refs[target]} track")
        if name in FILE_OPS:
            _check_file_op(op, where)
        if name in SPANNING:
            _check_span(op, where)
        if name == "keyframe":
            _check_keyframe(op, where)
