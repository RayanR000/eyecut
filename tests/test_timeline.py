"""Every check in `Timeline` stands for a bug that already happened once.

These tests are written from the failure, not from the code: each one builds the
exact draft shape that broke a real edit and asserts the save is refused. The
point of the module is that the mistake becomes impossible to repeat, so a test
that only exercised the happy path would be worthless.
"""
import json

import pytest

from eyecut.timeline import (SEGMENT_MATERIALS, Timeline, TimelineError,
                             on_grid, snap_us, uid)

FPS = 60.0
FRAME_US = 1_000_000 / FPS


@pytest.fixture(autouse=True)
def capcut_not_running(monkeypatch):
    """The guard is real and would fail the suite on a machine with CapCut open."""
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: False)


def seg(start_us, dur_us, material_id, *, src_start=0, refs=None):
    return {
        "id": uid(),
        "material_id": material_id,
        "target_timerange": {"start": int(start_us), "duration": int(dur_us)},
        "source_timerange": {"start": int(src_start), "duration": int(dur_us)},
        "extra_material_refs": list(refs or []),
    }


def build(tmp_path, segments, *, videos=None, extra_materials=None, fps=FPS,
          name="PROJECT"):
    """Write a minimal but structurally real draft and return its Timeline."""
    videos = videos or [{"id": "MAT-A", "material_name": "a.mp4", "duration": 60_000_000}]
    materials = {"videos": videos}
    for key in SEGMENT_MATERIALS:
        materials[key] = []
    materials.update(extra_materials or {})
    d = {
        "fps": fps,
        "duration": max((s["target_timerange"]["start"] + s["target_timerange"]["duration"]
                         for s in segments), default=0),
        "materials": materials,
        "tracks": [{"type": "video", "segments": list(segments)},
                   {"type": "audio", "segments": []}],
    }
    proj = tmp_path / name
    proj.mkdir()
    (proj / "draft_info.json").write_text(json.dumps(d))
    return Timeline(proj)


# --- snapping ---------------------------------------------------------------

def test_snap_lands_on_whole_frames():
    for sec in (0.0, 0.4137931, 1.6551724, 8.2333, 57.625):
        us = snap_us(sec, FPS)
        assert us % 1 == 0
        assert on_grid(us, FPS)
        assert abs(us / 1e6 - sec) <= (1 / FPS) / 2 + 1e-9


def test_off_grid_clip_length_gives_an_uneven_cadence():
    """A clip length that is not a whole number of frames cannot play evenly.

    CapCut quantizes every boundary to a project frame on save, so a length of
    1.5 frames alternates between 1- and 2-frame holds -- the cadence wobbles even
    though every clip is identical in microseconds. Snapping the boundaries to the
    frame grid up front is what makes a dense run uniform.
    """
    quantize = lambda us: round(us / FRAME_US)
    n = 40

    off = [round(i * 1.5 * FRAME_US) for i in range(n + 1)]      # 1.5 frames/clip
    assert {quantize(off[i + 1]) - quantize(off[i]) for i in range(n)} == {1, 2}

    snapped = [snap_us(i * 2 / FPS, FPS) for i in range(n + 1)]  # 2 frames/clip
    assert {quantize(snapped[i + 1]) - quantize(snapped[i]) for i in range(n)} == {2}
    assert all(on_grid(us, FPS) for us in snapped)


def test_dividing_a_span_leaves_boundaries_off_the_grid():
    """Laying out N clips by dividing the span is the habit `snap_us` replaces."""
    span_us, n = 49_250_000, 118
    naive = [round(i * span_us / n) for i in range(n + 1)]
    assert sum(not on_grid(us, FPS) for us in naive) > n // 2

    musical = [snap_us(i * (span_us / n) / 1e6, FPS) for i in range(n + 1)]
    assert all(on_grid(us, FPS) for us in musical)
    # snapping moves each boundary by at most half a frame
    assert max(abs(m - x) for m, x in zip(musical, naive)) <= FRAME_US / 2 + 1


def test_snap_rejects_bad_fps():
    with pytest.raises(ValueError):
        snap_us(1.0, 0)


# --- the guards -------------------------------------------------------------

def test_open_refuses_while_capcut_runs(tmp_path, monkeypatch):
    tl = build(tmp_path, [seg(0, 1_000_000, "MAT-A")])
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: True)
    with pytest.raises(TimelineError, match="CapCut is running"):
        Timeline(tl.dir)


def test_open_refuses_on_locked_marker(tmp_path):
    tl = build(tmp_path, [seg(0, 1_000_000, "MAT-A")])
    (tl.dir / ".locked").touch()
    with pytest.raises(TimelineError, match="marked open"):
        Timeline(tl.dir)


def test_missing_draft_is_an_error(tmp_path):
    with pytest.raises(TimelineError, match="no draft_info.json"):
        Timeline(tmp_path / "nope")


# --- array order is playback order -----------------------------------------

def test_save_refuses_unsorted_segments(tmp_path):
    """CapCut plays tracks[].segments in array order, ignoring the timestamps."""
    tl = build(tmp_path, [seg(2_000_000, 500_000, "MAT-A"),
                          seg(0, 500_000, "MAT-A")])
    codes = {p.code for p in tl.check()}
    assert "unsorted" in codes
    with pytest.raises(TimelineError, match="unsorted"):
        tl.save()


def test_sort_segments_fixes_it(tmp_path):
    tl = build(tmp_path, [seg(2_000_000, 500_000, "MAT-A"),
                          seg(0, 500_000, "MAT-A")])
    tl.sort_segments()
    assert "unsorted" not in {p.code for p in tl.check()}
    tl.save()
    written = json.loads((tl.dir / "draft_info.json").read_text())
    starts = [s["target_timerange"]["start"] for s in written["tracks"][0]["segments"]]
    assert starts == sorted(starts)


# --- overlaps and gaps ------------------------------------------------------

def test_overlap_is_fatal(tmp_path):
    tl = build(tmp_path, [seg(0, 1_000_000, "MAT-A"),
                          seg(500_000, 1_000_000, "MAT-A")])
    assert "overlap" in {p.code for p in tl.check()}


def test_gaps_allowed_by_default_but_reportable(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A"),
                          seg(snap_us(2.0, FPS), snap_us(0.5, FPS), "MAT-A")])
    assert "gap" not in {p.code for p in tl.check()}
    assert "gap" in {p.code for p in tl.check(allow_gaps=False)}


# --- the frame grid ---------------------------------------------------------

def test_off_grid_boundary_is_refused(tmp_path):
    """33_322us is a hair under two 60fps frames -- the exact drift bug."""
    tl = build(tmp_path, [seg(0, 33_322, "MAT-A"), seg(33_322, 33_322, "MAT-A")])
    problems = {p.code: p for p in tl.check()}
    assert "off-grid" in problems
    assert "snap_us" in problems["off-grid"].message
    with pytest.raises(TimelineError, match="off-grid"):
        tl.save()


def test_snapped_boundaries_pass(tmp_path):
    two = snap_us(2 / FPS, FPS)
    segs = [seg(snap_us(i * 2 / FPS, FPS), two, "MAT-A") for i in range(30)]
    tl = build(tmp_path, segs)
    assert not [p for p in tl.check() if p.fatal]


# --- orphan pruning ---------------------------------------------------------

def test_orphaned_segment_materials_are_caught_and_pruned(tmp_path):
    """Seven lists carry one entry per clip; a rebuild that drops segments without
    dropping these grew one draft by 6,286 entries."""
    extra = {key: [{"id": f"{key}-live"}, {"id": f"{key}-dead"}]
             for key in SEGMENT_MATERIALS}
    live_refs = [f"{key}-live" for key in SEGMENT_MATERIALS]
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A", refs=live_refs)],
               extra_materials=extra)

    assert "orphans" in {p.code for p in tl.check()}
    removed = tl.prune_orphans()
    assert removed == len(SEGMENT_MATERIALS)          # one dead entry per list
    assert "orphans" not in {p.code for p in tl.check()}
    for key in SEGMENT_MATERIALS:
        assert [m["id"] for m in tl.d["materials"][key]] == [f"{key}-live"]


def test_check_does_not_mutate(tmp_path):
    """check() probes pruning on a copy; calling it must leave the draft alone."""
    extra = {key: [{"id": f"{key}-dead"}] for key in SEGMENT_MATERIALS}
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")], extra_materials=extra)
    before = json.dumps(tl.d, sort_keys=True)
    tl.check()
    assert json.dumps(tl.d, sort_keys=True) == before


# --- materials.videos collapse ---------------------------------------------

def test_duplicate_video_entries_collapse_and_repoint(tmp_path):
    """CapCut's re-save expands videos to one entry per segment: 990 entries for
    4 files, most of a 6.3MB draft."""
    videos = [{"id": f"MAT-{i}", "material_name": "a.mp4", "duration": 60_000_000}
              for i in range(5)]
    segs = [seg(snap_us(i * 0.5, FPS), snap_us(0.5, FPS), f"MAT-{i}") for i in range(5)]
    tl = build(tmp_path, segs, videos=videos)

    dup = [p for p in tl.check() if p.code == "duplicate-videos"]
    assert dup and not dup[0].fatal          # bloat, not corruption
    assert tl.collapse_videos() == 4
    assert len(tl.d["materials"]["videos"]) == 1
    assert {s["material_id"] for s in tl.segments} == {"MAT-0"}
    assert "duplicate-videos" not in {p.code for p in tl.check()}


def test_collapse_is_a_noop_when_already_clean(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")])
    assert tl.collapse_videos() == 0


# --- referential integrity --------------------------------------------------

def test_segment_pointing_at_nothing_is_fatal(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-GONE")])
    assert "dangling-material" in {p.code for p in tl.check()}


def test_reading_past_the_end_of_the_source_is_fatal(tmp_path):
    videos = [{"id": "MAT-A", "material_name": "a.mp4", "duration": 1_000_000}]
    s = seg(0, snap_us(0.5, FPS), "MAT-A", src_start=900_000)
    tl = build(tmp_path, [s], videos=videos)
    problems = {p.code: p for p in tl.check()}
    assert "source-overrun" in problems
    assert "a.mp4" in problems["source-overrun"].message


# --- save / backup / restore ------------------------------------------------

def test_save_writes_a_backup_once(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")])
    original = (tl.dir / "draft_info.json").read_text()
    tl.save(backup_tag="v1")
    bak = tl.dir / "draft_info.json.v1_bak"
    assert bak.read_text() == original

    tl.d["duration"] = 999
    tl.save(backup_tag="v1")
    assert bak.read_text() == original      # not overwritten by the second save


def test_restore_brings_the_backup_back(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")])
    tl.save(backup_tag="pre")
    tl.replace_segments([seg(0, snap_us(0.25, FPS), "MAT-A"),
                         seg(snap_us(0.25, FPS), snap_us(0.25, FPS), "MAT-A")])
    tl.save()
    assert len(Timeline(tl.dir).segments) == 2
    tl.restore("pre")
    assert len(tl.segments) == 1
    assert len(Timeline(tl.dir).segments) == 1


def test_restore_without_a_backup_errors(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")])
    with pytest.raises(TimelineError, match="no backup"):
        tl.restore("nope")


def test_force_saves_a_broken_draft(tmp_path):
    """An escape hatch has to exist, but it must be explicit."""
    tl = build(tmp_path, [seg(0, 33_322, "MAT-A")])
    with pytest.raises(TimelineError):
        tl.save()
    tl.save(force=True)
    assert Timeline(tl.dir).segments[0]["target_timerange"]["duration"] == 33_322


def test_save_returns_nonfatal_problems(tmp_path):
    videos = [{"id": "MAT-A", "material_name": "a.mp4", "duration": 60_000_000},
              {"id": "MAT-B", "material_name": "a.mp4", "duration": 60_000_000}]
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")], videos=videos)
    warnings = tl.save()
    assert [p.code for p in warnings] == ["duplicate-videos"]


def test_replace_segments_sorts_and_prunes(tmp_path):
    extra = {key: [{"id": f"{key}-dead"}] for key in SEGMENT_MATERIALS}
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")], extra_materials=extra)
    tl.replace_segments([seg(snap_us(1.0, FPS), snap_us(0.5, FPS), "MAT-A"),
                         seg(0, snap_us(0.5, FPS), "MAT-A")])
    assert not [p for p in tl.check() if p.fatal]
    tl.save()


# --- reading helpers --------------------------------------------------------

def test_source_name_resolves_the_file(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, FPS), "MAT-A")])
    assert tl.source_name(tl.segments[0]) == "a.mp4"


def test_fps_defaults_when_absent(tmp_path):
    tl = build(tmp_path, [seg(0, snap_us(0.5, 30.0), "MAT-A")], fps=None)
    assert tl.fps == 30.0
