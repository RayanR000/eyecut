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
