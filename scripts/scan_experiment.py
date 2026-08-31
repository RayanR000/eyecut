#!/usr/bin/env python3
"""Stage-1 (`scan`) experiment: does contact-sheet selection work on unfamiliar footage?

Pipeline per docs/SPEC.md stage 1 — probe and reject, scene-detect, sample 3 frames
per shot, tile into contact sheets. Stops there: reading the sheets is the actual
test, and that happens with a vision model, not in this script.

    python3 scripts/scan_experiment.py footage/Sintel.2010.1080p.mkv --out scratch/sintel
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

FRAMES_PER_SHOT = 3          # 1 says nothing about motion; a midpoint sample can land on black
PER_SHEET = 35               # batching is what makes the vision pass affordable
MIN_SHOT_S = 0.15

# scdet scores visual dissimilarity, not "was there a cut". A well-matched cut
# (same palette, continuous motion) scores low; a whip pan inside one shot scores
# high. The two are not separable by any threshold.
#
# Deriving the threshold from the score distribution was tried and does not work
# [proven failure]: on a 49s span the candidates showed a clean 2.8 -> 1.8 break,
# but that was small-sample noise. Over a full film (Tears of Steel: 17,620
# frames, 921 candidates) the distribution is smooth from 36.6 down to 1.0 with
# no gap anywhere, and gap-hunting instead locked onto the sparse top of the
# range and *under*-detected -- 95 shots vs 106 at a plain fixed 10.0.
#
# So: a deliberately permissive fixed threshold, and let the per-boundary score
# carry the uncertainty. The error costs are asymmetric. Splitting one real shot
# in two is nearly free -- both halves describe the same content, and `arrange`
# can use either, or the vision pass can merge neighbours it describes
# identically. Missing a cut is not: a 48s "shot" sampled at 3 frames hands
# `arrange` a record that is wrong about most of its own span.
SCORE_THRESHOLD = 3.0        # ~22 cuts/min on ToS, near the prototype's 26/min


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def probe(src: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
             "-show_format", "-of", "json", str(src)])
    if r.returncode:
        sys.exit(f"ffprobe failed on {src}: {r.stderr.strip()}")
    d = json.loads(r.stdout)
    v = d["streams"][0]
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "path": str(src),
        "codec": v.get("codec_name"),
        "fps": round(fps, 3),
        "width": v.get("width"),
        "height": v.get("height"),
        "duration_s": round(float(d["format"]["duration"]), 3),
    }


def reject_reasons(info: dict) -> list[str]:
    """The three rejections the prototype hit in one session. fps is reported, not
    fatal: the != 24 check only means 'frame-interpolated' for anime sources."""
    out = []
    if info["codec"] == "av1":
        out.append("codec av1 — CapCut cannot read it")
    if abs(info["fps"] - 24.0) > 0.5:
        out.append(f"fps {info['fps']} != 24 (fine for non-anime; suspect interpolation if anime)")
    return out


def frame_scores(src: Path) -> list[tuple[float, float]]:
    """One decode pass at threshold=0 -> (time, score) for every frame. Cheaper
    than the old scheme: the same pass yields the boundaries and their scores."""
    r = run(["ffmpeg", "-v", "info", "-i", str(src),
             "-vf", "scdet=threshold=0", "-f", "null", "-"])
    out = []
    for m in re.finditer(r"lavfi\.scd\.score:\s*([\d.]+),\s*lavfi\.scd\.time:\s*([\d.]+)", r.stderr):
        out.append((float(m.group(2)), float(m.group(1))))
    if not out:
        sys.exit(f"scdet produced no scores for {src}: {r.stderr.strip()[-400:]}")
    return out


def score_summary(scores: list[float], threshold: float) -> dict:
    """Reported, not used to choose anything -- kept so a future calibration pass
    (hand-count one span, fit the threshold to it) has the raw shape to work from."""
    ranked = sorted(scores, reverse=True)
    return {
        "threshold": threshold,
        "basis": "fixed and permissive; distribution-derived thresholds proved unusable",
        "frames_scored": len(scores),
        "top_scores": [round(x, 1) for x in ranked[:10]],
        "counts_by_threshold": {str(t): sum(1 for x in scores if x >= t)
                                for t in (12, 8, 6, 4, 3, 2)},
    }


def cluster_boundaries(scored, threshold: float, fps: float):
    """Collapse runs of consecutive above-threshold frames into one boundary each.

    A single cut usually trips scdet on 2+ adjacent frames, and a dissolve trips it
    across many -- a dissolve *is* a sustained frame difference. Treating every
    qualifying frame as its own boundary produced 326 boundaries for 215 shots on
    Sintel, with the surplus silently swallowed by the minimum-shot-length filter.

    Clustering makes the de-duplication explicit and keeps what it was discarding:
    the run's *width*, which is the dissolve signal `profile` already relies on
    ("frame-diff width at boundaries").
    """
    gap = 1.5 / fps if fps else 0.08          # tolerate one dropped frame inside a run
    runs, cur = [], []
    for tm, sc in scored:
        if sc < threshold or tm <= 0:
            continue
        if cur and tm - cur[-1][0] <= gap:
            cur.append((tm, sc))
        else:
            if cur:
                runs.append(cur)
            cur = [(tm, sc)]
    if cur:
        runs.append(cur)

    bounds = []
    for run in runs:
        peak_t, peak_s = max(run, key=lambda x: x[1])
        width = len(run)
        mean_s = sum(sc for _, sc in run) / width

        bounds.append({
            "time": peak_t,
            "score": round(peak_s, 3),
            "run_mean_score": round(mean_s, 3),
            "width_frames": width,
            "width_s": round(width / fps, 4) if fps else None,
            "kind": "cut",
        })
    return bounds


def detect_gradual_change(scored, cut_threshold: float, fps: float):
    """Regions of sustained gradual change. NOT a dissolve detector [proven failure].

    Two things were tried and neither works. Dissolves are not wide runs above the
    cut threshold -- the widest such run is 4 frames in Sintel, 5 in Tears of Steel,
    because a dissolve changes little between adjacent frames, which is what makes
    it gradual. And they are not recoverable from the sustained sub-threshold bands
    below either: both candidates checked by eye were false positives, one a slow
    push-in through fog, one a credit crawl.

    The reason is structural, not a matter of tuning. `scdet` measures frame-to-frame
    difference, and "two images superimposed, changing gradually" and "one image
    moving slowly" produce the same signal. The information is not in the score.

    What this returns is honest about itself: regions where the picture changed
    steadily but never enough to be a cut. Useful as "something is happening here",
    useless as a transition classifier.
    """
    lo, hi = 0.5, cut_threshold
    need = max(8, int(0.3 * fps))
    out, cur = [], []
    for tm, sc in scored:
        if lo <= sc < hi:
            cur.append((tm, sc))
            continue
        if len(cur) >= need:
            out.append(cur)
        cur = []
    if len(cur) >= need:
        out.append(cur)

    return [{
        "start": run[0][0],
        "end": run[-1][0],
        "width_frames": len(run),
        "width_s": round(len(run) / fps, 4) if fps else None,
        "mean_score": round(sum(sc for _, sc in run) / len(run), 3),
        "peak_score": round(max(sc for _, sc in run), 3),
        "kind": "gradual-change (dissolve or slow motion; not separable)",
    } for run in out]


def scenes(src: Path, duration: float, fps: float):
    """-> (shots, diagnostics). Each shot carries the boundary that opened it, so
    `arrange` can prefer confident cuts and `profile` can count dissolves."""
    scored = frame_scores(src)
    t = SCORE_THRESHOLD
    pick = score_summary([sc for _, sc in scored], t)

    bounds = cluster_boundaries(scored, t, fps)
    pick["raw_qualifying_frames"] = sum(1 for _, sc in scored if sc >= t)
    pick["boundaries_after_clustering"] = len(bounds)
    dissolves = detect_gradual_change(scored, t, fps)
    pick["gradual_change_regions"] = len(dissolves)

    marks = [None] + bounds + [None]
    times = [0.0] + [b["time"] for b in bounds] + [duration]
    shots = []
    for i in range(len(times) - 1):
        a, b = times[i], times[i + 1]
        if b - a < MIN_SHOT_S:
            continue
        opened = marks[i]
        shots.append({
            "start": a, "end": b,
            "boundary_score": opened["score"] if opened else None,
            "boundary_kind": opened["kind"] if opened else "start-of-file",
            "boundary_width_frames": opened["width_frames"] if opened else None,
            "boundary_confidence": (
                "start-of-file" if not opened else
                "high" if opened["score"] >= t * 3 else
                "medium" if opened["score"] >= t * 1.6 else "low"),
        })
    return shots, pick, dissolves


def sample(src: Path, shots, outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    shot_records = []
    for i, sh in enumerate(shots):
        start, end = sh["start"], sh["end"]
        span = end - start
        # Fractions, not the midpoint — the midpoint landed on a black frame.
        times = [start + span * f for f in (0.2, 0.5, 0.8)][:FRAMES_PER_SHOT]
        frames = []
        for j, t in enumerate(times):
            dst = outdir / f"shot{i:04d}_{j}.jpg"
            r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
                     "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(dst)])
            if r.returncode == 0 and dst.exists():
                frames.append(dst.name)
        shot_records.append({"index": i, "start": round(start, 3), "end": round(end, 3),
                             "duration": round(span, 3), "frames": frames,
                             "boundary_score": sh["boundary_score"],
                             "boundary_kind": sh["boundary_kind"],
                             "boundary_width_frames": sh["boundary_width_frames"],
                             "boundary_confidence": sh["boundary_confidence"]})
    return shot_records


def sheets(shot_records, framedir: Path, outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    made = []
    for n in range(0, len(shot_records), PER_SHEET):
        batch = shot_records[n:n + PER_SHEET]
        listfile = outdir / f"sheet{n // PER_SHEET:03d}.txt"
        # one row per shot: its 3 frames side by side
        imgs = [framedir / f for rec in batch for f in rec["frames"]]
        if not imgs:
            continue
        listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in imgs))
        dst = outdir / f"sheet{n // PER_SHEET:03d}.jpg"
        cols = FRAMES_PER_SHOT
        rows = (len(imgs) + cols - 1) // cols
        r = run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listfile), "-vf", f"scale=320:-2,tile={cols}x{rows}",
                 "-frames:v", "1", "-q:v", "5", str(dst)])
        if r.returncode == 0:
            made.append({"sheet": dst.name,
                         "shots": [rec["index"] for rec in batch],
                         "bytes": dst.stat().st_size})
        else:
            made.append({"sheet": dst.name, "error": r.stderr.strip()[:200]})
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, help="only sample the first N shots")
    args = ap.parse_args()

    src, out = Path(args.source), Path(args.out)
    info = probe(src)
    info["reject_reasons"] = reject_reasons(info)
    print(json.dumps(info, indent=2))

    shots, pick, dissolves = scenes(src, info["duration_s"], info["fps"])
    print(json.dumps({"threshold_pick": pick}, indent=2))
    rate = len(shots) / (info["duration_s"] / 60)
    print(f"{len(shots)} shots detected ({rate:.1f}/min)", flush=True)
    if args.limit:
        shots = shots[:args.limit]

    recs = sample(src, shots, out / "frames")
    made = sheets(recs, out / "frames", out / "sheets")

    (out / "shots.json").write_text(json.dumps(
        {"source": info, "threshold_pick": pick, "shot_count": len(recs),
         "gradual_change_regions": dissolves, "shots": recs, "sheets": made}, indent=2) + "\n")
    print(f"{len(recs)} shots sampled, {len(made)} contact sheets -> {out}")


if __name__ == "__main__":
    main()
