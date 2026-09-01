"""Find spoken lines in footage, so a buildup can land on one.

    python -m eyecut.speech FOOTAGE.mp4 --windows shots.txt

Anime packs are mostly music and effects with occasional dialogue, and scrubbing
for a usable line by ear is slow. This scores every 25ms frame on three signals
and keeps the runs that look like speech:

* **Voice-band ratio** -- how much of the frame's energy sits in 300-3400 Hz.
* **Spectral flatness** -- speech is tonal (low flatness); effects and noise are
  flat. This is what separates a line from an explosion at the same loudness.
* **Level above the file's own noise floor**, taken as its 40th percentile, so a
  quiet source is not judged against a loud one.

Runs are merged across gaps of up to 120ms, because the pause between words is
not the end of a line. No external audio library is needed -- plain `wave` and
numpy, deliberately, since this ran on a machine with neither scipy nor librosa.

Pass `--windows` to also report which shot each candidate falls inside; that is
usually how you decide whether a line is usable, since the line has to belong to
a shot you would actually cut to.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

SR, WIN, HOP = 16000, 400, 160        # 16kHz, 25ms window, 10ms hop
LO, HI = 300, 3400                    # the voice band
GAP_FRAMES = 12                       # 120ms -- a pause between words, not an ending


@dataclass
class Candidate:
    start: float
    end: float
    length: float
    score: float
    shot: int | None = None

    def __str__(self) -> str:
        shot = f"shot {self.shot}" if self.shot is not None else "-"
        return (f"{self.start:8.2f}-{self.end:7.2f}s  {self.length:.2f}s  "
                f"score {self.score:5.2f}  {shot}")


def to_wav(source: Path, dest: Path) -> Path:
    """Mono 16kHz PCM, which is all the detector needs."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-vn",
         "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dest), "-y"],
        capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"could not extract audio from {source}: "
                           f"{out.stderr.strip()[-300:]}")
    return dest


def read_windows(path: Path) -> list[tuple[float, float]]:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            a, b = (float(x) for x in line.split()[:2])
            out.append((a, b))
    return out


def find(source: Path, *, windows: list[tuple[float, float]] | None = None,
         min_len: float = 0.8, max_len: float = 3.2,
         ratio_min: float = 0.50, flat_max: float = 0.50,
         floor_mult: float = 2.0) -> list[Candidate]:
    """Speech-like runs in `source`, best first."""
    with tempfile.TemporaryDirectory() as td:
        wav = to_wav(Path(source), Path(td) / "a.wav")
        with wave.open(str(wav)) as w:
            raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if len(a) < WIN * 2:
        return []

    n = 1 + (len(a) - WIN) // HOP
    frames = np.lib.stride_tricks.as_strided(
        a, (n, WIN), (a.strides[0] * HOP, a.strides[0])) * np.hanning(WIN)
    spec = np.abs(np.fft.rfft(frames, axis=1)) + 1e-10
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    band = spec[:, (freqs >= LO) & (freqs <= HI)]
    band_e, tot_e = band.sum(1), spec.sum(1)

    ratio = band_e / tot_e
    flat = np.exp(np.log(band).mean(1)) / band.mean(1)   # low = tonal = voice-like
    floor = np.percentile(band_e, 40)
    if floor <= 0:                                       # digitally silent track
        return []
    voiced = (ratio > ratio_min) & (flat < flat_max) & (band_e > floor * floor_mult)

    runs, start, gap = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > GAP_FRAMES:
                runs.append((start, i - gap))
                start = None
    if start is not None:
        runs.append((start, len(voiced) - 1))

    def shot_of(t0: float, t1: float) -> int | None:
        for i, (x, y) in enumerate(windows or []):
            if x <= t0 and t1 <= y:
                return i
        return None

    out: list[Candidate] = []
    for s, e in runs:
        t0, t1 = s * HOP / SR, e * HOP / SR
        if not (min_len <= t1 - t0 <= max_len):
            continue
        sl = slice(s, e)
        score = float(ratio[sl].mean() * (1 - flat[sl].mean())
                      * np.log10(1 + band_e[sl].mean() / floor))
        out.append(Candidate(round(t0, 2), round(t1, 2), round(t1 - t0, 2),
                             round(score, 3), shot_of(t0, t1)))
    out.sort(key=lambda c: (c.shot is not None, c.score), reverse=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find spoken lines in a video file.")
    ap.add_argument("source", type=Path)
    ap.add_argument("--windows", type=Path,
                    help="shot windows file; candidates get the shot they fall in")
    ap.add_argument("--min-len", type=float, default=0.8)
    ap.add_argument("--max-len", type=float, default=3.2)
    ap.add_argument("--ratio", type=float, default=0.50, help="min voice-band share")
    ap.add_argument("--flat", type=float, default=0.50, help="max spectral flatness")
    ap.add_argument("--json", type=Path, help="also write the full list here")
    ap.add_argument("-n", type=int, default=20, help="how many to print")
    a = ap.parse_args(argv)

    if not a.source.exists():
        print(f"no such source: {a.source}", file=sys.stderr)
        return 1
    windows = read_windows(a.windows) if a.windows else None
    try:
        cands = find(a.source, windows=windows, min_len=a.min_len, max_len=a.max_len,
                     ratio_min=a.ratio, flat_max=a.flat)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    inside = sum(c.shot is not None for c in cands)
    print(f"{len(cands)} speech candidates"
          + (f", {inside} inside a known shot" if windows else ""))
    for c in cands[:a.n]:
        print(f"  {c}")
    if not cands:
        print("  (nothing — try --ratio 0.40 --flat 0.60, or the track may be silent)")
    if a.json:
        a.json.write_text(json.dumps([asdict(c) for c in cands], indent=1))
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
