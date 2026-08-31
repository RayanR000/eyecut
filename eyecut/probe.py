"""`probe_media` — one file path in, the `MediaProbe` register_media wants out.

ffprobe is the only source of truth here; every field CapCut needs (dimensions,
duration in integer microseconds) comes from it. Stills have no duration at all,
so they get CapCut's own import default rather than a probed value.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from eyecut.media import IMAGE_SUFFIXES, PHOTO_DURATION_US, MediaProbe


class ProbeError(RuntimeError):
    """ffprobe could not read the file, or read it and found no usable stream."""


def _ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True)
    if proc.returncode:
        raise ProbeError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def probe_media(path: Path | str) -> MediaProbe:
    """Probe `path`. The returned path is absolute — CapCut resolves it verbatim."""
    path = Path(path).resolve()
    probed = _ffprobe(path)
    streams = probed.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise ProbeError(f"no audio or video stream in {path}")

    if path.suffix.lower() in IMAGE_SUFFIXES:
        # A still's "duration" is whatever CapCut assigns on import, not a property
        # of the file — ffprobe reports 1 frame at an invented frame rate.
        return MediaProbe(path=path, metetype="photo", width=video["width"],
                          height=video["height"], duration_us=PHOTO_DURATION_US)

    seconds = probed.get("format", {}).get("duration")
    if seconds is None:
        raise ProbeError(f"ffprobe reported no duration for {path}")
    duration_us = round(float(seconds) * 1_000_000)   # integer microseconds, always

    if video is None:
        return MediaProbe(path=path, metetype="music", width=0, height=0,
                          duration_us=duration_us)
    return MediaProbe(path=path, metetype="video", width=video["width"],
                      height=video["height"], duration_us=duration_us)
