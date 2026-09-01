"""The shot picker, and the detection that seeds it.

Detection is a sampling aid: it decides where the strip is cut, never which shot
is good. Four automatic clip-selection metrics were tried on real footage and
every one lost to a human looking at the clips.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from eyecut import shots


@pytest.fixture
def two_shot_clip(tmp_path):
    """Two visually distinct halves, so scene detection has something to find."""
    path = tmp_path / "two_shots.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1",
                    "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=True)
    return path


def test_build_writes_one_poster_per_shot(tmp_path, two_shot_clip):
    """The posters are the point: they are what makes the footage lookable-at, in
    the page and off disk both."""
    index = shots.build(two_shot_clip, [(0, 3), (3, 6)], tmp_path / "browser", quiet=True)

    posters = sorted((tmp_path / "browser" / "media").glob("*.jpg"))
    assert len(posters) == 2
    assert all(p.stat().st_size > 0 for p in posters)
    assert index.is_file()


@pytest.mark.skipif(shutil.which("capcut") is None, reason="capcut-cli not installed")
def test_detect_shots_finds_the_cut_between_two_halves(two_shot_clip):
    spans = shots.detect_shots(two_shot_clip)

    assert len(spans) >= 2, "the cut between the two halves should be found"
    assert spans[0][0] == 0


@pytest.mark.skipif(shutil.which("capcut") is None, reason="capcut-cli not installed")
def test_detect_shots_refuses_a_source_it_cannot_read(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    with pytest.raises(RuntimeError):
        shots.detect_shots(empty)
