"""Spec validation: the mistakes `capcut compile` does not catch.

compile validates plenty on its own, and where it does, eyecut stays out of the
way -- a duplicated check is a check that drifts. What is here is the set compile
lets through, or rejects only after it has started writing. Every case below is
one I made in the first ten minutes of using the spec for anything past a cut
list.
"""
import pytest

from eyecut.spec import SpecError, validate_spec

VIDEO = {"type": "video", "items": [
    {"path": "/footage/a.mp4", "start": 0, "duration": 4, "ref": "shot0"}]}
AUDIO = {"type": "audio", "items": [
    {"path": "/music/bed.wav", "start": 0, "duration": 4, "ref": "bed"}]}
TEXT = {"type": "text", "items": [
    {"text": "TITLE", "start": 0, "duration": 3, "ref": "title"}]}


def spec(*tracks, operations=None):
    return {"name": "t", "tracks": list(tracks) or [VIDEO],
            "operations": operations or []}


def test_a_plain_cut_list_passes():
    validate_spec(spec())


def test_audio_and_text_tracks_pass():
    validate_spec(spec(VIDEO, AUDIO, TEXT))


def test_keyframe_from_to_is_refused_because_it_compiles_into_nothing():
    """The expensive one. `from`/`to` is the natural guess, and compile accepts it
    silently: the draft builds, `capcut lint` reports it clean, and the keyframe on
    disk reads `time_offset: null, values: [null]` -- an animation that does
    nothing, discovered only by watching. compile wants one operation per
    keyframe, each with `time` and `value` [proven].
    """
    bad = spec(operations=[{"op": "keyframe", "target": "shot0",
                            "property": "uniform_scale", "from": 1.0, "to": 1.15}])
    with pytest.raises(SpecError, match="time.*value"):
        validate_spec(bad)


def test_a_keyframe_needs_both_time_and_value():
    with pytest.raises(SpecError, match="time"):
        validate_spec(spec(operations=[
            {"op": "keyframe", "target": "shot0", "property": "alpha", "value": 1}]))
    with pytest.raises(SpecError, match="value"):
        validate_spec(spec(operations=[
            {"op": "keyframe", "target": "shot0", "property": "alpha", "time": 0}]))


def test_a_pair_of_keyframes_is_the_shape_that_animates():
    validate_spec(spec(operations=[
        {"op": "keyframe", "target": "shot0", "property": "uniform_scale",
         "time": 0.0, "value": 1.0},
        {"op": "keyframe", "target": "shot0", "property": "uniform_scale",
         "time": 4.0, "value": 1.15, "easing": "ease-in-out"}]))


def test_scale_is_not_a_property_name_uniform_scale_is():
    with pytest.raises(SpecError, match="uniform_scale"):
        validate_spec(spec(operations=[
            {"op": "keyframe", "target": "shot0", "property": "scale",
             "time": 0, "value": 1}]))


def test_easing_is_hyphenated_not_underscored():
    with pytest.raises(SpecError, match="ease-in-out"):
        validate_spec(spec(operations=[
            {"op": "keyframe", "target": "shot0", "property": "alpha",
             "time": 0, "value": 1, "easing": "ease_in_out"}]))


def test_an_audio_fade_must_target_an_audio_item():
    """compile catches this, but only after initDraft has seeded the directory --
    a failed build that leaves a folder behind."""
    with pytest.raises(SpecError, match="audio"):
        validate_spec(spec(VIDEO, AUDIO, operations=[
            {"op": "audio-fade", "target": "shot0", "fadeIn": 0.2}]))


def test_an_audio_fade_on_an_audio_item_passes():
    validate_spec(spec(VIDEO, AUDIO, operations=[
        {"op": "audio-fade", "target": "bed", "fadeIn": 0.2, "fadeOut": 0.8}]))


def test_an_operation_pointing_at_a_ref_that_does_not_exist():
    with pytest.raises(SpecError, match="nosuch"):
        validate_spec(spec(operations=[
            {"op": "transition", "target": "nosuch", "slug": "dissolve"}]))


def test_an_unknown_operation_names_the_nine_that_exist():
    with pytest.raises(SpecError, match="transition"):
        validate_spec(spec(operations=[{"op": "colour-grade", "target": "shot0"}]))


def test_text_style_is_refused_because_it_crashes_the_compiler():
    """Upstream bug, not ours: `{"op": "text-style", "bold": true}` dies inside
    capcut-cli with `Cannot read properties of undefined (reading 'alpha')`. Set
    the look on the text item (`fontSize`, `color`) until that is fixed.
    """
    with pytest.raises(SpecError, match="text-style"):
        validate_spec(spec(VIDEO, TEXT, operations=[
            {"op": "text-style", "target": "title", "bold": True}]))


def test_start_is_the_timeline_position_so_overlapping_starts_are_caught():
    """The 615-second draft: three shots given their source in-points as `start`
    compiled to a timeline with two long gaps. Nothing rejected it -- `start` is a
    valid timeline position, just not the one meant. A same-track overlap is the
    detectable half of that mistake."""
    overlapping = {"type": "video", "items": [
        {"path": "/f/a.mp4", "start": 0, "duration": 4},
        {"path": "/f/a.mp4", "start": 2, "duration": 4}]}
    with pytest.raises(SpecError, match="overlap"):
        validate_spec(spec(overlapping))


def test_sourceStart_is_not_confused_for_an_overlap():
    """Shots taken from all over a source, laid end to end -- the correct shape."""
    validate_spec(spec({"type": "video", "items": [
        {"path": "/f/a.mp4", "start": 0, "duration": 4, "sourceStart": 120},
        {"path": "/f/a.mp4", "start": 4, "duration": 3.5, "sourceStart": 300}]}))


def test_tracks_of_different_types_may_overlap_freely():
    """An audio bed under the whole edit overlaps every video segment by design."""
    validate_spec(spec(VIDEO, AUDIO, TEXT))


def test_a_filter_over_a_span_passes():
    validate_spec(spec(operations=[
        {"op": "filter", "slug": "vintage", "start": 0, "duration": 8, "intensity": 0.6}]))


def test_an_effect_over_a_span_passes():
    validate_spec(spec(operations=[
        {"op": "effect", "slug": "blur", "start": 0, "duration": 2}]))


def test_a_span_operation_without_a_duration_writes_a_null_timeline():
    """compile accepts an effect with no duration and writes
    `target_timerange.duration: null`, which makes the whole draft's `duration`
    null too. eyecut then dies reading it back -- a TypeError from media.py, on a
    draft directory that has already been created [proven].
    """
    with pytest.raises(SpecError, match="duration"):
        validate_spec(spec(operations=[{"op": "effect", "slug": "blur", "start": 0}]))
    with pytest.raises(SpecError, match="start"):
        validate_spec(spec(operations=[{"op": "filter", "slug": "vintage", "duration": 2}]))


def test_a_span_operation_needs_a_slug():
    with pytest.raises(SpecError, match="slug"):
        validate_spec(spec(operations=[{"op": "effect", "start": 0, "duration": 2}]))


def test_intensity_outside_0_to_1_is_written_verbatim():
    """`intensity: 5.0` compiles and lands in the draft as 5 -- five times the
    maximum the CapCut UI can express. Nothing downstream complains [proven]."""
    with pytest.raises(SpecError, match="0.*1"):
        validate_spec(spec(operations=[
            {"op": "filter", "slug": "vintage", "start": 0, "duration": 2,
             "intensity": 5.0}]))


def test_span_operations_take_no_target():
    """`filter` and `effect` apply to a stretch of TIMELINE, not to a clip. Giving
    one a `target` reads like it is scoped to that clip; it is not."""
    with pytest.raises(SpecError, match="target"):
        validate_spec(spec(operations=[
            {"op": "effect", "slug": "blur", "start": 0, "duration": 2,
             "target": "shot0"}]))


def test_a_mask_is_refused_however_it_is_written():
    """`mask` used to be the headline key eyecut added to compile's vocabulary,
    and it masks nothing: proven on an export, and equally inert for a mask
    CapCut applied by hand. It is refused before the draft exists, because
    everything downstream reports success -- the CLI exits 0, the material is
    correct, `capcut lint` is clean and the app's own Mask panel shows the shape
    ticked. Refused for any shape and in either spelling, dict or bare slug.
    """
    for value in ({"slug": "circle", "size": 0.6, "feather": 0.2},
                  {"slug": "heart", "invert": True},
                  "circle"):
        with pytest.raises(SpecError, match="masks nothing"):
            validate_spec(spec({"type": "video", "items": [
                {"path": "/f/a.mp4", "start": 0, "duration": 4, "mask": value}]}))


def test_a_mask_on_audio_or_text_is_refused_too():
    """Refused for the discarded reason before the applicability one, since a
    mask on an audio item is two problems and the first is fatal."""
    with pytest.raises(SpecError, match="masks nothing"):
        validate_spec(spec(AUDIO_WITH_MASK))
    with pytest.raises(SpecError, match="masks nothing"):
        validate_spec(spec(TEXT_WITH_MASK))


AUDIO_WITH_MASK = {"type": "audio", "items": [
    {"path": "/m/bed.wav", "start": 0, "duration": 4, "mask": "circle"}]}
TEXT_WITH_MASK = {"type": "text", "items": [
    {"text": "T", "start": 0, "duration": 4, "mask": "circle"}]}


def test_two_unnamed_video_tracks_would_merge_and_collide():
    """The corruption case. compile keys a built track on (type, name), so two
    video tracks without distinct names become one -- a base clip and an overlay
    land on the same track, on top of each other. `capcut lint` reports it clean
    [proven]."""
    base = {"type": "video", "items": [{"path": "/f/a.mp4", "start": 0, "duration": 6}]}
    overlay = {"type": "video", "items": [{"path": "/f/b.mp4", "start": 1, "duration": 3}]}
    with pytest.raises(SpecError, match="name"):
        validate_spec(spec(base, overlay))


def test_named_video_tracks_may_overlap_because_that_is_an_overlay():
    base = {"type": "video", "name": "main",
            "items": [{"path": "/f/a.mp4", "start": 0, "duration": 6}]}
    overlay = {"type": "video", "name": "overlay",
               "items": [{"path": "/f/b.mp4", "start": 1, "duration": 3,
                          "scale": 0.4, "x": 0.3, "y": 0.3}]}
    validate_spec(spec(base, overlay))


def test_two_unnamed_video_tracks_that_do_not_collide_are_fine():
    """Merging is only a problem when the segments actually land on each other."""
    first = {"type": "video", "items": [{"path": "/f/a.mp4", "start": 0, "duration": 2}]}
    second = {"type": "video", "items": [{"path": "/f/b.mp4", "start": 2, "duration": 2}]}
    validate_spec(spec(first, second))


def test_captions_take_an_srt_path():
    validate_spec(spec(operations=[{"op": "captions", "path": "/subs/cues.srt"}]))


def test_the_out_of_scope_ops_are_refused_with_the_route_that_replaces_them():
    """`caption` and `tts` worked. They are refused anyway: each wrapped an
    external binary (whisper, `say`) around a route that already existed, and
    the test a feature has to pass here is not "is it useful" but "can Claude do
    it another way" -- the same test that cut beat detection for being about
    music rather than about CapCut.

    Refused by name rather than left to fall through to "unknown op", because a
    working op that vanishes reads as a typo, and the person who hits this needs
    the replacement route, not a spelling check."""
    with pytest.raises(SpecError, match="out of scope.*`captions` op"):
        validate_spec(spec(VIDEO, AUDIO, operations=[
            {"op": "caption", "fromSegment": "bed", "whisperCmd": "whisper"}]))
    with pytest.raises(SpecError, match="out of scope.*ordinary `audio` track"):
        validate_spec(spec(operations=[
            {"op": "tts", "text": "hi", "ttsCmd": "cmd {out}"}]))


def test_an_unmapped_post_op_key_is_refused_rather_than_dropped():
    """The CLI takes more flags than eyecut maps (`--style-ref`, `--preset`,
    `--time-offset`...). Accepting one would be the silent no-op this module
    exists to prevent: the draft would build, lint clean, and open looking
    exactly like one nobody asked to change."""
    with pytest.raises(SpecError, match="unknown `import-ass` key 'timeOffset'"):
        validate_spec(spec(operations=[
            {"op": "import-ass", "path": "/s/a.ass", "timeOffset": 1}]))


def test_a_relative_media_path_is_refused_because_the_spec_moves():
    """compile resolves a relative path against the spec file, and eyecut writes the
    spec into the drafts store -- so `footage/a.mp4` resolves inside
    ~/Movies/CapCut/.../com.lveditor.draft/ and compile reports a path the caller
    never wrote [proven]."""
    with pytest.raises(SpecError, match="absolute"):
        validate_spec(spec({"type": "video", "items": [
            {"path": "footage/a.mp4", "start": 0, "duration": 3}]}))


def test_a_text_item_needs_no_path():
    validate_spec(spec(TEXT))


def test_a_template_needs_a_path_a_start_and_a_duration():
    validate_spec(spec(operations=[
        {"op": "template", "path": "/t/gold-title.json", "start": 0, "duration": 3,
         "text": "HELLO"}]))
    with pytest.raises(SpecError, match="duration"):
        validate_spec(spec(operations=[
            {"op": "template", "path": "/t/gold-title.json", "start": 0}]))
    with pytest.raises(SpecError, match="absolute"):
        validate_spec(spec(operations=[
            {"op": "template", "path": "templates/gold.json", "start": 0, "duration": 3}]))


def test_a_captions_srt_must_be_absolute_too():
    with pytest.raises(SpecError, match="absolute"):
        validate_spec(spec(operations=[{"op": "captions", "path": "subs/cues.srt"}]))


def test_a_text_look_is_set_on_the_item_because_the_operation_crashes():
    """The `text-style` OPERATION crashes capcut-cli 0.21.1, but the standalone
    `capcut text-style` command it wraps works [proven -- the same border and
    shadow that die in compile return `{"ok":true,"applied":["shadow","border"]}`
    when applied to a built segment]. So the look is an item key, applied after
    the compile the way `mask` is, and the refusal message points there.
    """
    validate_spec(spec(VIDEO, {"type": "text", "items": [
        {"text": "TITLE", "start": 0, "duration": 3,
         "textStyle": {"borderWidth": 0.08, "borderColor": "#000000", "shadow": True}}]}))


def test_the_refused_operation_names_the_item_key_that_replaces_it():
    with pytest.raises(SpecError, match="textStyle"):
        validate_spec(spec(VIDEO, TEXT, operations=[
            {"op": "text-style", "target": "title", "bold": True}]))


def test_an_unknown_text_style_option_is_named_rather_than_passed_through():
    """An unknown flag makes `capcut text-style` exit non-zero *after* the draft
    exists, so the caption silently keeps the default look."""
    with pytest.raises(SpecError, match="outlineWidth"):
        validate_spec(spec({"type": "text", "items": [
            {"text": "T", "start": 0, "duration": 3, "textStyle": {"outlineWidth": 2}}]}))


def test_a_text_look_on_a_video_item_is_refused():
    """`capcut text-style` needs a text segment; on a video one it exits non-zero
    after the draft is already written."""
    with pytest.raises(SpecError, match="video"):
        validate_spec(spec({"type": "video", "items": [
            {"path": "/footage/a.mp4", "start": 0, "duration": 4,
             "textStyle": {"shadow": True}}]}))


def test_an_animation_reaches_a_caption_and_a_clip_alike():
    """`anim` is eyecut's own item key, like `mask` and `textStyle`: compile has no
    animation operation, so intros and outros are applied afterwards with
    `capcut text-anim` on text and `capcut image-anim` on video.
    """
    validate_spec(spec(
        {"type": "video", "items": [
            {"path": "/footage/a.mp4", "start": 0, "duration": 4,
             "anim": {"intro": "zoom-1", "introDuration": 0.5, "combo": "bounce-1"}}]},
        {"type": "text", "items": [
            {"text": "T", "start": 0, "duration": 3,
             "anim": {"intro": "typewriter", "introDuration": 0.6, "outro": "fade-out"}}]}))


def test_a_combo_animation_on_a_caption_is_refused():
    """`capcut text-anim` takes only --intro/--outro; --combo is image-anim's. On a
    text item it would exit non-zero after the draft is written."""
    with pytest.raises(SpecError, match="combo"):
        validate_spec(spec({"type": "text", "items": [
            {"text": "T", "start": 0, "duration": 3, "anim": {"combo": "bounce-1"}}]}))


def test_an_animation_on_an_audio_item_is_refused():
    with pytest.raises(SpecError, match="audio"):
        validate_spec(spec({"type": "audio", "items": [
            {"path": "/music/bed.wav", "start": 0, "duration": 4,
             "anim": {"intro": "fade-in"}}]}))


def test_an_unknown_animation_key_is_named_rather_than_passed_through():
    with pytest.raises(SpecError, match="loop"):
        validate_spec(spec({"type": "text", "items": [
            {"text": "T", "start": 0, "duration": 3, "anim": {"loop": "wiggle"}}]}))


def test_an_animation_with_a_duration_but_no_slug_is_refused():
    """`--intro-duration` alone animates nothing; compile and the CLI both accept
    it, so the caption opens plain."""
    with pytest.raises(SpecError, match="introDuration"):
        validate_spec(spec({"type": "text", "items": [
            {"text": "T", "start": 0, "duration": 3, "anim": {"introDuration": 0.5}}]}))


# --------------------------------------------------------------------------
# the post-compile item keys. Everything here is refused before the compile
# because the CLI behind each key reports a bad value by exiting non-zero
# *after* the draft exists -- and nothing downstream re-reads it, so the draft
# opens looking exactly like one nobody asked to change.
# --------------------------------------------------------------------------

def video(**item):
    return {"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4, **item}]}


def text(**item):
    return {"type": "text", "items": [
        {"text": "TITLE", "start": 0, "duration": 3, **item}]}


def test_crop_takes_exactly_one_of_ratio_or_rect():
    validate_spec(spec(video(crop="9:16")))
    validate_spec(spec(video(crop={"rect": [0.1, 0.1, 0.8, 0.8]})))
    with pytest.raises(SpecError, match="exactly one of `ratio`"):
        validate_spec(spec(video(crop={"ratio": "9:16", "rect": [0, 0, 1, 1]})))
    with pytest.raises(SpecError, match="`crop` must be a non-empty object"):
        validate_spec(spec(video(crop={})))


def test_a_crop_rect_is_fractions_of_the_frame_not_pixels():
    """The rect is 0-1 fractions of the source frame. Pixels are the natural guess
    and are written verbatim, landing far outside the frame."""
    with pytest.raises(SpecError, match="0–1 fractions of the source frame"):
        validate_spec(spec(video(crop={"rect": [0, 0, 1920, 1080]})))
    with pytest.raises(SpecError, match=r"crop `rect` is \[x, y, w, h\]"):
        validate_spec(spec(video(crop={"rect": [0, 0, 1]})))


def test_text_ranges_are_character_spans_that_have_to_cover_something():
    validate_spec(spec(text(textRanges=[{"start": 0, "end": 3, "font_color": "#FFD700"}])))
    with pytest.raises(SpecError, match="needs `end`"):
        validate_spec(spec(text(textRanges=[{"start": 0}])))
    with pytest.raises(SpecError, match="covers nothing"):
        validate_spec(spec(text(textRanges=[{"start": 3, "end": 3}])))
    with pytest.raises(SpecError, match="unknown key 'colour'"):
        validate_spec(spec(text(textRanges=[{"start": 0, "end": 3, "colour": "#FFF"}])))


def test_text_ranges_on_a_clip_are_refused():
    with pytest.raises(SpecError, match="`textRanges` only applies to text items"):
        validate_spec(spec(video(textRanges=[{"start": 0, "end": 3}])))


def test_opacity_is_refused_because_capcut_composites_it_opaque():
    """The subtlest of the discarded keys, and the reason this class needs the
    app and not the file as its standard: `clip.alpha` is written correctly, it
    survives CapCut's own save, and the Blend panel shows the reduced value --
    then the export composites 100% of the top clip. Measured on an exported
    frame against a reconstructed base: over a base of (161,61,60), a 0.4 blend
    cannot put red below 97 and the export read 70 [proven]. An opacity edit
    made by hand in the app writes a byte-identical segment, so there is no
    missing key to find."""
    with pytest.raises(SpecError, match="`opacity` is unusable"):
        validate_spec(spec(video(opacity=0.5)))


def test_rotation_is_a_number_of_degrees():
    """`rotation` is compile's own item field, so there is no CLI call to fail on
    it: a bad value is simply written. This is the only place it can be caught.
    Unlike `opacity` beside it, the app honours it -- confirmed on screen."""
    validate_spec(spec(video(rotation=90)))
    with pytest.raises(SpecError, match="`rotation` must be a number"):
        validate_spec(spec(video(rotation="90deg")))


# --------------------------------------------------------------------------
# the tracks compile does not build
# --------------------------------------------------------------------------

STICKER = {"type": "sticker", "items": [
    {"resourceId": "7137268628230638087", "start": 0, "duration": 2}]}
SFX = {"type": "sfx", "items": [{"slug": "big-house", "start": 0, "duration": 2}]}


def test_a_spec_of_only_built_after_tracks_leaves_compile_nothing_to_build():
    """sticker is stripped out before compile sees the spec, so a spec made of
    nothing else hands compile an empty track list."""
    with pytest.raises(SpecError, match="at least one video, audio or text track"):
        validate_spec(spec(STICKER))


def test_a_sticker_track_is_refused_because_the_asset_is_never_local():
    """The track survives the app's save intact -- and draws nothing, because the
    material's `path` is `##_material_placeholder_<uuid>_##` and there is no file
    behind the id [proven]. `bubble`'s boundary, reached from the other side."""
    with pytest.raises(SpecError, match="a `sticker` track is unusable"):
        validate_spec(spec(VIDEO, STICKER))


def test_an_unknown_track_type_is_named_with_the_ones_that_exist():
    with pytest.raises(SpecError, match="unknown track type 'subtitle'"):
        validate_spec(spec(VIDEO, {"type": "subtitle", "items": []}))


# --- keys the app throws away -------------------------------------------------
#
# These reach the draft, exit 0, and lint clean. `mix` is then discarded the
# first time CapCut opens and saves the project; `cover` writes a key the project
# list never reads, so the thumbnail stays black. A build that accepts them
# reports success for work the user will not get, which is the one thing spec
# validation exists to prevent.
#
# `chroma` sat here too, until the material CapCut writes for its own was
# captured by hand and compared: the CLI's version was the right material in the
# right place under the wrong field names, and `repair_chroma_materials` now
# rewrites it. Refusal is for what cannot be reached, not for what is merely
# broken on the way.


def test_mix_is_accepted_now_that_the_material_is_built_where_capcut_keeps_it():
    """It was refused while `capcut mix-mode`'s string field was all there was.
    `repair_mix_modes` builds the material CapCut actually reads, out of the
    manifest in the app's own bundle."""
    validate_spec(spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4,
         "mix": "screen"}]}))


def test_a_blend_mode_capcut_ships_no_shader_for_is_refused():
    """`capcut mix-mode` takes all twelve; the app's MixMode.json holds ten, and
    neither difference nor exclusion is among them. Writing one would name a
    resource that is not there -- the failure `sticker` dies of, caught early."""
    with pytest.raises(SpecError, match="ships no shader"):
        validate_spec(spec({"type": "video", "items": [
            {"path": "/footage/a.mp4", "start": 0, "duration": 4,
             "mix": "difference"}]}))


def test_chroma_is_accepted_now_that_the_material_is_repaired():
    """It was refused while the app discarded it. `repair_chroma_materials`
    rewrites what the CLI writes into CapCut's own shape, verified field for
    field against a chroma key applied by hand in the app."""
    validate_spec(spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4,
         "chroma": {"color": "#00FF00", "intensity": 0.6}}]}))


def test_a_cover_is_accepted_now_that_eyecut_writes_the_image_itself():
    """Refused while the only route was `capcut add-cover`, which writes a key
    the project list does not read. The list reads `draft_cover.jpg` beside the
    draft, so eyecut renders it and the CLI is not involved."""
    s = spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4}]})
    s["cover"] = {"path": "/tmp/cover.png", "time": 1}
    validate_spec(s)


def test_a_relative_cover_path_is_refused():
    """Same reason every other path in a spec must be absolute: the draft is
    written somewhere the caller never named, and nothing resolves it for them."""
    s = spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4}]})
    s["cover"] = {"path": "cover.png"}
    with pytest.raises(SpecError, match="must be absolute"):
        validate_spec(s)


def test_an_unknown_cover_key_is_named():
    s = spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4}]})
    s["cover"] = {"path": "/tmp/cover.png", "frame": 12}
    with pytest.raises(SpecError, match="unknown key 'frame'"):
        validate_spec(s)


def test_the_refusal_says_why_and_not_merely_that_it_is_unknown():
    """A bare 'unknown key' would send the reader looking for a typo."""
    with pytest.raises(SpecError) as excinfo:
        validate_spec(spec({"type": "video", "items": [
            {"path": "/footage/a.mp4", "start": 0, "duration": 4,
             "bgBlur": 3}]}))
    assert "black" in str(excinfo.value).lower()


def test_keys_written_in_the_same_pass_still_pass():
    """The refusal is specific to the three, not a retreat from the coverage."""
    validate_spec(spec({"type": "video", "items": [
        {"path": "/footage/a.mp4", "start": 0, "duration": 4,
         "crop": {"ratio": "9:16"}, "rotation": 15}]}))


def test_an_sfx_track_is_refused_because_the_effect_has_no_audio_file():
    with pytest.raises(SpecError, match="sfx"):
        validate_spec(spec(VIDEO, SFX))


def test_the_sfx_refusal_names_the_workaround():
    """An `audio` track pointed at a local file does what `sfx` was for, and is
    already proven. Without that in the message the reader has no way forward."""
    with pytest.raises(SpecError) as excinfo:
        validate_spec(spec(VIDEO, SFX))
    assert "audio" in str(excinfo.value)


def test_bg_blur_is_refused_because_it_renders_black():
    """It survives a CapCut save, unlike mix and chroma -- and still shows nothing
    but black beside a cropped clip."""
    with pytest.raises(SpecError, match="bgBlur"):
        validate_spec(spec(video(bgBlur=3)))


def test_a_bubble_is_refused_because_the_shape_is_a_store_asset():
    with pytest.raises(SpecError, match="bubble"):
        validate_spec(spec(VIDEO, text(bubble="cloud")))


def test_the_bubble_refusal_says_it_is_the_store_and_not_a_bad_slug():
    """A reader who thinks the slug is wrong will go hunting the catalogue for a
    better one. There isn't one -- no bubble slug can work."""
    with pytest.raises(SpecError) as excinfo:
        validate_spec(spec(VIDEO, text(bubble="cloud")))
    assert "store" in str(excinfo.value).lower()


def test_text_styling_still_passes_beside_the_refused_bubble():
    validate_spec(spec(VIDEO, text(textStyle={"borderWidth": 0.08,
                                              "shadow": True}),
                       ))
