"""`write_draft` — compile a spec into a CapCut draft, then finish what the
compiler leaves undone.

`capcut-cli compile` writes a correct timeline and stops there: it registers no
media (deliberately out of scope, see `eyecut.media`) and leaves `tm_duration` at
0, which lists the draft as 00:00. Both omissions are corrected here, so the draft
opens ready to adjust instead of prompting to relink every clip.

The CLI is shelled out through an injected `runner` so the compile step can be
faked in tests; `_capcut_runner` is the real one.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from eyecut import media
from eyecut.media import (MediaProbe, Registration, register_media,
                          set_timeline_duration, timeline_duration_us)


class CompileError(RuntimeError):
    """`capcut-cli compile` exited non-zero. Carries its stderr verbatim."""


@dataclass
class Draft:
    path: Path
    duration_us: int
    registration: Registration


def _capcut_runner(argv: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stderr


def write_draft(spec: dict, project_dir: Path | str, probes: list[MediaProbe],
                *, runner=_capcut_runner) -> Draft:
    """Compile `spec` into a draft at `project_dir` and make it openable.

    `probes` are the sources the spec references; every one is registered in
    draft_materials. The CapCut-is-running guard runs *before* the compile so a
    refused write leaves no half-built project behind.
    """
    project_dir = Path(project_dir)
    if media.capcut_is_running():   # via the module, so the guard stays patchable
        raise RuntimeError("CapCut is running — it overwrites draft_meta_info.json on quit. "
                           "Quit CapCut and re-run.")

    spec_path = project_dir.parent / f".{project_dir.name}.spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False))
    code, stderr = runner(["capcut", "compile", "--spec", str(spec_path),
                           "--out", str(project_dir)], project_dir)
    if code != 0:
        raise CompileError(f"capcut compile failed ({code}): {stderr.strip()}")

    meta_path = project_dir / "draft_meta_info.json"
    registration = register_media(meta_path, probes)
    duration_us = timeline_duration_us(project_dir / "draft_info.json")
    set_timeline_duration(meta_path, duration_us)
    return Draft(path=project_dir, duration_us=duration_us, registration=registration)
