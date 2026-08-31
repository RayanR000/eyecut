"""`write_draft` delegates the timeline to capcut-cli, then does the two things
capcut-cli does not: register the media and mirror the duration into the meta file.

capcut-cli is not importable, so it is faked -- but the fake writes the same files
the real one does, and every assertion here is on those files, never on the fake.
"""
import json
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
    """Stands in for `capcut-cli compile`: writes the draft pair it writes, with
    tm_duration left at 0 -- the omission write_draft exists to correct."""
    def run(argv, cwd):
        if returncode:
            return returncode, stderr
        out = Path(cwd)
        out.mkdir(parents=True, exist_ok=True)
        (out / "draft_info.json").write_text(json.dumps({"duration": duration_us}))
        (out / "draft_meta_info.json").write_text(
            json.dumps({"draft_name": out.name, "tm_duration": 0, "draft_materials": []}))
        return 0, ""
    return run


def test_compiled_draft_carries_the_timeline_duration_in_its_meta_file(tmp_path):
    draft = write_draft(SPEC, tmp_path / "proj", PROBES, runner=fake_capcut())

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    assert meta["tm_duration"] == 9_500_000  # 0 is what lists the draft as 00:00


def test_every_source_in_the_spec_is_registered(tmp_path):
    draft = write_draft(SPEC, tmp_path / "proj", PROBES, runner=fake_capcut())

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    registered = [e["file_Path"] for g in meta["draft_materials"] for e in g["value"]]
    assert registered == ["/footage/a.mp4"]


def test_a_failed_compile_raises_with_the_cli_output(tmp_path):
    with pytest.raises(CompileError, match="bad segment"):
        write_draft(SPEC, tmp_path / "proj", PROBES,
                    runner=fake_capcut(returncode=1, stderr="bad segment"))


def test_refuses_to_compile_while_capcut_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr("eyecut.media.capcut_is_running", lambda: True)
    with pytest.raises(RuntimeError, match="CapCut is running"):
        write_draft(SPEC, tmp_path / "proj", PROBES, runner=fake_capcut())
    assert not (tmp_path / "proj").exists()  # nothing half-written
