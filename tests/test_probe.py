"""probe_media turns a path into the MediaProbe register_media needs.

Every test runs against a real file made by ffmpeg -- ffprobe's output is the
thing under test, so faking it would test nothing.
"""
import subprocess

import pytest

from eyecut.probe import ProbeError, probe_media


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=True)
    return path


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    str(path)], capture_output=True, check=True)
    return path


def test_video_reports_its_dimensions_and_duration(video):
    p = probe_media(video)
    assert (p.metetype, p.width, p.height) == ("video", 320, 240)
    assert 5_900_000 <= p.duration_us <= 6_100_000


def test_duration_is_integer_microseconds(video):
    """Float seconds are the 1us phantom-overlap bug the draft layer rejects."""
    assert isinstance(probe_media(video).duration_us, int)


def test_audio_is_music_with_no_dimensions(song):
    p = probe_media(song)
    assert (p.metetype, p.width, p.height) == ("music", 0, 0)
    assert p.duration_us > 0


def test_a_still_gets_capcuts_default_duration(tmp_path):
    still = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=64x48",
                    "-frames:v", "1", str(still)], capture_output=True, check=True)

    p = probe_media(still)
    assert (p.metetype, p.width, p.height) == ("photo", 64, 48)
    assert p.duration_us == 5_000_000  # what CapCut gives a still on import


def test_paths_come_back_absolute(video, monkeypatch):
    """CapCut resolves file_Path verbatim, so register_media refuses a relative one."""
    monkeypatch.chdir(video.parent)
    assert probe_media(video.name).path.is_absolute()


def test_a_file_ffprobe_cannot_read_raises(tmp_path):
    junk = tmp_path / "not-media.mp4"
    junk.write_text("this is not a video")
    with pytest.raises(ProbeError, match="not-media.mp4"):
        probe_media(junk)
