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
from eyecut.spec import (ANIM_OPTIONS, MASK_FLAGS, MASK_OPTIONS, TEXT_STYLE_FLAGS,
                        TEXT_STYLE_OPTIONS, validate_spec)
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
    spec_path.write_text(json.dumps(spec, ensure_ascii=False))
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

    warnings += resync_speeds(project_dir, runner=runner, store=store)
    warnings += apply_masks(spec, project_dir, runner=runner, store=store)
    warnings += apply_text_styles(spec, project_dir, runner=runner, store=store)
    warnings += apply_animations(spec, project_dir, runner=runner, store=store)
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


def _mask_argv(mask: str | dict, project_dir: Path, segment_id: str) -> list[str]:
    settings = {"slug": mask} if isinstance(mask, str) else dict(mask)
    argv = ["capcut", "mask", str(project_dir), segment_id, settings.pop("slug")]
    for key, value in settings.items():
        if key in MASK_FLAGS:
            if value:
                argv.append(f"--{key}")
        else:
            argv += [MASK_OPTIONS[key], str(value)]
    return argv


def apply_masks(spec: dict, project_dir: Path, *, runner=_capcut_runner,
                store: Path | None = None) -> list[str]:
    """Apply each item's `mask` to the segment compile made for it.

    Masks are the one part of the spec that is eyecut's own: compile has no mask
    operation at all, so this shells out to `capcut mask` afterwards, the way
    `resync_speeds` repairs speed.

    Items are matched to segments by position (`_segment_ids`). If the counts
    disagree the masks are skipped with a warning rather than guessed at: a mask
    on the wrong shot is worse than none.
    """
    wanted = [(track.get("type", "video"), index, item["mask"])
              for track in spec.get("tracks") or []
              for index, item in enumerate(track.get("items") or [])
              if item.get("mask") is not None]
    if not wanted:
        return []

    segments = _segment_ids(project_dir)
    store = store or project_dir.parent
    warnings = []
    applied = False
    for track_type, index, mask in wanted:
        segment_id = segments.get((track_type, index))
        if segment_id is None:
            warnings.append(f"mask on {track_type} item {index} skipped: compile "
                            f"produced no matching segment")
            continue
        code, stderr = runner(_mask_argv(mask, project_dir, segment_id), store)
        if code != 0:
            warnings.append(f"mask on {track_type} item {index} failed: "
                            f"{stderr.strip()[:120]}")
        else:
            applied = True
    if applied:
        stamp_mask_ids(project_dir)
    return warnings


def _segment_ids(project_dir: Path) -> dict[tuple[str, int], str]:
    """(track type, position) -> segment id, for the timeline compile just built.

    Items are matched to segments by position: the nth item of the spec's nth
    track of a type is the nth segment of the built track of that type. Compile
    preserves both orders, and the filter/effect tracks it appends carry no items
    to confuse the count.
    """
    built = json.loads((project_dir / "draft_info.json").read_text())
    return {(track["type"], index): segment["id"]
            for track in built.get("tracks", [])
            for index, segment in enumerate(track.get("segments", []))}


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

    The `text-style` OPERATION crashes capcut-cli 0.21.1 outright ("Cannot read
    properties of undefined (reading 'alpha')") and `eyecut.spec` refuses it. The
    standalone `capcut text-style` command it wraps is fine on the same styling
    [proven -- a border and shadow that kill compile return
    `{"ok":true,"applied":["shadow","border"]}` here], so the look is applied
    afterwards, the way `mask` and speed are.

    This is not cosmetic: a caption with no border or shadow is unreadable over
    footage of any brightness, and `fontSize`/`color` on the item -- all compile
    offers -- cannot supply either.
    """
    wanted = [(index, item["textStyle"])
              for track in spec.get("tracks") or []
              if track.get("type") == "text"
              for index, item in enumerate(track.get("items") or [])
              if item.get("textStyle") is not None]
    if not wanted:
        return []

    segments = _segment_ids(project_dir)
    store = store or project_dir.parent
    warnings = []
    for index, style in wanted:
        segment_id = segments.get(("text", index))
        if segment_id is None:
            warnings.append(f"textStyle on text item {index} skipped: compile "
                            f"produced no matching segment")
            continue
        code, stderr = runner(_text_style_argv(style, project_dir, segment_id), store)
        if code != 0:
            warnings.append(f"textStyle on text item {index} failed: "
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
    wanted = [(track.get("type", "video"), index, item["anim"])
              for track in spec.get("tracks") or []
              for index, item in enumerate(track.get("items") or [])
              if item.get("anim") is not None]
    if not wanted:
        return []

    segments = _segment_ids(project_dir)
    store = store or project_dir.parent
    warnings = []
    for track_type, index, anim in wanted:
        segment_id = segments.get((track_type, index))
        if segment_id is None:
            warnings.append(f"anim on {track_type} item {index} skipped: compile "
                            f"produced no matching segment")
            continue
        verb = "text-anim" if track_type == "text" else "image-anim"
        argv = ["capcut", verb, str(project_dir), segment_id]
        for key, value in anim.items():
            argv += [ANIM_OPTIONS[key], str(value)]
        code, stderr = runner(argv, store)
        if code != 0:
            warnings.append(f"anim on {track_type} item {index} failed: "
                            f"{stderr.strip()[:120]}")
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
