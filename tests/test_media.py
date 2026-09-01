"""register_media must reproduce the entry shape CapCut writes itself.

The fixture is ground truth: entries captured from CapCut-authored projects with
paths and ids replaced. Anything we generate has to match it field for field,
because a missing or misspelled key is exactly what makes CapCut prompt to relink.
"""
import json
from pathlib import Path

import pytest

from eyecut.media import (MediaProbe, entry_for, register_media,
                          set_timeline_duration, timeline_duration_us)

@pytest.fixture(autouse=True)
def capcut_not_running(monkeypatch):
    """Every write goes through the running-CapCut guard, so a real CapCut open on
    the developer's machine failed the whole suite. Pin the guard; the one test
    that asserts it re-patches it to True."""
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: False)


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "draft_materials_entries.json").read_text())
BY_TYPE = {e["metetype"]: e for e in FIXTURE["entries"]}

# Generated per-entry; equality is checked separately or not at all.
VOLATILE = {"id", "create_time", "import_time", "import_time_ms", "file_Path", "extra_info"}


def probe_matching(entry, is_photo=False):
    return MediaProbe(
        path=Path(entry["file_Path"]),
        metetype=entry["metetype"],
        width=entry["width"],
        height=entry["height"],
        duration_us=entry["duration"],
    )


@pytest.mark.parametrize("metetype", ["video", "music", "photo"])
def test_entry_matches_capcut_field_for_field(metetype):
    expected = BY_TYPE[metetype]
    got = entry_for(probe_matching(expected))

    assert set(got) == set(expected), "key set differs from CapCut's"
    for key in set(expected) - VOLATILE:
        assert got[key] == expected[key], key


def test_path_fields_use_capcuts_spelling():
    entry = entry_for(probe_matching(BY_TYPE["video"]))
    assert "file_Path" in entry and "file_path" not in entry  # capital P, verbatim
    assert "metetype" in entry and "mediatype" not in entry   # misspelled, verbatim
    assert entry["extra_info"] == Path(BY_TYPE["video"]["file_Path"]).name


def test_photo_gets_a_default_duration_but_no_roughcut_range():
    photo = entry_for(probe_matching(BY_TYPE["photo"]))
    assert photo["duration"] == 5_000_000
    assert photo["roughcut_time_range"] == {"duration": -1, "start": -1}

    video = entry_for(probe_matching(BY_TYPE["video"]))
    assert video["roughcut_time_range"] == {"duration": video["duration"], "start": 0}


def test_durations_are_integer_microseconds():
    for metetype in ("video", "music", "photo"):
        entry = entry_for(probe_matching(BY_TYPE[metetype]))
        assert isinstance(entry["duration"], int)
        assert isinstance(entry["import_time_ms"], int)


# --- writing into a draft -------------------------------------------------

def make_draft(tmp_path, materials=None):
    meta = {"draft_name": "test", "draft_materials": materials if materials is not None else []}
    path = tmp_path / "draft_meta_info.json"
    path.write_text(json.dumps(meta, indent=4))
    return path


def probes(*specs):
    return [MediaProbe(path=Path(p), metetype=m, width=w, height=h, duration_us=d)
            for p, m, w, h, d in specs]


VIDEO = ("/tmp/a.mp4", "video", 1920, 1080, 405_333_000)
MUSIC = ("/tmp/b.mp3", "music", 0, 0, 196_519_000)


def test_writes_entries_into_the_type_zero_group(tmp_path):
    meta_path = make_draft(tmp_path)
    register_media(meta_path, probes(VIDEO, MUSIC))

    groups = json.loads(meta_path.read_text())["draft_materials"]
    by_type = {g["type"]: g["value"] for g in groups}
    # Every real entry sat in the type-0 group regardless of metetype, and the
    # other groups exist but stay empty.
    assert [e["file_Path"] for e in by_type[0]] == ["/tmp/a.mp4", "/tmp/b.mp3"]
    assert all(by_type[t] == [] for t in (1, 2, 3, 6, 7))


def test_preserves_existing_entries_and_unknown_keys(tmp_path):
    existing = {"file_Path": "/tmp/old.mp4", "metetype": "video", "some_future_key": 1}
    meta_path = make_draft(tmp_path, [{"type": 0, "value": [existing]}])
    register_media(meta_path, probes(VIDEO))

    value = json.loads(meta_path.read_text())["draft_materials"][0]["value"]
    assert value[0] == existing
    assert len(value) == 2


def test_is_idempotent_on_the_same_path(tmp_path):
    meta_path = make_draft(tmp_path)
    first = register_media(meta_path, probes(VIDEO))
    second = register_media(meta_path, probes(VIDEO))

    value = json.loads(meta_path.read_text())["draft_materials"][0]["value"]
    assert len(value) == 1
    assert first.added == ["/tmp/a.mp4"] and second.added == []
    assert second.already_registered == ["/tmp/a.mp4"]


def test_backs_up_before_writing(tmp_path):
    meta_path = make_draft(tmp_path)
    before = meta_path.read_text()
    register_media(meta_path, probes(VIDEO))

    assert (backup := meta_path.with_suffix(".json.bak")).exists()
    assert backup.read_text() == before


def test_refuses_to_write_while_capcut_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: True)
    meta_path = make_draft(tmp_path)
    before = meta_path.read_text()

    with pytest.raises(RuntimeError, match="CapCut is running"):
        register_media(meta_path, probes(VIDEO))
    assert meta_path.read_text() == before


def test_refuses_a_relative_path(tmp_path):
    meta_path = make_draft(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        register_media(meta_path, probes(("a.mp4", "video", 1920, 1080, 1)))


def test_tm_duration_mirrors_the_timeline(tmp_path):
    """CapCut lists a draft's length from tm_duration in the *meta* file, not from
    draft_info.json. capcut-cli compile leaves it 0, which lists as 00:00."""
    meta_path = make_draft(tmp_path)
    (info := tmp_path / "draft_info.json").write_text(json.dumps({"duration": 9_500_000}))

    set_timeline_duration(meta_path, timeline_duration_us(info))

    assert json.loads(meta_path.read_text())["tm_duration"] == 9_500_000


def test_tm_duration_refuses_float_seconds(tmp_path):
    """Float seconds are the 1µs-phantom-overlap bug in a different costume."""
    meta_path = make_draft(tmp_path)
    with pytest.raises(ValueError, match="integer microseconds"):
        set_timeline_duration(meta_path, 9.5)


def test_tm_duration_refuses_to_write_while_capcut_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: True)
    meta_path = make_draft(tmp_path)
    before = meta_path.read_text()

    with pytest.raises(RuntimeError, match="CapCut is running"):
        set_timeline_duration(meta_path, 9_500_000)
    assert meta_path.read_text() == before

