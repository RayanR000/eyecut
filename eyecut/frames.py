"""`extract_frames` — how eyecut sees footage.

Claude has vision; what it lacks is JPEGs. This writes them, and optionally tiles
them into contact sheets, which is what makes reading a long source affordable
(~35 rows of 3).

It also reports what would make a source unusable *before* anyone spends time on
it. All three of these were hit in one prototype session:

* codec `av1` — CapCut cannot read the file at all.
* fps != 24 on anime — every "clips for editing" pack online is
  frame-interpolated. Reported, never fatal: 30 and 60 fps live action are fine.
* watermarks cluster in the first and last seconds — so the sampler reports those
  windows rather than trying to detect a logo.

Sampling only. Shot *detection* lives in `eyecut.server.detect_shots`, and the
spec is explicit that it is a sampling aid, not a detector.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SHEET_ROWS = 35          # rows per contact sheet
SHEET_COLS = 3           # frames per row
FRAME_W = 480            # extracted frame width
SHEET_W = 320            # per-tile width inside a sheet
WATERMARK_S = 2.0        # the window at each end where logos live


class ExtractError(RuntimeError):
    """ffmpeg/ffprobe could not read the source, or no frame could be written."""


@dataclass
class Extraction:
    source: Path
    fps: float
    duration_s: float
    codec: str
    width: int
    height: int
    times: list[float]
    frames: list[Path]
    sheets: list[Path] = field(default_factory=list)
    unusable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True)


def probe_source(source: Path) -> dict:
    """codec/fps/size/duration of the first video stream."""
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_streams", "-show_format", "-of", "json", source])
    if proc.returncode:
        raise ExtractError(f"ffprobe failed on {source}: {proc.stderr.strip()[:300]}")
    probed = json.loads(proc.stdout or "{}")
    streams = probed.get("streams") or []
    if not streams:
        raise ExtractError(f"no video stream in {source}")
    stream = streams[0]
    num, den = (stream.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    duration = stream.get("duration") or probed.get("format", {}).get("duration") or 0.0
    return {"codec": stream.get("codec_name") or "?", "fps": round(fps, 3),
            "width": stream.get("width") or 0, "height": stream.get("height") or 0,
            "duration_s": round(float(duration), 3)}


def source_problems(info: dict) -> tuple[list[str], list[str]]:
    """(unusable, warnings). Only AV1 is fatal — the rest is for Claude to weigh."""
    unusable, warnings = [], []
    if info["codec"] == "av1":
        unusable.append("codec av1 — CapCut cannot read it")
    if info["duration_s"] <= 0:
        unusable.append("ffprobe reported no duration")
    if abs(info["fps"] - 24.0) > 0.5:
        warnings.append(f"fps {info['fps']} != 24 — if this is anime it is "
                        "frame-interpolated, which no amount of editing fixes")
    if info["duration_s"] > 2 * WATERMARK_S:
        warnings.append(
            f"check for a watermark in 0-{WATERMARK_S:g}s and "
            f"{info['duration_s'] - WATERMARK_S:.1f}-{info['duration_s']:.1f}s — "
            "that is where they cluster")
    return unusable, warnings


def sample_times(duration_s: float, *, times: list[float] | None = None,
                 every: float | None = None) -> list[float]:
    """The times to grab. `times` wins; `every` walks the source; neither gives
    the same 3-per-span fractions the shot sampler uses (0.2/0.5/0.8 — the
    midpoint alone landed on a black frame)."""
    if times:
        return [t for t in (float(t) for t in times) if 0 <= t < duration_s]
    if every:
        step = float(every)
        if step <= 0:
            raise ValueError("every must be > 0")
        out, t = [], 0.0
        while t < duration_s:
            out.append(round(t, 3))
            t += step
        return out
    return [round(duration_s * f, 3) for f in (0.2, 0.5, 0.8)]


def _tile(images: list[Path], dst: Path, listfile: Path) -> bool:
    cols = SHEET_COLS
    rows = (len(images) + cols - 1) // cols
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in images))
    proc = _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", listfile, "-vf", f"scale={SHEET_W}:-2,tile={cols}x{rows}",
                 "-frames:v", "1", "-q:v", "5", dst])
    return proc.returncode == 0 and dst.exists()


def extract_frames(source: Path | str, *, times: list[float] | None = None,
                   every: float | None = None, out: Path | str | None = None,
                   sheets: bool = True, width: int = FRAME_W) -> Extraction:
    """Write one JPEG per sampled time from `source` into `out`.

    Frames are written even when the source has a warning on it — the point of the
    report is that Claude can look and decide. A fatal problem (AV1) still returns
    the report, with no frames.
    """
    source = Path(source).resolve()
    if not source.exists():
        raise ExtractError(f"no such file: {source}")
    info = probe_source(source)
    unusable, warnings = source_problems(info)
    out_dir = Path(out) if out else Path("frames") / source.stem
    result = Extraction(source=source, times=[], frames=[], unusable=unusable,
                        warnings=warnings, **info)
    if unusable:
        return result

    result.times = sample_times(info["duration_s"], times=times, every=every)
    if not result.times:
        raise ExtractError(f"no sample times inside 0-{info['duration_s']}s of {source}")
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, t in enumerate(result.times):
        dst = out_dir / f"{source.stem}_{i:04d}_{t:.3f}s.jpg"
        # -ss before -i: seek without decoding everything up to it.
        proc = _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", source,
                     "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", dst])
        if proc.returncode == 0 and dst.exists():
            result.frames.append(dst)
        else:
            warnings.append(f"no frame at {t:.3f}s")
    if not result.frames:
        raise ExtractError(f"ffmpeg wrote no frames for {source}")

    if sheets:
        per_sheet = SHEET_ROWS * SHEET_COLS
        sheet_dir = out_dir / "sheets"
        sheet_dir.mkdir(exist_ok=True)
        for n in range(0, len(result.frames), per_sheet):
            batch = result.frames[n:n + per_sheet]
            idx = n // per_sheet
            dst = sheet_dir / f"sheet{idx:03d}.jpg"
            if _tile(batch, dst, sheet_dir / f"sheet{idx:03d}.txt"):
                result.sheets.append(dst)
            else:
                warnings.append(f"could not tile sheet {idx}")
    return result
