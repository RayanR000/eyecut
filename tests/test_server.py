"""The MCP surface: what Claude can actually call.

The tools are thin -- they turn paths into probes and JSON-able dicts and hand off
to eyecut.media / eyecut.draft, which have their own tests. What is tested here is
that hand-off, and that the tools are actually registered on the server.
"""
import asyncio
import json
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
    assert {"register_media", "write_draft"} <= names


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
