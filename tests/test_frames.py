"""`extract_frames`: the frames get written, and the source report is honest.

The rejections tested here are the three the prototype hit in one session, which
is why they are in the report at all rather than left for someone to notice.
"""
import subprocess
from pathlib import Path

import pytest

from eyecut import frames as fr


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=True)
    return path


def test_defaults_sample_three_points_not_just_the_midpoint(clip, tmp_path):
    got = fr.extract_frames(clip, out=tmp_path / "f", sheets=False)
    assert len(got.frames) == 3
    assert all(p.exists() and p.stat().st_size > 0 for p in got.frames)
    assert got.times == [1.2, 3.0, 4.8]


def test_every_walks_the_whole_source(clip, tmp_path):
    got = fr.extract_frames(clip, every=2.0, out=tmp_path / "f", sheets=False)
    assert got.times == [0.0, 2.0, 4.0]
    assert len(got.frames) == 3


def test_explicit_times_outside_the_source_are_dropped(clip, tmp_path):
    got = fr.extract_frames(clip, times=[1.0, 99.0], out=tmp_path / "f", sheets=False)
    assert got.times == [1.0]


def test_contact_sheets_tile_the_frames(clip, tmp_path):
    got = fr.extract_frames(clip, every=0.5, out=tmp_path / "f")
    assert len(got.frames) == 12
    assert len(got.sheets) == 1
    assert got.sheets[0].exists() and got.sheets[0].stat().st_size > 0


def test_one_sheet_per_batch_of_rows(clip, tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "SHEET_ROWS", 2)      # 2 rows x 3 cols = 6 per sheet
    got = fr.extract_frames(clip, every=0.5, out=tmp_path / "f")
    assert len(got.sheets) == 2


def test_off_24_fps_warns_because_anime_at_30_is_interpolated(clip, tmp_path):
    got = fr.extract_frames(clip, out=tmp_path / "f", sheets=False)
    assert any("fps 30.0 != 24" in w for w in got.warnings)


def test_the_watermark_windows_are_reported_not_detected(clip, tmp_path):
    got = fr.extract_frames(clip, out=tmp_path / "f", sheets=False)
    assert any("watermark" in w for w in got.warnings)


def test_av1_is_fatal_and_no_frames_are_written():
    unusable, _ = fr.source_problems(
        {"codec": "av1", "fps": 24.0, "width": 1920, "height": 1080, "duration_s": 10.0})
    assert unusable and "av1" in unusable[0]


def test_a_fatal_source_still_returns_its_report(clip, tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "probe_source", lambda s: {
        "codec": "av1", "fps": 24.0, "width": 1920, "height": 1080, "duration_s": 10.0})
    got = fr.extract_frames(clip, out=tmp_path / "f")
    assert got.frames == [] and got.unusable
    assert not (tmp_path / "f").exists()          # nothing written for an unusable source


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(fr.ExtractError):
        fr.extract_frames(tmp_path / "nope.mp4")


def test_the_cli_prints_the_sheets_it_wrote(clip, tmp_path, capsys):
    """A CLI, not an MCP tool: Claude has a shell, and anything a shell can do in a
    few lines of ffmpeg does not earn a place on the tool surface."""
    code = fr.main([str(clip), "--every", "2", "-o", str(tmp_path / "f")])
    printed = capsys.readouterr().out.split()
    assert code == 0
    assert printed and all(Path(p).exists() for p in printed)
