"""The MCP surface: what Claude can actually call.

The tools are thin -- they turn paths into probes and JSON-able dicts and hand off
to eyecut.media / eyecut.draft, which have their own tests. What is tested here is
that hand-off, and that the tools are actually registered on the server.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from eyecut import server as srv


@pytest.fixture(autouse=True)
def capcut_not_running(monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: False)


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=True)
    return path


def test_the_server_exposes_the_tools_claude_calls():
    names = {t.name for t in asyncio.run(srv.server.list_tools())}
    assert {"register_media", "write_draft", "browse_shots", "preview"} <= names


def test_register_media_probes_each_path_and_writes_the_entries(tmp_path, drafts_dir, clip):
    meta_path = drafts_dir / "proj" / "draft_meta_info.json"
    meta_path.parent.mkdir()
    meta_path.write_text(json.dumps({"draft_name": "proj"}))

    result = srv.register_media(str(meta_path.parent), [str(clip)])

    meta = json.loads(meta_path.read_text())
    entry = [e for g in meta["draft_materials"] for e in g["value"]]
    assert [e["file_Path"] for e in entry] == [str(clip)]
    assert entry[0]["width"] == 320          # probed, not guessed
    assert result["added"] == [str(clip)]


def test_register_media_reports_a_bad_path_without_writing(tmp_path, drafts_dir):
    meta_path = drafts_dir / "proj" / "draft_meta_info.json"
    meta_path.parent.mkdir()
    meta_path.write_text(json.dumps({"draft_name": "proj"}))

    with pytest.raises(Exception, match="missing.mp4"):
        srv.register_media(str(meta_path.parent), [str(tmp_path / "missing.mp4")])
    assert "draft_materials" not in json.loads(meta_path.read_text())


def test_write_draft_compiles_registers_and_reports_the_duration(tmp_path, drafts_dir, clip):
    spec = {"name": "srv", "tracks": [
        {"type": "video", "items": [{"path": str(clip), "start": 0, "duration": 5}]}]}

    result = srv.write_draft(spec, str(drafts_dir / "proj"))

    meta = json.loads((Path(result["project"]) / "draft_meta_info.json").read_text())
    # the copy compile made inside the draft, not the original the spec named
    assert [e["file_Path"] for g in meta["draft_materials"]
            for e in g["value"]] == ["./assets/video/clip.mp4"]
    assert result["duration_us"] == meta["tm_duration"] == 5_000_000


def test_write_draft_finds_its_sources_in_the_spec(tmp_path, drafts_dir, clip):
    """Claude writes one spec; it should not also have to list the same files again."""
    spec = {"name": "srv", "tracks": [
        {"type": "video", "items": [{"path": str(clip), "start": 0, "duration": 5}]}]}

    assert srv.sources_in(spec) == [Path(clip)]


@pytest.fixture
def two_shot_clip(tmp_path):
    """Two visually distinct halves, so scene detection has something to find."""
    path = tmp_path / "two_shots.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1",
                    "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=True)
    return path


def test_browse_shots_returns_one_poster_per_shot(tmp_path, two_shot_clip):
    """The posters are the point: Claude reads them off disk to see the footage."""
    result = srv.browse_shots(str(two_shot_clip), out=str(tmp_path / "browser"),
                              windows=[[0, 3], [3, 6]])

    assert result["shots"] == 2
    assert len(result["posters"]) == 2
    assert all(Path(p).stat().st_size > 0 for p in result["posters"])
    assert Path(result["page"]).is_file()


def test_browse_shots_detects_windows_when_not_given(tmp_path, two_shot_clip):
    result = srv.browse_shots(str(two_shot_clip), out=str(tmp_path / "browser"))

    assert result["shots"] >= 2, "the cut between the two halves should be found"
    assert result["windows"][0][0] == 0


def test_browse_shots_refuses_a_source_with_no_shots(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    with pytest.raises(RuntimeError):
        srv.browse_shots(str(empty), out=str(tmp_path / "browser"))


capcut_cli = pytest.mark.skipif(shutil.which("capcut") is None,
                                reason="capcut-cli not installed")


@capcut_cli
def test_preview_renders_the_timeline_it_was_given(tmp_path, drafts_dir, clip):
    """The proxy must resolve media the way the draft actually stores it. Assuming
    assets/<name> misses `capcut compile`'s assets/video/<name> and the render dies
    before it starts [proven -- a draft eyecut built would not preview at all]."""
    spec = {"name": "prev", "tracks": [
        {"type": "video", "items": [{"path": str(clip), "start": 0, "duration": 5}]}]}
    srv.write_draft(spec, str(drafts_dir / "proj"))

    result = srv.preview(str(drafts_dir / "proj"), out=str(tmp_path / "p.mp4"))

    assert Path(result["preview"]).stat().st_size == result["bytes"] > 0
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", result["preview"]], capture_output=True, text=True).stdout
    assert abs(float(duration) - 5.0) < 0.5, "proxy must match the timeline length"
