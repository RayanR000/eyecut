"""`write_draft` — compile a spec into a CapCut draft, then finish what the
compiler leaves undone.

`capcut-cli compile` writes a correct timeline and stops there: it registers no
media (deliberately out of scope, see `eyecut.media`) and leaves `tm_duration` at
0, which lists the draft as 00:00. Both omissions are corrected here, so the draft
opens ready to adjust instead of prompting to relink every clip.

The CLI is shelled out through an injected `runner` so the compile step can be
faked in tests; `_capcut_runner` is the real one.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from eyecut import media
from eyecut.media import (MediaProbe, Registration, groups_of, probe,
                          register_media, set_timeline_duration,
                          timeline_duration_us, write_meta)
from eyecut.ops import ITEM_OPS, TRACK_OPS_BY_TYPE
from eyecut.spec import POST_OPS, validate_spec
from eyecut.template import find_template


class CompileError(RuntimeError):
    """`capcut-cli compile` exited non-zero. Carries its stderr verbatim."""


# `capcut compile` rewrites media paths to this form: the file is copied into the
# draft's own assets/ and referenced through a placeholder that CapCut expands to
# the draft folder. CapCut-authored drafts register the same file as "./assets/..."
# -- registering the original absolute path instead is what leaves the media panel
# saying "Media lost" and pops the relink dialog [proven, probes D vs E].
PLACEHOLDER = re.compile(r"^##_draftpath_placeholder_[0-9A-Fa-f-]+_##/")


@dataclass
class Draft:
    path: Path
    duration_us: int
    registration: Registration
    template: Path | None = None
    warnings: list[str] = field(default_factory=list)


def timeline_media(draft_info_path: Path) -> list[tuple[Path, str]]:
    """Every distinct file the compiled timeline references, as
    (where it now lives, how the draft must refer to it).

    Compile copies each source into the draft's own assets/ and writes the copy's
    path two different ways depending on the template it was given: the
    `##_draftpath_placeholder_...##` form CapCut uses internally, or a plain
    absolute path into the draft folder. Both mean the same file; both are
    normalised here to the "./assets/..." form CapCut registers.
    """
    draft_info_path = Path(draft_info_path)
    data = json.loads(draft_info_path.read_text())
    draft_dir = draft_info_path.parent
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for bucket in data.get("materials", {}).values():
        if not isinstance(bucket, list):
            continue
        for material in bucket:
            path = material.get("path") if isinstance(material, dict) else None
            if not path:
                continue
            relative = _inside_draft(path, draft_dir)
            if relative is None or relative in seen:
                continue
            seen.add(relative)
            found.append((draft_dir / relative, f"./{relative}"))
    return found


def _inside_draft(path: str, draft_dir: Path) -> str | None:
    """`path` as a draft-relative posix path, or None if it is not in the draft."""
    if PLACEHOLDER.match(path):
        return PLACEHOLDER.sub("", path)
    try:
        return Path(path).resolve().relative_to(draft_dir.resolve()).as_posix()
    except ValueError:
        return None


def _capcut_runner(argv: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stderr


def write_draft(spec: dict, project_dir: Path | str, probes: list[MediaProbe],
                *, runner=_capcut_runner, template: Path | str | None = None) -> Draft:
    """Compile `spec` into a draft at `project_dir` and make it openable.

    `probes` are the sources the spec names; they are validated up front so a
    missing file fails before anything is written, but they are not what gets
    registered. Compile copies each source into the draft and renames it, so the
    registration below reads the copies back out of the compiled timeline.

    The CapCut-is-running guard runs *before* the compile so a refused write
    leaves no half-built project behind, and `validate_spec` runs before that: a
    spec compile would accept but silently mis-build (a `from`/`to` keyframe) is
    caught while rejecting it still costs nothing.
    """
    project_dir = Path(project_dir)
    # Before the running-CapCut guard and before anything is written: a spec that
    # would compile into the wrong thing is cheapest to reject here.
    validate_spec(spec)
    if media.capcut_is_running():   # via the module, so the guard stays patchable
        raise RuntimeError("CapCut is running — it overwrites draft_meta_info.json on quit. "
                           "Quit CapCut and re-run.")

    store = project_dir.parent
    store.mkdir(parents=True, exist_ok=True)
    # Without this the draft compiles, lists, and then will not open at all.
    template = Path(template) if template else find_template(store)

    spec_path = store / f".{project_dir.name}.spec.json"
    spec_path.write_text(json.dumps(_compile_spec(spec), ensure_ascii=False))
    warnings: list[str] = []
    code, stderr = runner(["capcut", "compile", str(spec_path), "--out", str(project_dir),
                           "--template", str(template)], store)
    if code != 0:
        raise CompileError(f"capcut compile failed ({code}): {stderr.strip()}")
    # The CLI prints its success line on stderr too; only the warnings matter, and
    # they matter a lot -- the 6.5.0 template problem was announced on every compile
    # and went unread for want of these three lines.
    warnings += [line.strip() for line in stderr.splitlines() if "WARNING" in line]

    # `capcut register` writes the store entry that makes the draft appear in
    # CapCut's project list, and rewrites the sidecar to do it -- so it runs
    # *before* the media registration. It preserves draft_materials, but only
    # because it never sees them here [verified against 0.21.1].
    code, stderr = runner(["capcut", "register", str(project_dir),
                           "--apply", "--drafts", str(store)], store)
    if code != 0:
        raise CompileError(f"capcut register failed ({code}): {stderr.strip()}")

    # order matters: resync_speeds repairs what compile wrote, apply_item_ops
    # decorates the segments compile built -- and both run while the positions
    # `_segment_ids` matches on are still the ones compile left behind.
    warnings += resync_speeds(project_dir, runner=runner, store=store)
    warnings += apply_item_ops(spec, project_dir, runner=runner, store=store)
    # last, because these ADD segments: a sticker or sfx track built earlier
    # would shift the positions `_segment_ids` matches every op above on
    warnings += apply_track_ops(spec, project_dir, runner=runner, store=store)
    # and immediately after, because `add-sfx` writes a segment CapCut deletes
    repair_sfx_materials(project_dir)
    warnings += apply_post_ops(spec, project_dir, runner=runner, store=store)
    warnings += apply_cover(spec, project_dir, runner=runner, store=store)
    mirror_timeline(project_dir)
    meta_path = project_dir / "draft_meta_info.json"
    clear_inherited_media(meta_path)
    copied = timeline_media(project_dir / "draft_info.json")
    registration = register_media(
        meta_path, [replace(probe(actual), path=relative) for actual, relative in copied])
    warnings += prune_inherited_assets(project_dir, [actual for actual, _ in copied])
    duration_us = timeline_duration_us(project_dir / "draft_info.json")
    set_timeline_duration(meta_path, duration_us)
    return Draft(path=project_dir, duration_us=duration_us, registration=registration,
                 template=template, warnings=warnings)


def apply_item_ops(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                   store: Path | None = None) -> list[str]:
    """Apply every post-compile item key in the spec to the segment compile made
    for it.

    `capcut compile` builds a timeline and stops. Masks, text styling, animation,
    blend modes, chroma keys, background blur, crops, text ranges and bubbles are
    each a separate capcut-cli command against a segment id, so they are applied
    here afterwards -- the way `resync_speeds` repairs speed. `eyecut.ops.ITEM_OPS`
    is the list; this is the loop.

    Items are matched to segments by position (`_segment_ids`). If the counts
    disagree the op is skipped with a warning rather than guessed at: an effect on
    the wrong shot is worse than none.

    A failing CLI call becomes a warning naming the item rather than an exception,
    because most of what can fail here is an unknown slug -- `capcut enums` carries
    hundreds and the app's store adds more, so they cannot be checked up front.
    The draft is already written and worth keeping; silence is what would not be,
    since a clip that never animates has nothing in the draft to say why.
    """
    segments = None  # read once, and only if something asks for it
    store = store or project_dir.parent
    warnings: list[str] = []
    pending: list[str] = []
    for op in ITEM_OPS:
        applied = False
        for where, _item, value in _spec_items(spec, op.key, types=op.tracks):
            if segments is None:
                segments = _segment_ids(project_dir)
            segment_id = segments.get(where)
            if segment_id is None:
                warnings.append(f"{op.key} on {_where(where)} skipped: compile "
                                f"produced no matching segment")
                continue
            argv = op.argv(value, project_dir, segment_id, where[0])
            code, stderr = runner(argv, store)
            if code != 0:
                warnings.append(f"{op.key} on {_where(where)} failed: "
                                f"{stderr.strip()[:120]}")
            else:
                applied = True
        if applied and op.after is not None and op.after not in pending:
            pending.append(op.after)
    # after every CLI call, not after each one. Each `capcut` command rewrites
    # draft_info.json from what it recognises, so a material a hook adds is
    # dropped by the next op's call -- silently, and only when a spec carries
    # both. Single-key drafts never showed it [proven]: a mask and a blend mode
    # in one spec lost the blend mode, its `check_flag` and the repaired chroma.
    for hook in pending:
        AFTER_HOOKS[hook](project_dir)
    return warnings


def _segment_ids(project_dir: Path) -> dict[tuple[str, int, int], str]:
    """(track type, track position, item position) -> segment id, for the timeline
    compile just built.

    Items are matched to segments by position: the nth item of the spec's nth
    track of a type is the nth segment of the built nth track of that type.
    Compile preserves both orders, and the filter/effect tracks it appends carry
    no items to confuse the count.

    The track position is load-bearing, not decoration. Keying on (type, item)
    alone collapses every video track onto one set of positions, so the last
    track of a type wins every key and a mask meant for the base clip is applied
    to the overlay -- silently, since the counts still agree and the mismatch
    guard never fires [proven failure, against the real CLI]. Two video tracks
    are how an overlay is built, so that is not a corner case.
    """
    built = json.loads((project_dir / "draft_info.json").read_text())
    seen: dict[str, int] = {}
    ids = {}
    for track in built.get("tracks", []):
        track_type = track["type"]
        ordinal = seen.get(track_type, 0)
        seen[track_type] = ordinal + 1
        for index, segment in enumerate(track.get("segments", [])):
            ids[(track_type, ordinal, index)] = segment["id"]
    return ids


# `ItemOp.after` names a repair that is cheaper over the whole draft than per
# segment. It is a name rather than the function itself because `eyecut.ops`
# cannot import this module -- this one imports it.
AFTER_HOOKS = {}


def _compile_spec(spec: dict) -> dict:
    """The spec with the parts compile cannot parse taken out.

    eyecut passes the spec through untouched wherever it can -- that is what makes
    a feature capcut-cli gains arrive here for free. The exceptions are the parts
    compile has no vocabulary for: `sticker` and `sfx` tracks, which it rejects
    outright ("tracks[1].type must be one of video|audio|text"), and the top-level
    `cover`. Both are built afterwards, so they are removed here rather than
    renamed or wrapped.
    """
    trimmed = {key: value for key, value in spec.items() if key != "cover"}
    trimmed["tracks"] = [track for track in spec.get("tracks") or []
                         if track.get("type", "video") not in TRACK_OPS_BY_TYPE]
    if trimmed.get("operations"):
        trimmed["operations"] = [op for op in trimmed["operations"]
                                 if op.get("op") not in POST_OPS]
        if not trimmed["operations"]:
            del trimmed["operations"]
    return trimmed


def apply_track_ops(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                    store: Path | None = None) -> list[str]:
    """Build the tracks compile does not: `sticker` and `sfx`.

    Compile knows video, audio and text. A sticker is an overlay resource and a
    sound effect is a catalogue lookup, so each is its own capcut-cli command that
    creates the track on first use -- one call per item, `--track-name` carrying
    the spec's track name so two of a type stay apart the way they must elsewhere.

    Unlike the item ops these match nothing: they add segments rather than
    decorate ones compile made, which is exactly why they run after everything
    that matches by position.
    """
    store = store or project_dir.parent
    warnings: list[str] = []
    for track in spec.get("tracks") or []:
        op = TRACK_OPS_BY_TYPE.get(track.get("type", "video"))
        if op is None:
            continue
        for index, item in enumerate(track.get("items") or []):
            argv = op.argv(item, project_dir, track.get("name") or "")
            code, stderr = runner(argv, store)
            if code != 0:
                warnings.append(f"{op.type} item {index} ({item[op.subject]}) "
                                f"failed: {stderr.strip()[:120]}")
    return warnings


#: What CapCut names its own thumbnail, and the size it writes. Every draft's
#: `draft_meta_info.draft_cover` already says `draft_cover.jpg` -- the template's
#: default, which eyecut inherits -- so the only thing missing has always been a
#: file at that name. Read off 28 CapCut-authored drafts, all 1920x1080 [proven].
COVER_NAME = "draft_cover.jpg"
COVER_SIZE = (1920, 1080)


def apply_cover(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                store: Path | None = None) -> list[str]:
    """Write the thumbnail CapCut's project list actually reads.

    `capcut add-cover` writes `draft_info.cover` and nothing else -- no image
    file, and the list goes on showing black [proven]. But the list is not
    reading that key at all: it opens `draft_cover.jpg` beside the draft, the
    name the meta already carries. So this does not need the CLI. It renders the
    caller's image to that name at CapCut's own size, letterboxed rather than
    stretched, which is what makes the thumbnail appear without the project ever
    being opened.

    `time` is accepted and ignored: it addressed a frame of the timeline for a
    key nothing reads. The image is the cover.
    """
    cover = spec.get("cover")
    if cover is None:
        return []
    if isinstance(cover, str):
        cover = {"path": cover}
    source = Path(cover["path"])
    if not source.is_file():
        return [f"cover failed: no such file: {source}"]
    width, height = COVER_SIZE
    code, stderr = runner(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1",
         "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
         str(project_dir / COVER_NAME)],
        store or project_dir.parent)
    return [] if code == 0 else [f"cover failed: {stderr.strip()[:120]}"]


def _where(where: tuple[str, int, int]) -> str:
    """A spec coordinate as the caller wrote it, for a warning they have to act on."""
    track_type, track_index, item_index = where
    return f"{track_type} track {track_index} item {item_index}"


def _spec_items(spec: dict, key: str, *, types: tuple[str, ...] | None = None):
    """((track type, track position, item position), item, value) per item carrying
    `key`, in the same coordinates `_segment_ids` returns."""
    seen: dict[str, int] = {}
    found = []
    for track in spec.get("tracks") or []:
        track_type = track.get("type", "video")
        ordinal = seen.get(track_type, 0)
        seen[track_type] = ordinal + 1
        if types is not None and track_type not in types:
            continue
        for index, item in enumerate(track.get("items") or []):
            if item.get(key) is not None:
                found.append(((track_type, ordinal, index), item, item[key]))
    return found


def _text_style_argv(style: dict, project_dir: Path, segment_id: str) -> list[str]:
    argv = ["capcut", "text-style", str(project_dir), segment_id]
    for key, value in style.items():
        if key in TEXT_STYLE_FLAGS:
            if value:
                argv.append(f"--{key}")
        else:
            argv += [TEXT_STYLE_OPTIONS[key], str(value)]
    return argv


def apply_text_styles(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                      store: Path | None = None) -> list[str]:
    """Apply each text item's `textStyle` to the caption compile made for it.

    The `text-style` OPERATION is the wrong shape -- styling is per text item,
    matched here to its built segment, which a whole-spec operation cannot do --
    and `eyecut.spec` refuses it (see BROKEN_UPSTREAM). So the look is applied
    afterwards with the standalone `capcut text-style` command, the way `mask`
    and speed are.

    This is not cosmetic: a caption with no border or shadow is unreadable over
    footage of any brightness, and `fontSize`/`color` on the item -- all compile
    offers -- cannot supply either.
    """
    wanted = _spec_items(spec, "textStyle", types=("text",))
    if not wanted:
        return []

    segments = _segment_ids(project_dir)
    store = store or project_dir.parent
    warnings = []
    for where, _item, style in wanted:
        segment_id = segments.get(where)
        if segment_id is None:
            warnings.append(f"textStyle on {_where(where)} skipped: compile "
                            f"produced no matching segment")
            continue
        code, stderr = runner(_text_style_argv(style, project_dir, segment_id), store)
        if code != 0:
            warnings.append(f"textStyle on {_where(where)} failed: "
                            f"{stderr.strip()[:120]}")
    return warnings


def apply_animations(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                     store: Path | None = None) -> list[str]:
    """Apply each item's `anim` to the segment compile made for it.

    compile has no animation operation, so intros and outros are applied
    afterwards the way `mask` and `textStyle` are. The command depends on what is
    being animated: `capcut text-anim` for a caption, `capcut image-anim` for a
    clip or still. Both take the same --intro/--outro/--duration shape, so the
    only difference is the verb.

    An unknown slug is what this cannot catch up front -- there are 318 across the
    five catalogues -- so the CLI's refusal is turned into a warning naming the
    item. Silence would leave a clip that simply never animates, with nothing in
    the draft to say why.
    """
    wanted = _spec_items(spec, "anim")
    if not wanted:
        return []

    segments = _segment_ids(project_dir)
    store = store or project_dir.parent
    warnings = []
    for where, _item, anim in wanted:
        segment_id = segments.get(where)
        if segment_id is None:
            warnings.append(f"anim on {_where(where)} skipped: compile "
                            f"produced no matching segment")
            continue
        verb = "text-anim" if where[0] == "text" else "image-anim"
        argv = ["capcut", verb, str(project_dir), segment_id]
        for key, value in anim.items():
            argv += [ANIM_OPTIONS[key], str(value)]
        code, stderr = runner(argv, store)
        if code != 0:
            warnings.append(f"anim on {_where(where)} failed: "
                            f"{stderr.strip()[:120]}")
    return warnings


def apply_post_ops(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                   store: Path | None = None) -> list[str]:
    """Execute post-compile operations. `import-ass` is the only one.

    These are CLI commands that modify an existing draft -- compile has no
    vocabulary for them. They run after all item and track ops, because they
    may add segments that would confuse position-based matching.

    `caption` and `tts` were here and are gone: both wrapped an external binary
    (whisper, `say`) around a route that already existed -- an .srt through the
    proven `captions` op, a wav on an ordinary audio track -- and Claude has a
    shell. `eyecut.spec.OUT_OF_SCOPE` refuses them by name with that recipe.
    """
    ops = [op for op in spec.get("operations") or [] if op.get("op") in POST_OPS]
    warnings: list[str] = []
    store = store or project_dir.parent
    for i, op in enumerate(ops):
        where = f"post-op[{i}] ({op['op']})"
        argv = ["capcut", "import-ass", str(project_dir), str(op["path"])]
        for key, flag in (("trackName", "--track-name"),
                          ("fontSize", "--font-size"),
                          ("color", "--color"),
                          ("timeOffset", "--time-offset")):
            if op.get(key) is not None:
                argv += [flag, str(op[key])]
        code, stderr = runner(argv, store)
        if code != 0:
            warnings.append(f"{where} failed: {stderr.strip()[:120]}")
    return warnings


def stamp_mask_ids(project_dir: Path) -> int:
    """Give every mask the `constant_material_id` CapCut gives its own.

    `capcut mask` leaves the field empty; a mask CapCut authors carries a UUID
    there. The id is self-contained -- it appears exactly once in the draft and
    references nothing -- so a fresh one is as good as CapCut's own. Written to
    match what a CapCut-authored draft looks like, captured by hand from the app.
    """
    draft_info = project_dir / "draft_info.json"
    data = json.loads(draft_info.read_text())
    stamped = 0
    for mask in data.get("materials", {}).get("common_mask", []):
        if not mask.get("constant_material_id"):
            mask["constant_material_id"] = str(uuid.uuid4()).upper()
            stamped += 1
    if stamped:
        draft_info.write_text(json.dumps(data, ensure_ascii=False))
    return stamped



AFTER_HOOKS["stamp_mask_ids"] = stamp_mask_ids


#: Where CapCut keeps the chroma shader. Fixed inside the app bundle, so a chroma
#: key is not a store asset the way a sticker is -- it is here on every install.
CHROMA_PATH = "/Applications/CapCut.app/Contents/Resources/Chroma2"

#: `capcut chroma` writes each of these under the wrong name, and CapCut reads
#: nothing: field on the left as the CLI writes it, CapCut's own on the right.
#: Captured by diffing a chroma key applied by hand in the app against the same
#: one applied by `capcut chroma` on a copy of the identical draft.
CHROMA_RENAMES = {"intensity": "intensity_value", "shadow": "shadow_value"}

#: The rest of CapCut's own shape, which the CLI omits entirely.
CHROMA_DEFAULTS = {"should_transfer_color": True, "edge_smooth_value": 0.0,
                   "spill_value": 0.0, "version": "v2", "resource_id": ""}

#: `check_flag` on a VIDEO MATERIAL is a bitmask of which effects CapCut will
#: honour on the segments using it. Everything eyecut and compile write leaves it
#: at 7, and the app then ignores an otherwise perfect material -- which is why a
#: chroma key written field-for-field like CapCut's own still did nothing.
#: Read off three segments of one hand-edited draft: plain 7, blend mode 15
#: (7|8), chroma 39 (7|32) [proven].
CHECK_FLAG_BLEND = 8
CHECK_FLAG_CHROMA = 32


def _set_check_flag(data: dict, material_ids: set[str], bit: int) -> int:
    """Turn on `bit` in `check_flag` for the named video materials.

    Note the flag lives on the *material*, not the segment, so two segments
    sharing one video material share the flag. compile writes one material per
    segment, so that does not arise on a freshly built draft -- but a draft
    CapCut has re-saved can collapse them, which is what `collapse_videos` in
    `eyecut.timeline` exists for.
    """
    changed = 0
    for material in data.get("materials", {}).get("videos", []):
        if material.get("id") in material_ids:
            flag = material.get("check_flag", 0)
            if not flag & bit:
                material["check_flag"] = flag | bit
                changed += 1
    return changed


def _segments_using(data: dict, material_type: str) -> set[str]:
    """Video-material ids of every segment referencing a material of this type."""
    by_id = {m["id"]: m for lst in data.get("materials", {}).values()
             if isinstance(lst, list)
             for m in lst if isinstance(m, dict) and "id" in m}
    used = set()
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        for segment in track.get("segments", []):
            types = {by_id.get(ref, {}).get("type")
                     for ref in segment.get("extra_material_refs", [])}
            if material_type in types:
                used.add(segment.get("material_id"))
    return used


def repair_chroma_materials(project_dir: Path) -> int:
    """Rewrite `capcut chroma`'s material into the shape CapCut reads.

    The CLI gets the hard part right -- the material is created and the segment
    references it -- and every field wrong. `type` is `chromas` where CapCut
    writes `chroma`; the strength is `intensity`/`shadow` where CapCut reads
    `intensity_value`/`shadow_value`; `color` is missing the alpha suffix CapCut
    appends; `path` is empty where CapCut points at its own shader; and four
    more fields are absent. The app therefore reads a material it does not
    recognise, which is the whole of why a correctly-written chroma key came to
    nothing [proven].

    Same class as `stamp_mask_ids` and the speed resync: the CLI reached the
    draft, and what it wrote is not what the app reads. Returns the number of
    materials repaired, and writes nothing if none.
    """
    draft_info = project_dir / "draft_info.json"
    data = json.loads(draft_info.read_text())
    repaired = 0
    for chroma in data.get("materials", {}).get("chromas", []):
        if chroma.get("type") == "chroma":
            continue                      # already CapCut's own shape
        chroma["type"] = "chroma"
        for cli_name, capcut_name in CHROMA_RENAMES.items():
            if cli_name in chroma:
                chroma[capcut_name] = float(chroma.pop(cli_name))
        colour = chroma.get("color") or ""
        # CapCut stores the key colour RGBA; the CLI takes and writes #RRGGBB
        if len(colour) == 7:
            chroma["color"] = colour + "ff"
        chroma["path"] = CHROMA_PATH
        for key, value in CHROMA_DEFAULTS.items():
            chroma.setdefault(key, value)
        repaired += 1
    # and the flag that lets the app read any of it
    repaired += _set_check_flag(data, _segments_using(data, "chroma"),
                                CHECK_FLAG_CHROMA)
    if repaired:
        draft_info.write_text(json.dumps(data, ensure_ascii=False))
    return repaired


AFTER_HOOKS["repair_chroma_materials"] = repair_chroma_materials


#: CapCut ships its blend shaders here, with a manifest naming each one's
#: `effectId`, `resourceId` and file. Reading it is what made a hand-harvested
#: catalogue unnecessary -- the sticker id had to be captured from the app
#: because no such manifest exists for stickers.
MIX_MODE_MANIFEST = Path("/Applications/CapCut.app/Contents/Resources/MixMode/"
                         "MixMode.json")

#: `capcut mix-mode`'s slug -> the manifest's `nameId`. The internal names are
#: not the UI's: `color_filter` is Screen (confirmed against a blend mode set by
#: hand, which wrote `effect_id: 871339`), and the rest read across from the
#: Chinese originals -- `dark_en` 变暗 Darken, `bright_en` 变亮 Lighten,
#: `glare_pc` 强光 Hard Light, `darken_color` 颜色加深 Color Burn.
#: `normal` is absent on purpose: it is the no-material case.
MIX_MODE_NAME_IDS = {
    "multiply": "multiply_blend_mode", "screen": "color_filter",
    "overlay": "over_lay", "soft-light": "soft_light", "hard-light": "glare_pc",
    "color-dodge": "color_dodge", "color-burn": "darken_color",
    "darken": "dark_en", "lighten": "bright_en",
}

#: `capcut mix-mode` accepts these and CapCut ships no shader for them, so the
#: material could be written and would name nothing. Refused in `eyecut.spec`.
MIX_MODES_WITHOUT_A_SHADER = ("difference", "exclusion")


def _mix_mode_catalogue() -> dict:
    """The manifest, keyed by `nameId`. Empty if this install has no bundle."""
    if not MIX_MODE_MANIFEST.exists():
        return {}
    manifest = json.loads(MIX_MODE_MANIFEST.read_text())
    return {entry["nameId"]: entry for entry in manifest.get("resourceList", [])}


def repair_mix_modes(project_dir: Path) -> int:
    """Move the blend mode to where CapCut keeps it, and let the app read it.

    `capcut mix-mode` writes `mix_mode: "Screen"` as a string field on the video
    material -- a field CapCut has no reader for, which is why it is stripped on
    the first save rather than honoured [proven]. CapCut keeps a blend mode as
    its own material in `materials.effects`, referenced from the segment, and
    only draws it once `check_flag` on the video material has bit 8 set.

    So this reads the CLI's string field, builds the material the app expects
    from the manifest in CapCut's own bundle, references it from every segment
    using that video material, sets the flag, and deletes the string. Returns the
    number of segments given a blend mode.
    """
    draft_info = project_dir / "draft_info.json"
    data = json.loads(draft_info.read_text())
    catalogue = _mix_mode_catalogue()
    materials = data.setdefault("materials", {})
    effects = materials.setdefault("effects", [])

    wanted = {m["id"]: m.pop("mix_mode") for m in materials.get("videos", [])
              if isinstance(m.get("mix_mode"), str)}
    if not wanted:
        return 0

    applied = 0
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        for segment in track.get("segments", []):
            display = wanted.get(segment.get("material_id"))
            if display is None:
                continue
            slug = display.lower().replace(" ", "-")
            if slug == "normal":
                applied += 1          # nothing to reference: normal is the default
                continue
            entry = catalogue.get(MIX_MODE_NAME_IDS.get(slug, ""))
            if entry is None:
                continue              # refused in validation; nothing to write here
            material = {
                "id": str(uuid.uuid4()).upper(), "type": "mix_mode",
                "name": display, "effect_id": entry["effectId"],
                "resource_id": entry["resourceId"],
                "path": str(MIX_MODE_MANIFEST.parent / entry["path"]),
                "value": 1.0, "visible": True, "platform": "all",
                "apply_target_type": 0, "item_effect_type": 0,
                "sub_type": "manual_stretch", "adjust_params": [],
                "third_resource_id": "", "report_name": "", "category_id": "",
                "category_name": "", "category_key": "", "sub_category_id": "",
                "sub_category_name": "", "source_platform": 0, "version": "",
                "time_range": None, "formula_id": "",
            }
            effects.append(material)
            segment.setdefault("extra_material_refs", []).append(material["id"])
            _set_check_flag(data, {segment["material_id"]}, CHECK_FLAG_BLEND)
            applied += 1

    draft_info.write_text(json.dumps(data, ensure_ascii=False))
    return applied


AFTER_HOOKS["repair_mix_modes"] = repair_mix_modes


#: The four companion materials every audio segment carries. CapCut authors them
#: for its own, `capcut compile` writes them for an audio track, and `add-sfx`
#: writes none -- so a repaired segment needs them built here. Field-for-field
#: from capcut-cli's own `createCompanionMaterials`, which is what compile's
#: working audio segments are made of.
def _audio_companions() -> list[tuple[str, dict]]:
    return [
        ("speeds", {"id": str(uuid.uuid4()), "type": "speed", "speed": 1,
                    "mode": 0, "curve_speed": None}),
        ("placeholder_infos", {"id": str(uuid.uuid4()), "type": "placeholder_info",
                               "error_path": "", "error_text": "",
                               "meta_type": "none", "res_path": "", "res_text": ""}),
        ("sound_channel_mappings", {"id": str(uuid.uuid4()), "type": "none",
                                    "audio_channel_mapping": 0,
                                    "is_config_open": False}),
        ("vocal_separations", {"id": str(uuid.uuid4()), "type": "vocal_separation",
                               "choice": 0, "enter_from": "", "final_algorithm": "",
                               "production_path": "", "removed_sounds": [],
                               "time_range": None}),
    ]

#: What an audio material carries beyond its own identity. Taken from the
#: `extract_music` material capcut-cli's compile writes, and corroborated against
#: a CapCut-authored draft that had music added by hand -- the same key set.
_AUDIO_MATERIAL_DEFAULTS = {
    "category_id": "", "category_name": "", "check_flag": 1, "music_id": "",
    "request_id": "", "source_platform": 0, "team_id": "", "text_id": "",
    "tone_category_id": "", "tone_category_name": "", "tone_effect_id": "",
    "tone_effect_name": "", "tone_platform": "", "tone_second_category_id": "",
    "tone_second_category_name": "", "tone_speaker": "", "tone_type": "",
    "wave_points": [],
}

#: `compile` gives its audio segments this render index; a repaired sfx segment
#: gets the same so it layers like one.
_AUDIO_RENDER_INDEX = 11000


def repair_sfx_materials(project_dir: Path) -> int:
    """Move what `add-sfx` wrote onto a material CapCut will actually resolve.

    `capcut add-sfx` pushes a `type: "sound_effect"` material into
    `materials.audio_effects` and points the audio segment's `material_id` at it.
    **CapCut resolves an audio segment through `materials.audios`.** A segment
    whose material is not in that list has no material at all, so the app does
    not merely ignore the effect -- it deletes the entire track on save
    **[proven]**: the verify draft was reopened and came back with only its video
    track, `materials.audios` empty and both `audio_effects` entries gone.

    `audio_effects` is decoration applied *to* an audio material, not a substitute
    for one. So the repair is to build the `audios` entry the segment should have
    pointed at, keep the effect entry where an effect belongs (in the segment's
    `extra_material_refs`), and give the segment the four companions and render
    index that compile's own audio segments carry.

    The catalogue identity -- `name`, `effect_id`, `resource_id`, `md5` -- is
    carried across unchanged. It is how CapCut resolves the store resource, and it
    is all `add-sfx` is given: the effect ships with `path: ""`, no local file.
    Whether CapCut fetches the audio from those ids or needs the asset downloaded
    first is the one thing this cannot answer from the files.

    Returns the number of segments repaired, and writes nothing if none.
    """
    draft_info = project_dir / "draft_info.json"
    data = json.loads(draft_info.read_text())
    materials = data.setdefault("materials", {})
    effects = {m["id"]: m for m in materials.get("audio_effects") or []
               if m.get("type") == "sound_effect"}
    if not effects:
        return 0
    audios = materials.setdefault("audios", [])

    repaired = 0
    for track in data.get("tracks", []):
        if track.get("type") != "audio":
            continue
        for segment in track.get("segments", []):
            effect = effects.get(segment.get("material_id"))
            if effect is None:
                continue  # a normal audio segment, already pointing at `audios`
            duration = (segment.get("target_timerange") or {}).get("duration", 0)
            material_id = str(uuid.uuid4())
            audios.append({
                "id": material_id,
                "type": "sound_effect",
                "name": effect.get("name", ""),
                "path": effect.get("path", ""),
                "duration": duration,
                "effect_id": effect.get("effect_id", ""),
                "resource_id": effect.get("resource_id", ""),
                "md5": effect.get("md5", ""),
                **_AUDIO_MATERIAL_DEFAULTS,
            })
            segment["material_id"] = material_id
            refs = segment.setdefault("extra_material_refs", [])
            # the effect itself, now decorating rather than standing in for the
            # material, plus the companions the segment was built without
            refs.append(effect["id"])
            for group, companion in _audio_companions():
                materials.setdefault(group, []).append(companion)
                refs.append(companion["id"])
            segment["render_index"] = _AUDIO_RENDER_INDEX
            repaired += 1

    if repaired:
        draft_info.write_text(json.dumps(data, ensure_ascii=False))
    return repaired


def resync_speeds(project_dir: Path, *, runner=_capcut_runner,
                  store: Path | None = None) -> list[str]:
    """Make a segment's speed material agree with the segment.

    `compile` writes `segment.speed` and leaves the segment's `speed` material at
    1. **CapCut reads the material**, so a clip asked to run at 2x plays at normal
    speed while its trim is still cut for 2x -- the edit is wrong in a way that
    looks like the footage is wrong. `capcut lint` reports it as
    `speed-material-mismatch` and cannot auto-fix it; `capcut speed <segment> <n>`
    re-syncs both, and leaves source and target timeranges alone [verified].

    Returns a warning per segment it could not repair, rather than raising: a
    draft with one unfixed speed is still worth opening.
    """
    draft_info = project_dir / "draft_info.json"
    data = json.loads(draft_info.read_text())
    speeds = {m["id"]: m for m in data.get("materials", {}).get("speeds", [])}
    store = store or project_dir.parent

    mismatched = []
    for track in data.get("tracks", []):
        for segment in track.get("segments", []):
            wanted = segment.get("speed", 1)
            for ref in segment.get("extra_material_refs", []):
                material = speeds.get(ref)
                if material is not None and material.get("speed") != wanted:
                    mismatched.append((segment["id"], wanted))
                    break

    warnings = []
    for segment_id, wanted in mismatched:
        code, stderr = runner(["capcut", "speed", str(project_dir), segment_id,
                               str(wanted)], store)
        if code != 0:
            warnings.append(f"speed {wanted}x on segment {segment_id[:8]} could not be "
                            f"applied ({stderr.strip()[:120]}); CapCut will play it at 1x")
    return warnings


def mirror_timeline(project_dir: Path) -> list[Path]:
    """Give the draft its own timeline identity and put the compiled timeline
    everywhere CapCut 9.x looks for it.

    A CapCut 9.1 draft stores its timeline four times over, byte for byte: the
    draft's own draft_info.json and template-2.tmp, and the same pair again under
    Timelines/<main_timeline_id>/. `capcut compile` writes only the first.

    It also ties the two together by id: in every CapCut-authored draft,
    draft_info.json's `id` *is* the main timeline's id and the name of the folder
    holding it. Compile gives the draft a fresh id but leaves the template's
    Timelines/ folder untouched, so the two disagree -- and a draft whose timeline
    id resolves to nothing does not open at all: clicking it in the project list
    does nothing, with no error [proven].

    Both problems come from the template, so neither was visible until eyecut
    started using one. The template's timeline folder is reused rather than
    rebuilt: it carries attachment files of its own.
    """
    project = Path(project_dir)
    compiled = project / "draft_info.json"
    timeline_id = str(uuid.uuid4()).upper()          # CapCut writes these uppercase

    timeline = json.loads(compiled.read_text())
    timeline["id"] = timeline_id
    compiled.write_text(json.dumps(timeline, ensure_ascii=False))

    written = [project / "template-2.tmp"]
    index = project / "Timelines" / "project.json"
    if index.is_file():
        data = json.loads(index.read_text())
        inherited = project / "Timelines" / str(data.get("main_timeline_id"))
        folder = project / "Timelines" / timeline_id
        if inherited.is_dir() and inherited != folder:
            inherited.rename(folder)                  # keep its attachment files
        folder.mkdir(parents=True, exist_ok=True)
        data["id"] = timeline_id
        data["main_timeline_id"] = timeline_id
        for entry in data.get("timelines") or []:
            entry["id"] = timeline_id
        index.write_text(json.dumps(data, ensure_ascii=False))
        written += [folder / "draft_info.json", folder / "template-2.tmp"]
    for path in written:
        shutil.copyfile(compiled, path)   # byte-identical, as CapCut keeps them
    return written


def mirror_timeline_files(project_dir: Path | str) -> list[Path]:
    """Copy an already-correct draft_info.json to the other three places CapCut
    keeps it, leaving every id alone.

    This is the half of `mirror_timeline` that an in-place edit needs: the draft
    already has its identity, it just changed. Writing only the root file loses
    the edit outright -- CapCut reads Timelines/<main_timeline_id>/draft_info.json
    and overwrites the root from it, even when the root is the newer file
    [proven: a draft whose root said 4 clips / 10s and whose timeline folder said
    32 clips / 36s opened as 32 clips / 36s].
    """
    project = Path(project_dir)
    compiled = project / "draft_info.json"
    written = [project / "template-2.tmp"]
    index = project / "Timelines" / "project.json"
    if index.is_file():
        main_id = json.loads(index.read_text()).get("main_timeline_id")
        if main_id:
            folder = project / "Timelines" / str(main_id)
            if folder.is_dir():
                written += [folder / "draft_info.json", folder / "template-2.tmp"]
    for path in written:
        shutil.copyfile(compiled, path)
    return written


def clear_inherited_media(meta_path: Path) -> None:
    """Drop the template's own `draft_materials`.

    Compile copies the template folder wholesale, so the new draft starts out
    claiming to have imported whatever the template had. Those files are not in
    this timeline and are usually long gone from disk, so CapCut opens the draft
    with a media panel full of "Media lost" and a relink dialog for files the
    edit never used [proven -- five stale mp3s from an empty template].
    """
    meta = json.loads(meta_path.read_text())
    for group in groups_of(meta):
        group["value"] = []
    write_meta(meta_path, meta)


def prune_inherited_assets(project_dir: Path, keep: list[Path]) -> list[str]:
    """Delete the template's media that compile copied into this draft.

    `clear_inherited_media` drops the template's `draft_materials` on the
    assumption that the files behind them are "usually long gone from disk".
    When they are not, the bytes stay: compile copies the template folder
    wholesale, so a large source sitting in the template lands an unreferenced
    copy inside *every* draft compiled against it. Clearing the registration
    hides it from CapCut's media panel and leaves the disk cost behind [proven
    -- five drafts holding 5.5 GB of a test movie none of them referenced,
    noticed only because each draft was 1.1 GB].

    Conservative on purpose: only files under the draft's own `assets/`, and
    only those neither in `keep` nor named anywhere in `draft_info.json`, so
    media reached by a route the timeline scan does not model survives.
    """
    project_dir = Path(project_dir)
    assets = project_dir / "assets"
    if not assets.is_dir():
        return []
    info_path = project_dir / "draft_info.json"
    info = info_path.read_text() if info_path.is_file() else ""
    keep_real = {Path(k).resolve() for k in keep}
    removed = []
    for path in sorted(assets.rglob("*")):
        if not path.is_file() or path.resolve() in keep_real or path.name in info:
            continue
        size = path.stat().st_size
        path.unlink()
        removed.append("removed inherited media not in this timeline: "
                       f"{path.name} ({size / 1e6:.0f} MB)")
    return removed
