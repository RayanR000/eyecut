"""The MCP surface — the two tools Claude calls to get an edit into CapCut.

Deliberately thin. The editing decisions happen in the conversation; these tools
only turn what Claude decided into files. Paths in, JSON-able summaries out, and
every real behaviour (the relink-proof media entries, the CapCut-is-running
refusal, the atomic writes) lives in eyecut.media / eyecut.draft.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from eyecut import draft as draft_mod
from eyecut import media as media_mod
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


@server.tool()
def write_draft(spec: dict, project_dir: str) -> dict[str, Any]:
    """Build a CapCut draft from a declarative spec and make it openable: compile
    the timeline, list it in CapCut's project store, register its media, and mirror
    the timeline duration so it does not list as 00:00.

    spec:        capcut-cli compile spec — {"name", "tracks":[{"type","items":[...]}]},
                 item times in seconds.
    project_dir: where to create the draft folder.
    """
    probes = [probe_media(p) for p in sources_in(spec)]
    result = draft_mod.write_draft(spec, project_dir, probes)
    return {"project": str(result.path),
            "duration_us": result.duration_us,
            "registered": result.registration.added}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
