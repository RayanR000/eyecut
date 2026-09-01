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

from typing import Any

US = 1_000_000


class SpecError(ValueError):
    """The spec would not build what the caller meant. Raised before anything is
    written, so a rejected spec leaves no half-made draft behind."""


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

# `{"op": "text-style", "bold": true}` dies inside capcut-cli 0.21.1 with
# "Cannot read properties of undefined (reading 'alpha')". Refused here with an
# explanation rather than passed through to crash. Drop this when upstream fixes
# it -- the test that pins it says the same.
BROKEN_UPSTREAM = {"text-style": "capcut-cli 0.21.1 crashes on it "
                                 "(\"Cannot read properties of undefined (reading 'alpha')\"). "
                                 "Set the look on the text item instead: fontSize, color."}


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
    """Two items overlapping *on one track*. Tracks of different types overlap by
    design -- an audio bed runs under every video segment -- so this is per track.
    """
    for index, track in enumerate(spec.get("tracks") or []):
        placed = []
        for item in track.get("items") or []:
            start = item.get("start")
            duration = item.get("duration")
            if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
                continue
            placed.append((start, start + duration))
        placed.sort()
        for (a_start, a_end), (b_start, b_end) in zip(placed, placed[1:]):
            if b_start < a_end - 1e-9:
                raise SpecError(
                    f"tracks[{index}] ({track.get('type', 'video')}): items overlap — "
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


def validate_spec(spec: dict[str, Any]) -> None:
    """Raise `SpecError` if `spec` would not build what it appears to say."""
    if not isinstance(spec, dict):
        raise SpecError(f"spec must be a dict, got {type(spec).__name__}")
    if not spec.get("tracks"):
        raise SpecError("spec.tracks is required and must hold at least one track")

    _check_overlaps(spec)
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
        if name == "keyframe":
            _check_keyframe(op, where)
