"""`write_draft` delegates the timeline to capcut-cli, then does the two things
capcut-cli does not: register the media and mirror the duration into the meta file.

capcut-cli is not importable, so it is faked -- but the fake writes the same files
the real one does, and every assertion here is on those files, never on the fake.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from eyecut.draft import CompileError, write_draft
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
