"""`write_draft` delegates the timeline to capcut-cli, then does the two things
capcut-cli does not: register the media and mirror the duration into the meta file.

capcut-cli is not importable, so it is faked -- but the fake writes the same files
the real one does, and every assertion here is on those files, never on the fake.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from eyecut.draft import (AFTER_HOOKS, CHECK_FLAG_BLEND, CHECK_FLAG_CHROMA, CHROMA_PATH,
                          CompileError, MIX_MODE_NAME_IDS, _mix_mode_catalogue,
                          repair_chroma_materials, repair_mix_modes,
                          repair_sfx_materials, write_draft)
from eyecut.media import MediaProbe


@pytest.fixture(autouse=True)
def capcut_not_running(monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: False)


SPEC = {"tracks": [{"segments": [{"material": "/footage/a.mp4"}]}]}
PROBES = [MediaProbe(path=Path("/footage/a.mp4"), metetype="video",
                     width=1920, height=1080, duration_us=405_333_000)]


def fake_capcut(duration_us=9_500_000, returncode=0, stderr=""):
    """Stands in for the CLI: `compile` writes the draft pair it writes, with
    tm_duration left at 0 -- the omission write_draft exists to correct. `register`
    is a no-op here; only the integration test below exercises the real one."""
    def run(argv, cwd):
        if argv[1] != "compile":
            return 0, ""
        if returncode:
            return returncode, stderr
        out = Path(argv[argv.index("--out") + 1])
        (out / "assets" / "video").mkdir(parents=True, exist_ok=True)
        # compile copies each source into the draft and points the timeline at the
        # copy through this placeholder -- the real CLI's format, verbatim.
        copied = out / "assets" / "video" / "a.mp4"
        copied.write_bytes(b"")
        (out / "draft_info.json").write_text(json.dumps(
            {"duration": duration_us,
             "materials": {"videos": [{"path": "##_draftpath_placeholder_0E685133-18CE-"
                                               "45ED-8CB8-2904A212EC80_##/assets/video/a.mp4"}]}}))
        (out / "draft_meta_info.json").write_text(json.dumps(
            {"draft_name": out.name, "tm_duration": 0, "draft_materials": []}))
        return 0, ""
    return run


@pytest.fixture(autouse=True)
def fake_probe(monkeypatch):
    """The fake compile writes empty files; only the registered path is under test."""
    monkeypatch.setattr("eyecut.draft.probe", lambda p: MediaProbe(
        path=Path(p), metetype="video", width=1920, height=1080, duration_us=9_500_000))


def test_compiled_draft_carries_the_timeline_duration_in_its_meta_file(tmp_path, drafts_dir):
    draft = write_draft(SPEC, drafts_dir / "proj", PROBES, runner=fake_capcut())

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    assert meta["tm_duration"] == 9_500_000  # 0 is what lists the draft as 00:00


def test_every_source_in_the_spec_is_registered(tmp_path, drafts_dir):
    draft = write_draft(SPEC, drafts_dir / "proj", PROBES, runner=fake_capcut())

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    registered = [e["file_Path"] for g in meta["draft_materials"] for e in g["value"]]
    # The copy inside the draft, not the original: registering the original is what
    # leaves CapCut showing "Media lost" and a relink dialog [proven, probes D/E].
    assert registered == ["./assets/video/a.mp4"]


def test_a_failed_compile_raises_with_the_cli_output(tmp_path, drafts_dir):
    with pytest.raises(CompileError, match="bad segment"):
        write_draft(SPEC, drafts_dir / "proj", PROBES,
                    runner=fake_capcut(returncode=1, stderr="bad segment"))


def test_refuses_to_compile_while_capcut_is_running(tmp_path, drafts_dir, monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: True)
    with pytest.raises(RuntimeError, match="CapCut is running"):
        write_draft(SPEC, drafts_dir / "proj", PROBES, runner=fake_capcut())
    assert not (drafts_dir / "proj").exists()  # nothing half-written


# --- against the real capcut-cli ------------------------------------------
# The fake above pins our logic; only a real run can pin the CLI's argv and the
# division of labour between us and it.

capcut_cli = pytest.mark.skipif(shutil.which("capcut") is None,
                                reason="capcut-cli not installed")


@capcut_cli
def test_a_real_compile_produces_a_draft_capcut_can_open(tmp_path, drafts_dir, monkeypatch):
    """Everything `capcut compile` alone leaves wrong, against the real CLI:
    the 6.5.0 template CapCut refuses outright, no draft_materials (the relink
    prompt), and tm_duration 0 (lists as 00:00). The store entry is `capcut
    register`'s job and is delegated to it."""
    monkeypatch.undo()   # the real CLI writes real files; probe them for real
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(source)],
                   capture_output=True, check=True)
    spec = {"name": "eyecut-int", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 5}]}]}
    probes = [MediaProbe(path=source, metetype="video",
                         width=320, height=240, duration_us=6_000_000)]

    draft = write_draft(spec, drafts_dir / "proj", probes)

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    registered = [e["file_Path"] for g in meta["draft_materials"] for e in g["value"]]
    assert registered == ["./assets/video/a.mp4"], "must register the copy, not the original"
    assert (draft.path / "assets" / "video" / "a.mp4").is_file()
    assert meta["tm_duration"] == draft.duration_us == 5_000_000
    store = json.loads((drafts_dir / "root_meta_info.json").read_text())
    assert store["all_draft_store"], "draft is not listed in the store"

    # the whole reason for the template: a 6.5.0 draft will not open at all
    built = json.loads((draft.path / "draft_info.json").read_text())
    assert built["platform"]["app_version"] != "6.5.0"


@capcut_cli
def test_the_compiled_timeline_reaches_the_folder_capcut_reads(tmp_path, drafts_dir, monkeypatch):
    """A CapCut 9.x draft keeps its timeline under Timelines/<main_timeline_id>/ as
    well as at the root, and reads the former. Compile writes only the root, so a
    draft compiled against a real template opened with an empty timeline [proven]."""
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-tl", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 5}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [], )

    index = json.loads((draft.path / "Timelines" / "project.json").read_text())
    active = draft.path / "Timelines" / index["main_timeline_id"] / "draft_info.json"
    assert json.loads(active.read_text())["duration"] == 5_000_000
    assert (draft.path / "template-2.tmp").read_text() == \
        (draft.path / "draft_info.json").read_text()


@capcut_cli
def test_the_draft_gets_a_timeline_identity_of_its_own(tmp_path, drafts_dir, monkeypatch):
    """draft_info.json's id must be the main timeline's id and its folder name.
    Compile leaves the template's, which resolves to nothing: the draft lists,
    and clicking it does nothing at all [proven]."""
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-id", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 5}]}]}
    template_id = json.loads(
        (drafts_dir / "empty_project" / "Timelines" / "project.json").read_text())["main_timeline_id"]

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    index = json.loads((draft.path / "Timelines" / "project.json").read_text())
    assert built["id"] == index["main_timeline_id"] == index["id"]
    assert [t["id"] for t in index["timelines"]] == [built["id"]]
    assert (draft.path / "Timelines" / built["id"] / "draft_info.json").is_file()
    assert built["id"] != template_id, "must not reuse the template's timeline id"


@capcut_cli
def test_the_source_in_point_is_sourceStart_not_start(tmp_path, drafts_dir, monkeypatch):
    """`start` is where the clip lands on the TIMELINE; the in-point into the source
    is `sourceStart` (camelCase, compile.js:485). Confusing them is silent and
    expensive: three 4s shots taken from 120s/300s/610s of a film compiled to a
    615-second draft with two long gaps, because each `start` was read as a
    timeline position. `--check` accepts an unknown key without complaint, so
    nothing catches it until you watch the result.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-in", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 2, "sourceStart": 5},
        {"path": str(source), "start": 2, "duration": 3, "sourceStart": 12}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    segments = [s for t in json.loads((draft.path / "draft_info.json").read_text())["tracks"]
                for s in t["segments"]]
    assert [s["source_timerange"] for s in segments] == [
        {"start": 5_000_000, "duration": 2_000_000},
        {"start": 12_000_000, "duration": 3_000_000}]
    assert [s["target_timerange"] for s in segments] == [
        {"start": 0, "duration": 2_000_000},
        {"start": 2_000_000, "duration": 3_000_000}]
    assert draft.duration_us == 5_000_000, "timeline is the sum of the clips, not the last in-point"


@capcut_cli
def test_audio_text_transitions_and_keyframes_all_reach_the_draft(tmp_path, drafts_dir,
                                                                  monkeypatch):
    """Most of CapCut is reachable through the spec compile already takes; what was
    missing was anyone checking. Audio and text tracks, a transition, an animated
    keyframe pair and an audio fade in one draft, asserted on disk -- especially
    the keyframe's real values, since the wrong spelling writes nulls that lint
    reports as clean [proven].
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    tone = tmp_path / "bed.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=8",
                    str(tone)], capture_output=True, check=True)
    spec = {"name": "eyecut-rich", "tracks": [
        {"type": "video", "items": [
            {"path": str(source), "start": 0, "duration": 4, "sourceStart": 2, "ref": "shot0"},
            {"path": str(source), "start": 4, "duration": 4, "sourceStart": 12, "ref": "shot1"}]},
        {"type": "audio", "items": [
            {"path": str(tone), "start": 0, "duration": 8, "volume": 0.25, "ref": "bed"}]},
        {"type": "text", "items": [
            {"text": "TITLE", "start": 0, "duration": 3, "fontSize": 24, "color": "#FFD700"}]}],
        "operations": [
            {"op": "transition", "target": "shot0", "slug": "dissolve", "duration": 0.5},
            {"op": "keyframe", "target": "shot1", "property": "uniform_scale",
             "time": 0.0, "value": 1.0},
            {"op": "keyframe", "target": "shot1", "property": "uniform_scale",
             "time": 4.0, "value": 1.15, "easing": "ease-in-out"},
            {"op": "audio-fade", "target": "bed", "fadeIn": 0.2, "fadeOut": 0.8}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    assert {t["type"] for t in built["tracks"]} >= {"video", "audio", "text"}
    assert len(built["materials"]["transitions"]) == 1
    assert len(built["materials"]["audio_fades"]) == 1

    keyframes = [k for t in built["tracks"] for s in t["segments"]
                 for k in (s.get("common_keyframes") or [])]
    assert len(keyframes) == 1, "both points belong to one property's keyframe list"
    points = [(p["time_offset"], p["values"]) for p in keyframes[0]["keyframe_list"]]
    assert points == [(0, [1.0]), (4_000_000, [1.15])], "a from/to spec writes nulls here"

    # the audio source is registered the same way the video is -- no relink prompt
    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    registered = {e["file_Path"] for g in meta["draft_materials"] for e in g["value"]}
    assert registered == {"./assets/video/a.mp4", "./assets/audio/bed.wav"}


@capcut_cli
def test_filters_and_effects_get_tracks_of_their_own(tmp_path, drafts_dir, monkeypatch):
    """`filter` and `effect` cover a span of timeline, and compile gives each its
    own track. The slugs resolve to real CapCut resource ids, so this is the 345-
    effect catalogue reachable by name -- picking one is easy, and only naming the
    one in someone else's video is guesswork.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=10",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-fx", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 8, "ref": "a"}]}],
        "operations": [
            {"op": "filter", "slug": "vintage", "start": 0, "duration": 8, "intensity": 0.6},
            {"op": "effect", "slug": "blur", "start": 0, "duration": 2}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    assert {t["type"] for t in built["tracks"]} == {"video", "filter", "effect"}
    applied = {m["name"]: m["value"] for m in built["materials"]["video_effects"]}
    assert applied == {"Vintage": 0.6, "Blur": 1}
    assert all(m.get("effect_id") or m.get("resource_id")
               for m in built["materials"]["video_effects"]), "slugs must resolve to ids"
    assert draft.duration_us == 8_000_000


@capcut_cli
def test_a_speed_change_reaches_the_material_the_app_actually_reads(tmp_path, drafts_dir,
                                                                    monkeypatch):
    """compile writes `segment.speed` and leaves the segment's speed material at 1.
    CapCut reads the material, so a clip asked for 2x plays at 1x while its trim is
    still cut for 2x -- an edit that is wrong in a way that looks like bad footage.
    `capcut lint` catches it (`speed-material-mismatch`) but cannot fix it, so
    write_draft re-syncs each one [proven].
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-speed", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 2, "sourceStart": 0, "speed": 2.0},
        {"path": str(source), "start": 2, "duration": 4, "sourceStart": 8, "speed": 0.5},
        {"path": str(source), "start": 6, "duration": 2, "sourceStart": 14}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    speeds = {m["id"]: m["speed"] for m in built["materials"]["speeds"]}
    segments = [s for t in built["tracks"] for s in t["segments"]]
    for segment in segments:
        material = next(speeds[r] for r in segment["extra_material_refs"] if r in speeds)
        assert material == segment["speed"], "the material is what CapCut plays"
    assert [s["speed"] for s in segments] == [2, 0.5, 1]

    # the re-sync must not disturb the trim: 2x consumes twice the source
    assert [(s["source_timerange"]["duration"], s["target_timerange"]["duration"])
            for s in segments] == [(4_000_000, 2_000_000), (2_000_000, 4_000_000),
                                   (2_000_000, 2_000_000)]
    assert draft.duration_us == 8_000_000


@capcut_cli
def test_a_mask_in_the_spec_reaches_the_segment_it_names(tmp_path, drafts_dir, monkeypatch):
    """`mask` is the one key eyecut adds to compile's vocabulary, because compile
    has no mask operation: it is applied afterwards with `capcut mask`, matched to
    the segment by position. The second clip is masked and the first is not, so a
    mask applied to the wrong segment fails this test.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-mask", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 4, "sourceStart": 0},
        {"path": str(source), "start": 4, "duration": 4, "sourceStart": 8,
         "mask": {"slug": "circle", "size": 0.6, "invert": True}}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    assert draft.warnings == []
    built = json.loads((draft.path / "draft_info.json").read_text())
    masks = built["materials"]["common_mask"]
    assert [m["name"] for m in masks] == ["Circle"]
    assert masks[0]["config"]["invert"] is True

    segments = [s for t in built["tracks"] for s in t["segments"]]
    masked = [s for s in segments if masks[0]["id"] in s.get("extra_material_refs", [])]
    assert len(masked) == 1
    assert masked[0]["target_timerange"]["start"] == 4_000_000, "the second clip, not the first"

    # `capcut mask` leaves this empty; a CapCut-authored mask carries a UUID, and
    # the shape was captured by hand from the app to find that out.
    assert masks[0]["constant_material_id"], "must match what CapCut writes for its own"


@capcut_cli
def test_a_named_second_video_track_becomes_an_overlay(tmp_path, drafts_dir, monkeypatch):
    """Two video tracks stay two only if each carries a distinct `name` -- compile
    keys the built track on (type, name). With names, the second track is an
    overlay and `scale`/`x`/`y` place it; without them both clips land on one
    track on top of each other, which lints clean [proven].
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-pip", "tracks": [
        {"type": "video", "name": "main", "items": [
            {"path": str(source), "start": 0, "duration": 6, "sourceStart": 0}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": 1, "duration": 3, "sourceStart": 10,
             "scale": 0.4, "x": 0.3, "y": 0.3}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    video = [t for t in built["tracks"] if t["type"] == "video"]
    assert [t["name"] for t in video] == ["main", "overlay"]
    assert [len(t["segments"]) for t in video] == [1, 1]

    placed = video[1]["segments"][0]["clip"]
    assert placed["scale"] == {"x": 0.4, "y": 0.4}
    assert placed["transform"] == {"x": 0.3, "y": 0.3}
    assert draft.duration_us == 6_000_000


@capcut_cli
def test_captions_come_from_an_srt_and_land_on_their_own_track(tmp_path, drafts_dir,
                                                               monkeypatch):
    """`captions` reads an SRT and writes one text segment per cue, flagged
    `sub_type: 1` so CapCut treats them as captions rather than plain text."""
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=10",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    srt = tmp_path / "cues.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nFirst line\n\n"
                   "2\n00:00:02,500 --> 00:00:04,000\nSecond line\n")
    spec = {"name": "eyecut-caps", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 6}]}],
        "operations": [{"op": "captions", "path": str(srt)}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    built = json.loads((draft.path / "draft_info.json").read_text())
    captions = next(t for t in built["tracks"] if t["type"] == "text")
    assert captions["name"] == "captions"
    assert [s["target_timerange"] for s in captions["segments"]] == [
        {"start": 500_000, "duration": 1_500_000},
        {"start": 2_500_000, "duration": 1_500_000}]
    texts = built["materials"]["texts"]
    assert [m["sub_type"] for m in texts] == [1, 1], "captions, not plain text"
    assert [json.loads(m["content"])["text"] for m in texts] == ["First line", "Second line"]


@capcut_cli
def test_a_saved_template_carries_its_style_and_takes_new_text(tmp_path, drafts_dir,
                                                               monkeypatch):
    """The reuse loop: style a title once, apply it many times. `capcut
    save-template` captures a segment and its materials; the `template` op clones
    them with fresh ids and swaps the text, recomputing the style's character range
    so the styling still covers the new string [proven].
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=10",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)

    # 1. a draft holding the styled title
    styled = write_draft({"name": "eyecut-tpl-src", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 5}]},
        {"type": "text", "items": [{"text": "MY TITLE", "start": 0, "duration": 3,
                                    "fontSize": 30, "color": "#FFD700"}]}]},
        drafts_dir / "src", [])
    text_id = json.loads(subprocess.run(["capcut", "texts", str(styled.path)],
                                        capture_output=True, text=True).stdout)[0]["id"]
    template = tmp_path / "gold-title.json"
    subprocess.run(["capcut", "save-template", str(styled.path), text_id, "gold-title",
                    "--out", str(template)], capture_output=True, check=True)

    # 2. a different draft that reuses it twice, with different words
    reused = write_draft({"name": "eyecut-tpl", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 8}]}],
        "operations": [
            {"op": "template", "path": str(template), "start": 0, "duration": 3,
             "text": "STOP USING THE DEFAULT FONT"},
            {"op": "template", "path": str(template), "start": 4, "duration": 3,
             "text": "SECOND CARD"}]},
        drafts_dir / "reuse", [])

    built = json.loads((reused.path / "draft_info.json").read_text())
    contents = [json.loads(m["content"]) for m in built["materials"]["texts"]]
    assert [c["text"] for c in contents] == ["STOP USING THE DEFAULT FONT", "SECOND CARD"]
    for content, text in zip(contents, ["STOP USING THE DEFAULT FONT", "SECOND CARD"]):
        style = content["styles"][0]
        assert style["size"] == 30, "the style comes from the template"
        assert style["fill"]["content"]["solid"]["color"][0] == 1, "gold, from the template"
        assert style["range"] == [0, len(text)], "the range follows the new text"

    captions = next(t for t in built["tracks"] if t["type"] == "text")
    assert [s["target_timerange"]["start"] for s in captions["segments"]] == [0, 4_000_000]


def test_a_big_file_in_the_template_does_not_land_in_every_draft(tmp_path):
    """Compile copies the template folder wholesale. `clear_inherited_media`
    un-registers what came with it but leaves the bytes, so a large source in the
    template put an unreferenced copy inside every draft compiled against it
    [proven -- five sukuna drafts holding 5.5 GB of a test movie none referenced,
    found only because each draft was 1.1 GB].
    """
    from eyecut.draft import prune_inherited_assets

    project = tmp_path / "proj"
    (project / "assets" / "video").mkdir(parents=True)
    used = project / "assets" / "video" / "used.mp4"
    inherited = project / "assets" / "video" / "Sintel.2010.1080p.mkv"
    caption = project / "assets" / "captions.srt"
    for f in (used, inherited, caption):
        f.write_bytes(b"x" * 16)
    (project / "draft_info.json").write_text(json.dumps(
        {"materials": {"videos": [{"path": str(used)}]},
         "tracks": [{"type": "text", "segments": [{"srt": "./assets/captions.srt"}]}]}))

    removed = prune_inherited_assets(project, [used])

    assert not inherited.exists(), "the template's media is gone"
    assert used.exists(), "the media this timeline uses survives"
    assert caption.exists(), "anything draft_info.json names survives, however reached"
    assert len(removed) == 1 and "Sintel" in removed[0], removed


@capcut_cli
def test_a_text_look_in_the_spec_reaches_the_caption_it_names(tmp_path, drafts_dir, monkeypatch):
    """The `text-style` OPERATION crashes capcut-cli 0.21.1 ("Cannot read
    properties of undefined (reading 'alpha')"), so eyecut refuses it. The
    standalone `capcut text-style` command works on the same styling, so the look
    is an item key applied after the compile, matched to the segment by position
    the way `mask` is. The second caption is styled and the first is not, so a
    look applied to the wrong segment fails this test.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-textstyle", "tracks": [
        {"type": "video", "items": [
            {"path": str(source), "start": 0, "duration": 8, "sourceStart": 0}]},
        {"type": "text", "items": [
            {"text": "PLAIN", "start": 0, "duration": 4},
            {"text": "STYLED", "start": 4, "duration": 4,
             "textStyle": {"borderWidth": 0.08, "borderColor": "#000000",
                           "shadow": True, "shadowAlpha": 0.6}}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    assert draft.warnings == []
    built = json.loads((draft.path / "draft_info.json").read_text())
    texts = {t["id"]: t for t in built["materials"]["texts"]}
    captions = next(t for t in built["tracks"] if t["type"] == "text")
    looks = [texts[s["material_id"]] for s in captions["segments"]]

    assert [json.loads(t["content"])["text"] for t in looks] == ["PLAIN", "STYLED"]
    assert not looks[0].get("has_shadow"), "the first caption keeps the default look"
    assert looks[1]["has_shadow"] is True
    assert looks[1]["shadow_alpha"] == 0.6
    assert looks[1]["border_width"] == 0.08
    assert looks[1]["border_color"] == "#000000"


@capcut_cli
def test_an_animation_in_the_spec_reaches_the_segment_it_names(tmp_path, drafts_dir, monkeypatch):
    """compile has no animation operation, so `anim` is applied afterwards --
    `capcut text-anim` on a caption, `capcut image-anim` on a clip -- matched to
    the segment by position. The second clip and the caption are animated and the
    first clip is not, so an animation on the wrong segment fails this test.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-anim", "tracks": [
        {"type": "video", "items": [
            {"path": str(source), "start": 0, "duration": 4, "sourceStart": 0},
            {"path": str(source), "start": 4, "duration": 4, "sourceStart": 8,
             "anim": {"intro": "fade-in", "introDuration": 0.5}}]},
        {"type": "text", "items": [
            {"text": "TITLE", "start": 0, "duration": 3,
             "anim": {"intro": "typewriter", "introDuration": 0.6}}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    assert draft.warnings == []
    built = json.loads((draft.path / "draft_info.json").read_text())
    animations = {a["id"]: a for a in built["materials"]["material_animations"]}
    assert sorted(anim["animations"][0]["name"] for anim in animations.values()) == \
        ["Fade In", "Typewriter"]

    segments = [s for t in built["tracks"] for s in t["segments"]]
    animated = {anim["animations"][0]["name"]:
                next(s for s in segments if anim["id"] in s.get("extra_material_refs", []))
                for anim in animations.values()}
    assert animated["Fade In"]["target_timerange"]["start"] == 4_000_000, \
        "the second clip, not the first"
    assert animated["Typewriter"]["target_timerange"]["duration"] == 3_000_000, "the caption"
    assert [anim["animations"][0]["duration"] for anim in animations.values()
            if anim["animations"][0]["name"] == "Typewriter"] == [600_000]


@capcut_cli
def test_a_mask_on_the_base_track_does_not_land_on_the_overlay(tmp_path, drafts_dir,
                                                               monkeypatch):
    """Two video tracks are how an overlay is built, and the matcher has to tell
    them apart.

    Keying segments on (type, position) alone collapses every video track onto one
    set of positions, so the LAST track of a type wins every key: a mask meant for
    the base clip is applied to the overlay instead -- silently, because the counts
    still agree and the mismatch guard never fires. (Masking the overlay hides the
    bug, since the overlay is the track that overwrites.) The overlay here carries
    no mask, so a mask that lands on it fails this test.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-overlay-mask", "tracks": [
        {"type": "video", "name": "base", "items": [
            {"path": str(source), "start": 0, "duration": 8, "sourceStart": 0,
             "mask": {"slug": "circle", "size": 0.6}}]},
        {"type": "video", "name": "overlay", "items": [
            {"path": str(source), "start": 2, "duration": 4, "sourceStart": 8,
             "scale": 0.4}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    assert draft.warnings == []
    built = json.loads((draft.path / "draft_info.json").read_text())
    masked = {m["id"] for m in built["materials"]["common_mask"]}
    assert len(masked) == 1, "one mask asked for, one mask written"

    tracks = {t.get("name"): t for t in built["tracks"] if t["type"] == "video"}
    base, overlay = tracks["base"]["segments"], tracks["overlay"]["segments"]
    assert set(base[0]["extra_material_refs"]) & masked, \
        "the base clip is the one that asked for a mask"
    assert not (set(overlay[0]["extra_material_refs"]) & masked), \
        "the overlay clip asked for no mask"


@capcut_cli
def test_the_new_item_keys_reach_the_segments_they_name(tmp_path, drafts_dir, monkeypatch):
    """Crop, applied against the real CLI.

    `mix`, `chroma` and `bgBlur` were exercised here too until validation started
    refusing them. Each reached the draft exactly as asserted, and the app then
    discarded it or drew nothing -- so a green assertion here was evidence of
    nothing, which is the whole reason these are refused rather than documented.

    Each is a separate capcut-cli command against a segment id, so what this pins
    is the argv shape: a wrong flag name exits non-zero *after* the draft exists,
    and nothing downstream re-reads it. The first clip carries every key and the
    second carries none, so a key applied to the wrong segment fails here.
    """
    monkeypatch.undo()
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=20",
                    "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=True)
    spec = {"name": "eyecut-item-ops", "tracks": [{"type": "video", "items": [
        {"path": str(source), "start": 0, "duration": 4, "sourceStart": 0,
         "crop": {"ratio": "9:16"}},
        {"path": str(source), "start": 4, "duration": 4, "sourceStart": 8}]}]}

    draft = write_draft(spec, drafts_dir / "proj", [])

    assert draft.warnings == [], "every key applied cleanly"
    built = json.loads((draft.path / "draft_info.json").read_text())
    segments = [s for t in built["tracks"] if t["type"] == "video" for s in t["segments"]]
    decorated, plain = segments

    materials = {m["id"]: m for m in built["materials"]["videos"]}
    styled = materials[decorated["material_id"]]
    untouched = materials[plain["material_id"]]

    # 9:16 out of a 4:3 source keeps the middle 42% of the width, full height
    crop = styled["crop"]
    assert crop["upper_left_y"] == 0 and crop["lower_left_y"] == 1
    assert round(crop["lower_right_x"] - crop["upper_left_x"], 3) == 0.422
    assert untouched["crop"]["lower_right_x"] == 1, "the second clip is uncropped"


def test_an_sfx_segment_is_repaired_onto_an_audios_material():
    """`capcut add-sfx` writes the sound effect into `materials.audio_effects` and
    points the segment's `material_id` at it. CapCut resolves an audio segment
    through `materials.audios`; a segment whose material is not there has no
    material at all, so **CapCut deletes the whole track on save** [proven: the
    verify draft came back with only its video track and both effect entries
    gone].

    `audio_effects` is decoration applied *to* an audio material, not a substitute
    for one -- the same shape as the speed bug, where the app reads the material
    and not the segment.
    """
    data = {"tracks": [
        {"type": "video", "name": "video", "segments": [
            {"id": "v0", "material_id": "vm", "extra_material_refs": []}]},
        {"type": "audio", "name": "hits", "segments": [
            {"id": "a0", "material_id": "sfx-mat", "volume": 0.5,
             "target_timerange": {"start": 1_000_000, "duration": 2_000_000},
             "source_timerange": {"start": 0, "duration": 2_000_000},
             "extra_material_refs": [], "render_index": 0}]}],
        "materials": {"videos": [{"id": "vm"}], "audios": [],
                      "audio_effects": [
                          {"id": "sfx-mat", "type": "sound_effect",
                           "name": "Big House", "effect_id": "EFF",
                           "resource_id": "RES", "md5": "abc", "path": ""}]}}

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "draft_info.json").write_text(json.dumps(data))

        repaired = repair_sfx_materials(project)

        assert repaired == 1
        out = json.loads((project / "draft_info.json").read_text())
        audios = out["materials"]["audios"]
        assert len(audios) == 1, "the segment now has a material CapCut will find"
        material = audios[0]
        assert material["type"] == "sound_effect"
        # the catalogue identity has to survive: it is how CapCut resolves the
        # store resource, and it is all `add-sfx` was given
        assert (material["effect_id"], material["resource_id"]) == ("EFF", "RES")
        assert material["name"] == "Big House"
        assert material["duration"] == 2_000_000, "taken from the segment"

        segment = out["tracks"][1]["segments"][0]
        assert segment["material_id"] == material["id"]
        assert segment["material_id"] != "sfx-mat"

        # the effect entry stays, moved to where an effect belongs
        assert [m["id"] for m in out["materials"]["audio_effects"]] == ["sfx-mat"]
        assert "sfx-mat" in segment["extra_material_refs"]

        # an audio segment CapCut authors carries these four, and compile's own
        # audio segments carry them too -- without them the segment is malformed
        kinds = {m["type"] for name in ("speeds", "placeholder_infos",
                                        "sound_channel_mappings", "vocal_separations")
                 for m in out["materials"][name]}
        assert kinds == {"speed", "placeholder_info", "none", "vocal_separation"}
        for name in ("speeds", "placeholder_infos", "sound_channel_mappings",
                     "vocal_separations"):
            assert out["materials"][name][0]["id"] in segment["extra_material_refs"]
        assert segment["render_index"] == 11000, "compile's own audio render index"

        assert segment["volume"] == 0.5, "the spec's volume is untouched"
        assert segment["target_timerange"] == {"start": 1_000_000,
                                               "duration": 2_000_000}


def test_repairing_a_draft_with_no_sfx_changes_nothing():
    data = {"tracks": [{"type": "video", "name": "video", "segments": [
                {"id": "v0", "material_id": "vm", "extra_material_refs": []}]}],
            "materials": {"videos": [{"id": "vm"}], "audios": []}}
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        before = json.dumps(data)
        (project / "draft_info.json").write_text(before)

        assert repair_sfx_materials(project) == 0
        assert (project / "draft_info.json").read_text() == before


def test_a_normal_audio_segment_is_left_alone():
    """compile's audio tracks already point at `materials.audios`. Only what
    `add-sfx` wrote is wrong, so only that is touched."""
    data = {"tracks": [{"type": "audio", "name": "bed", "segments": [
                {"id": "a0", "material_id": "music", "extra_material_refs": ["x"],
                 "target_timerange": {"start": 0, "duration": 4_000_000}}]}],
            "materials": {"audios": [{"id": "music", "type": "extract_music"}],
                          "audio_effects": []}}
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "draft_info.json").write_text(json.dumps(data))

        assert repair_sfx_materials(project) == 0
        out = json.loads((project / "draft_info.json").read_text())
        assert out["tracks"][0]["segments"][0]["material_id"] == "music"
        assert len(out["materials"]["audios"]) == 1


# `apply_track_ops` has no integration test any more: `sfx` is refused by
# `validate_spec` (CapCut rewrites the material to `type: "none"` and the clip is
# silent), and `sticker` needs a resource id harvested by hand from the app, which
# a test cannot obtain. `repair_sfx_materials` keeps its unit tests -- the repair
# is still what stops CapCut deleting the track outright, and is the half of the
# problem that was solvable from the files.


# --- chroma ------------------------------------------------------------------
#
# The shape on the right of each assertion is CapCut's own, captured by applying
# a chroma key by hand in the app and reading the material back out. `capcut
# chroma` creates the material and references it from the right segment, and
# then names every field something CapCut does not read.


def test_the_chroma_material_is_rewritten_into_the_shape_capcut_reads():
    """Six wrong fields and four missing ones, all of them silent: the CLI exits
    0 and lints clean, and the app renders no key at all [proven]."""
    data = {"tracks": [], "materials": {"chromas": [
        {"id": "c0", "type": "chromas", "color": "#0d1618",
         "intensity": 0.2, "shadow": 0, "path": ""}]}}
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "draft_info.json").write_text(json.dumps(data))

        assert repair_chroma_materials(project) == 1

        out = json.loads((project / "draft_info.json").read_text())
        chroma = out["materials"]["chromas"][0]
        assert chroma["type"] == "chroma", "the CLI writes the list name, not the type"
        assert chroma["intensity_value"] == 0.2, "CapCut never reads `intensity`"
        assert chroma["shadow_value"] == 0.0
        assert "intensity" not in chroma and "shadow" not in chroma
        assert chroma["color"] == "#0d1618ff", "CapCut stores the key colour RGBA"
        assert chroma["path"] == CHROMA_PATH, "the shader ships inside the app bundle"
        assert chroma["version"] == "v2"
        assert chroma["should_transfer_color"] is True
        assert chroma["edge_smooth_value"] == 0.0 and chroma["spill_value"] == 0.0


def test_a_chroma_already_in_capcuts_shape_is_left_alone():
    """The repair has to be safe to re-run over a draft CapCut has saved once,
    the way every other rebuild step is."""
    data = {"tracks": [], "materials": {"chromas": [
        {"id": "c0", "type": "chroma", "color": "#0d1618ff", "intensity_value": 0.2,
         "shadow_value": 0.0, "path": CHROMA_PATH, "resource_id": "",
         "should_transfer_color": True, "edge_smooth_value": 0.0,
         "spill_value": 0.0, "version": "v2"}]}}
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        before = json.dumps(data)
        (project / "draft_info.json").write_text(before)

        assert repair_chroma_materials(project) == 0
        assert (project / "draft_info.json").read_text() == before


def test_a_draft_with_no_chroma_is_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        before = json.dumps({"tracks": [], "materials": {"videos": [{"id": "v"}]}})
        (project / "draft_info.json").write_text(before)

        assert repair_chroma_materials(project) == 0
        assert (project / "draft_info.json").read_text() == before


# --- blend modes -------------------------------------------------------------
#
# `capcut mix-mode` writes a string field CapCut has no reader for. These check
# the move to where the app does read it, against the shape captured from a blend
# mode set by hand.


def _blend_draft(display="Screen"):
    return {"tracks": [{"type": "video", "name": "overlay", "segments": [
                {"id": "s0", "material_id": "vm", "extra_material_refs": ["speed"]}]}],
            "materials": {"videos": [{"id": "vm", "check_flag": 7,
                                      "mix_mode": display}]}}


def test_a_blend_mode_moves_to_the_material_capcut_reads():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "draft_info.json").write_text(json.dumps(_blend_draft()))

        assert repair_mix_modes(project) == 1

        out = json.loads((project / "draft_info.json").read_text())
        video = out["materials"]["videos"][0]
        assert "mix_mode" not in video, "the string field is what CapCut strips"
        assert video["check_flag"] == 7 | CHECK_FLAG_BLEND, "or the app ignores it"

        effects = out["materials"]["effects"]
        assert len(effects) == 1
        material = effects[0]
        assert material["type"] == "mix_mode"
        assert material["name"] == "Screen"
        # the identity CapCut resolves the shader by, from its own manifest
        assert material["effect_id"] == "871339"
        assert material["resource_id"] == "6758325170760323597"
        assert material["path"].endswith("d9c1d4ca7ab91df4f48d12b339f2da88")
        assert material["value"] == 1.0 and material["visible"] is True

        segment = out["tracks"][0]["segments"][0]
        assert material["id"] in segment["extra_material_refs"], \
            "a material nothing references is a material CapCut never reads"


def test_normal_writes_no_material_because_it_is_the_default():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "draft_info.json").write_text(json.dumps(_blend_draft("Normal")))

        assert repair_mix_modes(project) == 1

        out = json.loads((project / "draft_info.json").read_text())
        assert out["materials"].get("effects", []) == []
        assert "mix_mode" not in out["materials"]["videos"][0]


def test_a_draft_with_no_blend_mode_is_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        before = json.dumps({"tracks": [], "materials": {"videos": [{"id": "v"}]}})
        (project / "draft_info.json").write_text(before)

        assert repair_mix_modes(project) == 0
        assert (project / "draft_info.json").read_text() == before


def test_every_slug_names_a_shader_this_install_actually_has():
    """The mapping is only as good as the bundle it points into. If CapCut ever
    renames one, this is the test that says so rather than a silent no-op."""
    catalogue = _mix_mode_catalogue()
    if not catalogue:
        pytest.skip("no CapCut bundle on this machine")
    missing = sorted(n for n in MIX_MODE_NAME_IDS.values() if n not in catalogue)
    assert not missing, f"MixMode.json has no {missing}"


def test_after_hooks_run_once_all_the_cli_calls_are_done():
    """A spec carrying two decorated keys used to lose the first one's repair.

    Every `capcut` command rewrites draft_info.json from what it recognises, so a
    material an after-hook adds is dropped by the *next* op's call. It only shows
    when one spec carries two: a mask beside a blend mode lost the blend mode,
    its `check_flag` and the repaired chroma with it, and every single-key draft
    built to check the repairs passed [proven]. So the hooks run after the loop,
    not inside it.
    """
    timeline = []          # CLI calls and hook runs, in the order they happen
    original = dict(AFTER_HOOKS)
    try:
        for name in original:
            AFTER_HOOKS[name] = lambda _p, n=name: timeline.append(("hook", n))
        spec = {"tracks": [{"type": "video", "items": [
            {"path": "/a.mp4", "start": 0, "duration": 2, "mix": "screen",
             "mask": {"slug": "circle"}, "chroma": {"color": "#00ff00"}}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "draft_info.json").write_text(json.dumps(
                {"tracks": [{"type": "video", "segments": [
                    {"id": "s0", "material_id": "vm", "extra_material_refs": []}]}],
                 "materials": {"videos": [{"id": "vm"}]}}))
            from eyecut.draft import apply_item_ops
            apply_item_ops(spec, project,
                           runner=lambda argv, cwd: (timeline.append(("cli", argv[1]))
                                                     or (0, "")),
                           store=project)
    finally:
        AFTER_HOOKS.clear()
        AFTER_HOOKS.update(original)

    kinds = [kind for kind, _ in timeline]
    assert "hook" in kinds, "the hooks have to run at all"
    assert kinds.index("hook") == len(kinds) - kinds.count("hook"), \
        f"every CLI call must come before the first hook, got {timeline}"
    hooks = [name for kind, name in timeline if kind == "hook"]
    assert len(set(hooks)) == len(hooks), "a hook re-run is wasted work at best"
