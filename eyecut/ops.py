"""The item keys eyecut adds to compile's vocabulary, as one table.

`capcut compile` builds a timeline and stops there. Everything else CapCut can do
to a segment -- mask it, key it, blur behind it, blend it, style its text -- is a
separate capcut-cli command against a segment id, so eyecut applies it after the
compile. This module is the list of those keys and how each becomes an argv.

**One table, not one function per key.** Each op restates the same three rules:
find the segment by position, skip with a warning when there is none, turn a
non-zero exit into a warning naming the item. Written out per key that is twelve
copies of the rule SPEC.md says must never be guessed at -- and the first thing
to drift. `eyecut.draft.apply_item_ops` states them once and walks this table;
`eyecut.spec` validates against the same table, so a key cannot be applicable in
one and unknown in the other.

Adding an op is a row here, a validator if its shape needs one, and a test.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


class SpecError(ValueError):
    """The spec would not build what the caller meant. Raised before anything is
    written, so a rejected spec leaves no half-made draft behind.

    Defined here rather than in `eyecut.spec` because the validators below raise
    it and `eyecut.spec` imports this module; it is re-exported there, which is
    where callers should keep importing it from.
    """


# --------------------------------------------------------------------------
# per-op vocabulary. These come from capcut-cli's own flags: the constants are
# the CLI's option names keyed by the spec key that carries them, so a spec key
# is renamed here and nowhere else.
# --------------------------------------------------------------------------

# `capcut enums --masks`
MASK_SLUGS = ("split", "filmstrip", "circle", "rectangle", "stars", "heart",
              "text", "brush", "pen")
MASK_OPTIONS = {"centerX": "--center-x", "centerY": "--center-y", "size": "--size",
                "rotation": "--rotation", "feather": "--feather",
                "rectWidth": "--rect-width", "roundCorner": "--round-corner"}
MASK_FLAGS = ("invert",)

# `capcut text-style`. The standalone command is the whole reason `textStyle` is
# an item key: the compile OPERATION of the same name crashes (see
# BROKEN_UPSTREAM in eyecut.spec), while the command it wraps applies the
# identical border and shadow to a built segment [proven against 0.21.1].
TEXT_STYLE_OPTIONS = {
    "alpha": "--alpha", "fixedWidth": "--fixed-width", "fixedHeight": "--fixed-height",
    "shadowAlpha": "--shadow-alpha", "shadowAngle": "--shadow-angle",
    "shadowColor": "--shadow-color", "shadowDistance": "--shadow-distance",
    "shadowSmoothing": "--shadow-smoothing",
    "borderWidth": "--border-width", "borderColor": "--border-color",
    "borderAlpha": "--border-alpha",
    "bgColor": "--bg-color", "bgAlpha": "--bg-alpha", "bgStyle": "--bg-style",
    "bgRoundRadius": "--bg-round-radius", "bgWidth": "--bg-width",
    "bgHeight": "--bg-height", "bgHOffset": "--bg-h-offset", "bgVOffset": "--bg-v-offset",
    "preset": "--preset"}
TEXT_STYLE_FLAGS = ("shadow", "vertical")
# these take a "#RRGGBB" string; everything else in OPTIONS is a number
TEXT_STYLE_COLORS = ("shadowColor", "borderColor", "bgColor")
# a make-preset file, so it is a path and gets the absolute-path rule
TEXT_STYLE_PATHS = ("preset",)

# `capcut text-anim` / `image-anim`. Which command runs is decided by the track
# type: captions animate with text-anim, clips and stills with image-anim.
ANIM_OPTIONS = {"intro": "--intro", "outro": "--outro", "combo": "--combo",
                "introDuration": "--intro-duration",
                "outroDuration": "--outro-duration",
                "comboDuration": "--combo-duration"}
# a slug and the duration that belongs to it
ANIM_SLOTS = {"intro": "introDuration", "outro": "outroDuration",
              "combo": "comboDuration"}
# `capcut text-anim` takes --intro/--outro only; --combo is image-anim's
ANIM_TEXT_SLOTS = ("intro", "outro")
# the slugs are NOT checked against a list: `capcut enums` carries 318 of them
# across the five animation catalogues and the app's store adds more, so a
# whitelist here would reject valid ones. An unknown slug makes the CLI exit
# non-zero after the draft exists, which the walker turns into a warning.

# `capcut mix-mode`. A closed list, unlike the slug catalogues: these are written
# into the draft as an enum, not looked up in a store, so an unknown one is a
# mistake and not a resource eyecut has not heard of.
MIX_MODES = ("normal", "multiply", "screen", "overlay", "soft-light",
             "hard-light", "color-dodge", "color-burn", "darken", "lighten",
             "difference", "exclusion")

# `capcut crop --ratio`. Also closed: the CLI computes a centred maximal crop of
# that aspect and refuses anything else.
CROP_RATIOS = ("free", "1:1", "16:9", "9:16", "4:3", "3:4")

# `capcut bg-blur` takes a level, not a fraction: 1-4 map to 0.0625 / 0.375 /
# 0.75 / 1.0. Passing the fraction gets a level-shaped error after the draft
# exists, so it is caught here.
BG_BLUR_LEVELS = (1, 2, 3, 4)

# `capcut text-ranges --styles`, the per-range keys it accepts
TEXT_RANGE_FLAGS = ("bold", "italic", "underline")
TEXT_RANGE_NUMBERS = ("start", "end", "font_size", "font_alpha")
TEXT_RANGE_COLORS = ("font_color",)


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ItemOp:
    """One item key, and everything needed to validate it and run it.

    `single` is the sub-key a non-dict value collapses to, so `"mask": "circle"`
    and `"mask": {"slug": "circle"}` are the same thing. `positional` names the
    sub-key the CLI takes as a bare argument rather than behind a flag.
    """
    key: str
    command: str | Callable[[str], str]
    tracks: tuple[str, ...]
    single: str | None = None
    positional: str | None = None
    options: Mapping[str, str] = field(default_factory=dict)
    flags: tuple[str, ...] = ()
    formatters: Mapping[str, Callable[[Any], str]] = field(default_factory=dict)
    validate: Callable[[Any, str, str], None] | None = None
    # runs once after the op applied to at least one segment, for repairs that
    # are cheaper over the whole draft than per segment (see stamp_mask_ids)
    after: str | None = None

    def settings(self, value: Any) -> dict:
        """The value as a dict of sub-key -> value, however the caller wrote it."""
        if isinstance(value, dict):
            return dict(value)
        return {self.single: value}

    def argv(self, value: Any, project_dir: Path, segment_id: str,
             track_type: str) -> list[str]:
        command = self.command(track_type) if callable(self.command) else self.command
        argv = ["capcut", command, str(project_dir), segment_id]
        settings = self.settings(value)
        if self.positional is not None:
            argv.append(self._render(self.positional, settings.pop(self.positional)))
        for key, sub in settings.items():
            if key in self.flags:
                if sub:
                    argv.append(f"--{_dashed(key)}")
            else:
                argv += [self.options[key], self._render(key, sub)]
        return argv

    def _render(self, key: str, value: Any) -> str:
        formatter = self.formatters.get(key)
        return formatter(value) if formatter else str(value)


def _dashed(key: str) -> str:
    """`rectWidth` -> `rect-width`, the CLI's flag spelling."""
    return "".join(f"-{c.lower()}" if c.isupper() else c for c in key)


# --------------------------------------------------------------------------
# validators. Each is called with (value, track type, where) and raises
# SpecError. What they catch is what the CLI reports by exiting non-zero *after*
# the draft exists -- at which point nothing re-reads it and the draft opens
# looking untouched.
# --------------------------------------------------------------------------

def _require_dict(value: Any, key: str, where: str, what: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise SpecError(f"{where}: `{key}` must be a non-empty object of {what}, "
                        f"got {value!r}")
    return value


def _check_number(value: Any, label: str, where: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SpecError(f"{where}: {label} must be a number, got {value!r}")


def _check_color(value: Any, label: str, where: str) -> None:
    if not isinstance(value, str) or not value.startswith("#"):
        raise SpecError(f'{where}: {label} must be a "#RRGGBB" string, got {value!r}')


def _check_mask(mask: Any, track_type: str, where: str) -> None:
    slug = mask if isinstance(mask, str) else mask.get("slug") if isinstance(mask, dict) else None
    if slug not in MASK_SLUGS:
        raise SpecError(f"{where}: unknown mask {slug!r}. "
                        f"One of: {', '.join(MASK_SLUGS)}")
    if not isinstance(mask, dict):
        return
    for key, value in mask.items():
        if key == "slug" or key in MASK_FLAGS:
            continue
        if key not in MASK_OPTIONS:
            raise SpecError(f"{where}: unknown mask option {key!r}. "
                            f"One of: {', '.join(MASK_OPTIONS)}, "
                            f"{', '.join(MASK_FLAGS)}")
        _check_number(value, f"mask `{key}`", where)


def _check_text_style(style: Any, track_type: str, where: str) -> None:
    from eyecut.spec import _check_path  # its own module owns the path rule

    _require_dict(style, "textStyle", where, "options")
    for key, value in style.items():
        if key in TEXT_STYLE_FLAGS:
            continue
        if key not in TEXT_STYLE_OPTIONS:
            raise SpecError(f"{where}: unknown text style option {key!r}. "
                            f"One of: {', '.join(TEXT_STYLE_OPTIONS)}, "
                            f"{', '.join(TEXT_STYLE_FLAGS)}")
        if key in TEXT_STYLE_PATHS:
            _check_path(value, f"`{key}`", where)
        elif key in TEXT_STYLE_COLORS:
            _check_color(value, f"text style `{key}`", where)
        else:
            _check_number(value, f"text style `{key}`", where)


def _check_anim(anim: Any, track_type: str, where: str) -> None:
    _require_dict(anim, "anim", where, "intro/outro/combo")
    for key, value in anim.items():
        if key not in ANIM_OPTIONS:
            raise SpecError(f"{where}: unknown animation key {key!r}. "
                            f"One of: {', '.join(ANIM_OPTIONS)}")
        if key in ANIM_SLOTS:
            if track_type == "text" and key not in ANIM_TEXT_SLOTS:
                raise SpecError(f"{where}: `{key}` is a video animation — "
                                f"`capcut text-anim` takes only "
                                f"{' and '.join(ANIM_TEXT_SLOTS)}")
            if not isinstance(value, str) or not value:
                raise SpecError(f"{where}: animation `{key}` must be a slug string, "
                                f"got {value!r}. List them with `capcut enums "
                                f"--text-intros` and friends.")
        else:
            _check_number(value, f"animation `{key}`", where)
    for slot, duration in ANIM_SLOTS.items():
        if duration in anim and slot not in anim:
            raise SpecError(f"{where}: `{duration}` without `{slot}` animates "
                            f"nothing — the duration belongs to a slug that is "
                            f"not there.")


def _check_mix(mix: Any, track_type: str, where: str) -> None:
    mode = mix.get("mode") if isinstance(mix, dict) else mix
    if mode not in MIX_MODES:
        raise SpecError(f"{where}: unknown blend mode {mode!r}. "
                        f"One of: {', '.join(MIX_MODES)}")


def _check_chroma(chroma: Any, track_type: str, where: str) -> None:
    if isinstance(chroma, str):
        chroma = {"color": chroma}
    _require_dict(chroma, "chroma", where, "color/intensity")
    unknown = set(chroma) - {"color", "intensity"}
    if unknown:
        raise SpecError(f"{where}: unknown chroma key {sorted(unknown)[0]!r}. "
                        f"One of: color, intensity")
    _check_color(chroma.get("color"), "chroma `color`", where)
    intensity = chroma.get("intensity")
    if intensity is not None:
        _check_number(intensity, "chroma `intensity`", where)
        if not 0 <= intensity <= 1:
            raise SpecError(f"{where}: chroma `intensity` {intensity} is outside 0–1")


def _check_bg_blur(level: Any, track_type: str, where: str) -> None:
    level = level.get("level") if isinstance(level, dict) else level
    if level not in BG_BLUR_LEVELS:
        raise SpecError(f"{where}: `bgBlur` is a level, one of "
                        f"{', '.join(str(n) for n in BG_BLUR_LEVELS)} "
                        f"(0.0625 / 0.375 / 0.75 / 1.0 behind the scenes), "
                        f"got {level!r}")


def _check_crop(crop: Any, track_type: str, where: str) -> None:
    if isinstance(crop, str):
        crop = {"ratio": crop}
    _require_dict(crop, "crop", where, "ratio/rect")
    unknown = set(crop) - {"ratio", "rect"}
    if unknown:
        raise SpecError(f"{where}: unknown crop key {sorted(unknown)[0]!r}. "
                        f"One of: ratio, rect")
    if ("ratio" in crop) == ("rect" in crop):
        raise SpecError(f"{where}: `crop` takes exactly one of `ratio` "
                        f"({'/'.join(CROP_RATIOS)}) or `rect` ([x, y, w, h] as "
                        f"0–1 fractions of the source frame)")
    if "ratio" in crop:
        if crop["ratio"] not in CROP_RATIOS:
            raise SpecError(f"{where}: unknown crop ratio {crop['ratio']!r}. "
                            f"One of: {', '.join(CROP_RATIOS)}")
        return
    rect = crop["rect"]
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise SpecError(f"{where}: crop `rect` is [x, y, w, h], got {rect!r}")
    for value in rect:
        _check_number(value, "crop `rect` value", where)
        if not 0 <= value <= 1:
            raise SpecError(f"{where}: crop `rect` values are 0–1 fractions of the "
                            f"source frame, got {value!r}")


def _check_text_ranges(ranges: Any, track_type: str, where: str) -> None:
    if not isinstance(ranges, list) or not ranges:
        raise SpecError(f"{where}: `textRanges` must be a non-empty list of "
                        f"{{start, end, ...}} ranges, got {ranges!r}")
    known = set(TEXT_RANGE_FLAGS) | set(TEXT_RANGE_NUMBERS) | set(TEXT_RANGE_COLORS)
    for index, span in enumerate(ranges):
        at = f"{where}: textRanges[{index}]"
        if not isinstance(span, dict):
            raise SpecError(f"{at} must be an object, got {span!r}")
        for key, value in span.items():
            if key not in known:
                raise SpecError(f"{at}: unknown key {key!r}. "
                                f"One of: {', '.join(sorted(known))}")
            if key in TEXT_RANGE_COLORS:
                _check_color(value, f"`{key}`", at)
            elif key in TEXT_RANGE_NUMBERS:
                _check_number(value, f"`{key}`", at)
        for bound in ("start", "end"):
            if bound not in span:
                raise SpecError(f"{at}: needs `{bound}` — ranges are character "
                                f"offsets into the item's text")
        if span["end"] <= span["start"]:
            raise SpecError(f"{at}: `end` must be after `start` "
                            f"({span['start']!r}–{span['end']!r} covers nothing)")


def _check_bubble(bubble: Any, track_type: str, where: str) -> None:
    slug = bubble.get("bubble") if isinstance(bubble, dict) else bubble
    if not isinstance(slug, str) or not slug:
        raise SpecError(f"{where}: `bubble` must be a slug string, got {bubble!r}. "
                        f"List them with `capcut enums --bubbles`.")


# Applied to a segment after the compile, in this order. `opacity` and `rotation`
# are deliberately absent: compile already writes both as item fields, and going
# through `capcut opacity` would be a second way to say what compile says (their
# shape is checked in eyecut.spec, with the rest of compile's own vocabulary).
ITEM_OPS = (
    ItemOp(key="mask", command="mask", tracks=("video",), single="slug",
           positional="slug", options=MASK_OPTIONS, flags=MASK_FLAGS,
           validate=_check_mask, after="stamp_mask_ids"),
    ItemOp(key="textStyle", command="text-style", tracks=("text",),
           options=TEXT_STYLE_OPTIONS, flags=TEXT_STYLE_FLAGS,
           validate=_check_text_style),
    ItemOp(key="anim", tracks=("video", "text"),
           command=lambda t: "text-anim" if t == "text" else "image-anim",
           options=ANIM_OPTIONS, validate=_check_anim),
    ItemOp(key="mix", command="mix-mode", tracks=("video",), single="mode",
           positional="mode", validate=_check_mix),
    ItemOp(key="chroma", command="chroma", tracks=("video",), single="color",
           options={"color": "--color", "intensity": "--intensity"},
           validate=_check_chroma),
    ItemOp(key="bgBlur", command="bg-blur", tracks=("video",), single="level",
           positional="level", validate=_check_bg_blur),
    ItemOp(key="crop", command="crop", tracks=("video",), single="ratio",
           options={"ratio": "--ratio", "rect": "--rect"},
           formatters={"rect": lambda r: ",".join(str(v) for v in r)},
           validate=_check_crop),
    ItemOp(key="textRanges", command="text-ranges", tracks=("text",),
           single="styles", options={"styles": "--styles"},
           formatters={"styles": json.dumps}, validate=_check_text_ranges),
    ItemOp(key="bubble", command="bubble-text", tracks=("text",), single="bubble",
           options={"bubble": "--bubble"}, validate=_check_bubble),
)

BY_KEY = {op.key: op for op in ITEM_OPS}


# --------------------------------------------------------------------------
# tracks compile does not build
#
# `sticker` and `sfx` are the only additions that ADD segments rather than
# decorate the ones compile made. That is why they run last in `write_draft`:
# creating a segment shifts the positions `_segment_ids` matches on, so every
# per-segment op has to be applied before these exist.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackOp:
    """A whole track eyecut builds after the compile, one CLI call per item."""
    type: str
    command: str
    #: the item key the CLI takes as its first bare argument
    subject: str
    options: Mapping[str, str] = field(default_factory=dict)
    validate: Callable[[dict, str], None] | None = None

    def argv(self, item: dict, project_dir: Path, track_name: str) -> list[str]:
        argv = ["capcut", self.command, str(project_dir), str(item[self.subject]),
                str(item["start"]), str(item["duration"])]
        for key, flag in self.options.items():
            if item.get(key) is not None:
                argv += [flag, str(item[key])]
        if track_name:
            argv += ["--track-name", track_name]
        return argv


def _check_track_item(op: "TrackOp", item: dict, where: str) -> None:
    subject = item.get(op.subject)
    if not isinstance(subject, str) or not subject:
        raise SpecError(f"{where}: a {op.type} item needs `{op.subject}` (a string)")
    for bound in ("start", "duration"):
        _check_number(item.get(bound), f"`{bound}` (seconds)", where)
    if item["duration"] <= 0:
        raise SpecError(f"{where}: `duration` must be > 0")
    known = {op.subject, "start", "duration", *op.options}
    unknown = set(item) - known
    if unknown:
        raise SpecError(f"{where}: unknown {op.type} key {sorted(unknown)[0]!r}. "
                        f"One of: {', '.join(sorted(known))}")


def _check_sticker(item: dict, where: str) -> None:
    """`add-sticker` takes a raw resource id, and there is no `enums --stickers`.

    Every other catalogue in eyecut is addressed by slug; stickers are not,
    because capcut-cli publishes no sticker catalogue to look one up in. The id
    comes from `capcut harvest-enums` against a draft where a sticker was placed
    by hand. Refusing a slug-shaped value here is the only warning the caller
    gets before the CLI rejects the id after the draft exists.
    """
    _check_track_item(STICKER, item, where)
    if not item["resourceId"].isdigit():
        raise SpecError(
            f"{where}: sticker `resourceId` is a numeric resource id, not a slug "
            f"(got {item['resourceId']!r}). There is no `capcut enums --stickers`: "
            f"place one by hand in CapCut and read its id out with "
            f"`capcut harvest-enums`.")


def _check_sfx(item: dict, where: str) -> None:
    _check_track_item(SFX, item, where)
    volume = item.get("volume")
    if volume is not None and not 0 <= volume <= 1:
        raise SpecError(f"{where}: sfx `volume` {volume} is outside 0–1")


STICKER = TrackOp(type="sticker", command="add-sticker", subject="resourceId",
                  options={"x": "--x", "y": "--y", "scale": "--scale",
                           "rotation": "--rotation"},
                  validate=_check_sticker)
SFX = TrackOp(type="sfx", command="add-sfx", subject="slug",
              options={"volume": "--volume"}, validate=_check_sfx)

TRACK_OPS = (STICKER, SFX)
TRACK_OPS_BY_TYPE = {op.type: op for op in TRACK_OPS}

#: every track type a spec may declare. video/audio/text are compile's;
#: sticker/sfx are built afterwards.
TRACK_TYPES = ("video", "audio", "text", *TRACK_OPS_BY_TYPE)
#: the types whose items name a media file of their own
MEDIA_TRACKS = ("video", "audio")
