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


# --- against the real capcut-cli ------------------------------------------
# The fake above pins our logic; only a real run can pin the CLI's argv and the
# division of labour between us and it.

capcut_cli = pytest.mark.skipif(shutil.which("capcut") is None,
                                reason="capcut-cli not installed")


@capcut_cli
def test_a_real_compile_produces_a_draft_capcut_can_list_and_relink(tmp_path):
    """The two things `capcut compile` alone leaves wrong: no draft_materials (the
    relink prompt) and tm_duration 0 (lists as 00:00). Plus the store entry, which
    is `capcut register`'s job and is delegated to it."""
    source = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30:duration=6",
                    "-pix_fmt", "yuv420p", str(source)],
                   capture_output=True, check=True)
    spec = {"name": "eyecut-int", "tracks": [
        {"type": "video", "items": [{"path": str(source), "start": 0, "duration": 5}]}]}
    probes = [MediaProbe(path=source, metetype="video",
                         width=320, height=240, duration_us=6_000_000)]

    draft = write_draft(spec, tmp_path / "proj", probes)

    meta = json.loads((draft.path / "draft_meta_info.json").read_text())
    assert [e["file_Path"] for g in meta["draft_materials"] for e in g["value"]] == [str(source)]
    assert meta["tm_duration"] == draft.duration_us == 5_000_000
    store = json.loads((tmp_path / "root_meta_info.json").read_text())
    assert store["all_draft_store"], "draft is not listed in the store"
