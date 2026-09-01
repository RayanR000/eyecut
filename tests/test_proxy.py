"""Rendering a draft's timeline without opening CapCut.

Approximate by design -- treat any disagreement with the app as the proxy's fault.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from eyecut import proxy
from eyecut.draft import write_draft


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


capcut_cli = pytest.mark.skipif(shutil.which("capcut") is None,
                                reason="capcut-cli not installed")


@capcut_cli
def test_render_matches_the_timeline_it_was_given(tmp_path, drafts_dir, clip):
    """The proxy must resolve media the way the draft actually stores it. Assuming
    assets/<name> misses `capcut compile`'s assets/video/<name> and the render dies
    before it starts [proven -- a draft eyecut built would not preview at all]."""
    spec = {"name": "prev", "tracks": [
        {"type": "video", "items": [{"path": str(clip), "start": 0, "duration": 5}]}]}
    write_draft(spec, drafts_dir / "proj", [])

    rendered = proxy.render(drafts_dir / "proj", tmp_path / "p.mp4", quiet=True)

    assert rendered.stat().st_size > 0
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(rendered)], capture_output=True, text=True).stdout
    assert abs(float(duration) - 5.0) < 0.5, "proxy must match the timeline length"
