"""The MCP surface — the tools Claude calls to get an edit into CapCut.

Deliberately thin. The editing decisions happen in the conversation; these tools
only turn what Claude decided into files. Paths in, JSON-able summaries out, and
every real behaviour (the relink-proof media entries, the CapCut-is-running
refusal, the atomic writes) lives in eyecut.media / eyecut.draft.

Two of them are how Claude *sees*: `browse_shots` writes one poster per shot and
hands back the paths, because Claude reads images from disk directly, and
`preview` renders the timeline so the edit can be judged without opening CapCut.
Neither returns image bytes through MCP -- a path Claude can open costs nothing
to return and keeps a 200-shot source affordable to look at.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from eyecut import draft as draft_mod
from eyecut import frames as frames_mod
from eyecut import media as media_mod
from eyecut import proxy as proxy_mod
from eyecut import shots as shots_mod
from eyecut.probe import probe_media

server = MCPServer("eyecut")

META = "draft_meta_info.json"


def _meta_path(project: str | Path) -> Path:
    """Accept either the project folder or the sidecar itself."""
    project = Path(project)
    return project if project.name == META else project / META


def sources_in(spec: dict) -> list[Path]:
    """Every media path the spec references, in order, without duplicates.

    Claude writes the paths once, in the spec; making it list them a second time
    for registration is how the two lists drift apart.
    """
    found: list[Path] = []
    for track in spec.get("tracks", []):
        for item in track.get("items", []):
            path = item.get("path")
            if path and Path(path) not in found:
                found.append(Path(path))
    return found


@server.tool()
def register_media(project: str, paths: list[str]) -> dict[str, Any]:
    """Register media files in a CapCut draft so it opens without prompting to
    relink every clip. Probes each file with ffprobe. Safe to re-run: paths already
    registered are reported, not duplicated. Refuses to write while CapCut is open.

    project: the draft folder (or its draft_meta_info.json).
    paths:   absolute paths to the video/audio/image files the draft uses.
    """
    probes = [probe_media(p) for p in paths]          # raises before anything is written
    result = media_mod.register_media(_meta_path(project), probes)
    return {"project": str(Path(project)),
            "added": result.added,
            "already_registered": result.already_registered,
            "backup": str(result.backup) if result.backup else None}


# CapCut imports these but cannot decode them, so the draft opens correct and plays
# nothing. Worth saying up front rather than after the user wonders why the preview
# is black [proven -- Sintel.2010.1080p.mkv, Matroska + AC-3].
UNPLAYABLE_SUFFIXES = {".mkv", ".webm", ".avi", ".flv", ".wmv"}


def unplayable(p) -> bool:
    return Path(str(p.path)).suffix.lower() in UNPLAYABLE_SUFFIXES


@server.tool()
def write_draft(spec: dict, project_dir: str) -> dict[str, Any]:
    """Build a CapCut draft from a declarative spec and make it openable: compile
    the timeline, list it in CapCut's project store, register its media, and mirror
    the timeline duration so it does not list as 00:00.

    spec:        capcut-cli compile spec — {"name", "tracks":[{"type","items":[...]}]},
                 item times in seconds.
    project_dir: where to create the draft folder. Its parent must hold at least one
                 project CapCut itself made — compile needs one as a template, or the
                 draft will not open.
    """
    probes = [probe_media(p) for p in sources_in(spec)]
    result = draft_mod.write_draft(spec, project_dir, probes)
    return {"project": str(result.path),
            "duration_us": result.duration_us,
            "registered": result.registration.added,
            "template": str(result.template) if result.template else None,
            "warnings": result.warnings,
            "unplayable": [str(p.path) for p in probes if unplayable(p)]}


def detect_shots(source: str | Path, *, threshold: float = 0.4,
                 min_gap: float = 2.0) -> list[tuple[float, float]]:
    """Shot windows for `source`, via `capcut detect-scenes`.

    Detection is a sampling aid, not a substitute for looking: the spec is explicit
    that four automatic clip-selection metrics lost to a human reading the frames.
    This only decides where to *cut the contact sheet*, never which shot is good.
    """
    out = subprocess.run(["capcut", "detect-scenes", str(source),
                          "--threshold", str(threshold), "--min-gap", str(min_gap), "--json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"capcut detect-scenes failed: {out.stderr.strip()[:300]}")
    return [(s["start"], s["end"]) for s in json.loads(out.stdout).get("segments", [])]


@server.tool()
def browse_shots(source: str, out: str | None = None, windows: list | None = None,
                 title: str | None = None) -> dict[str, Any]:
    """Split a video into shots and render one poster image per shot, so the footage
    can actually be looked at before anything is cut. Also writes a browsable page
    (posters, hover previews, a seekable proxy) for the user to pick shots in.

    Returns the poster paths — read them to see the footage.

    source:  the video file.
    out:     output directory (default ./browser/<name>).
    windows: [[start, end], ...] in seconds. Detected with ffmpeg scene detection
             when omitted.
    title:   label for the page header.
    """
    source = Path(source)
    spans = ([(float(a), float(b)) for a, b in windows] if windows
             else detect_shots(source))
    if not spans:
        raise RuntimeError(f"no shots detected in {source}; pass windows explicitly")
    out_dir = Path(out) if out else Path("browser") / source.stem
    index = shots_mod.build(source, spans, out_dir, title=title, quiet=True)
    posters = sorted(str(p) for p in (out_dir / "media").glob("*.jpg"))
    return {"shots": len(spans),
            "posters": posters,
            "windows": [[a, b] for a, b in spans],
            "page": str(index),
            "serve": f"python -m eyecut.static_server --root {out_dir}"}


@server.tool()
def extract_frames(source: str, times: list[float] | None = None,
                   every: float | None = None, out: str | None = None,
                   sheets: bool = True) -> dict[str, Any]:
    """Write JPEGs from a video so the footage can be looked at. Read the returned
    paths — contact sheets first if there are any, they hold every frame.

    Also reports what makes a source not worth editing: AV1 (CapCut cannot read
    it), fps != 24 (frame-interpolated, if the source is anime), and the windows
    at each end where watermarks live.

    source: the video file.
    times:  seconds to sample. Defaults to 20/50/80% of the source.
    every:  sample every N seconds instead.
    out:    output directory (default ./frames/<name>).
    sheets: tile the frames into contact sheets (~35 rows of 3).
    """
    got = frames_mod.extract_frames(source, times=times, every=every, out=out,
                                   sheets=sheets)
    return {"source": str(got.source),
            "codec": got.codec,
            "fps": got.fps,
            "duration_s": got.duration_s,
            "size": [got.width, got.height],
            "times": got.times,
            "frames": [str(p) for p in got.frames],
            "sheets": [str(p) for p in got.sheets],
            "unusable": got.unusable,
            "warnings": got.warnings}


@server.tool()
def preview(project: str, out: str | None = None) -> dict[str, Any]:
    """Render a watchable proxy of a draft's timeline so the edit can be judged
    without opening CapCut. Approximate, not CapCut's renderer: treat any
    disagreement with the app as the proxy's fault.

    project: draft folder name, or an absolute path to it.
    out:     output .mp4 (default ./proxy.mp4).
    """
    destination = Path(out) if out else Path("proxy.mp4")
    rendered = proxy_mod.render(project, destination, quiet=True)
    return {"preview": str(rendered),
            "bytes": rendered.stat().st_size}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
