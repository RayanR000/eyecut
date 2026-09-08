"""Render a watchable proxy of a CapCut draft, without opening CapCut.

    python -m eyecut.proxy GOJO_BURST -o preview.mp4

Built for edits CapCut itself struggles to scrub: at thousands of clips a second
of timeline, per-clip ffmpeg invocations take twenty minutes and the app's own
preview stutters. This decodes each source **once** into a low-res JPEG sequence
and then assembles the timeline by picking frames, which turns the same job into
seconds and caches the decode for later renders.

Two things it gets right that a naive renderer does not:

* **Frame indices come from the global timeline**, not from each clip's own
  duration. Rendering clips separately and concatenating quantizes every clip
  independently, so a dense run accumulates drift -- and the total duration still
  looks correct because `-shortest` truncates to the audio, which hides it.
* **Source fps is probed per file.** Mixed-rate sources are normal (23.976 and 30
  in the same draft), and assuming one rate silently picks the wrong frames.

It also reproduces the treatments `eyecut.timeline` can write -- `KFTypeScale` as
a centre crop-zoom, `KFTypePositionX/Y` as shake, `KFTypeAlpha` pairs as fades,
and flat brightness/contrast/saturation keyframes as a global grade.

Multi-track: when a draft has overlay video tracks, the proxy composites them
onto the base using Pillow -- with scale, position, rotation, and blend modes
(multiply, screen, overlay, darken, lighten, soft/hard light, dodge, burn).
It also applies material-level crop, chroma keying, and text track rendering.

This is an approximation of CapCut's renderer, not CapCut. It has been wrong
before: a non-uniform `KFTypeScaleX/Y` pair that unfitted clips in CapCut looked
perfectly fine here. Treat a disagreement between the two as the proxy's fault
and check the real app.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# CapCut writes media paths through this token; it expands to the draft folder.
PLACEHOLDER = re.compile(r"^##_draftpath_placeholder_[0-9A-Fa-f-]+_##/")

DRAFT_STORE = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
W, H = 854, 480

# Keyframe properties this renderer understands. CapCut misspells some of its own
# names (KFTypeHightLight, KFTypeLightSensatione); these four are spelled normally.
XFORM_KEYS = ("KFTypeScale", "KFTypePositionX", "KFTypePositionY", "KFTypeBrightness")


def _blend_pil(base, overlay, mode: str):
    """Composite overlay onto base using a named blend mode via Pillow."""
    from PIL import ImageChops
    if mode == "multiply":
        return ImageChops.multiply(base, overlay)
    if mode == "screen":
        return ImageChops.screen(base, overlay)
    if mode in ("darken", "color-burn"):
        return ImageChops.darker(base, overlay)
    if mode in ("lighten", "color-dodge"):
        return ImageChops.lighter(base, overlay)
    if mode == "overlay":
        # 2 * a * b if b < 0.5, else 1 - 2*(1-a)*(1-b)
        import numpy as np
        a = np.asarray(base, dtype=np.float32) / 255
        b = np.asarray(overlay, dtype=np.float32) / 255
        r = np.where(b < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))
        from PIL import Image
        return Image.fromarray((np.clip(r, 0, 1) * 255).astype(np.uint8))
    if mode == "soft-light":
        import numpy as np
        a = np.asarray(base, dtype=np.float32) / 255
        b = np.asarray(overlay, dtype=np.float32) / 255
        r = np.where(b < 0.5, a - (1 - 2*b) * a * (1 - a),
                     a + (2*b - 1) * (np.sqrt(a) - a))
        from PIL import Image
        return Image.fromarray((np.clip(r, 0, 1) * 255).astype(np.uint8))
    if mode == "hard-light":
        import numpy as np
        a = np.asarray(base, dtype=np.float32) / 255
        b = np.asarray(overlay, dtype=np.float32) / 255
        r = np.where(b < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))
        from PIL import Image
        return Image.fromarray((np.clip(r, 0, 1) * 255).astype(np.uint8))
    return overlay


def _find_active_seg(segs: list[dict], t: float) -> dict | None:
    """Return the segment active at time t (seconds), or None."""
    for s in segs:
        start = s["target_timerange"]["start"] / 1e6
        dur = s["target_timerange"]["duration"] / 1e6
        if start <= t < start + dur:
            return s
    return None


def _seg_source_frame(seg: dict, t: float, name_map: dict, src_fps: dict,
                      tmp: Path) -> Path | None:
    """Return the decoded JPEG path for a segment at timeline time t."""
    f = name_map[seg["material_id"]]
    tl = seg["target_timerange"]["start"] / 1e6
    speed = seg["source_timerange"]["duration"] / seg["target_timerange"]["duration"]
    st = seg["source_timerange"]["start"] / 1e6 + (t - tl) * speed
    idx = int(round(st * src_fps[f])) + 1
    src = tmp / f.replace(".", "_") / ("%06d.jpg" % idx)
    if not src.exists():
        src = tmp / f.replace(".", "_") / ("%06d.jpg" % max(1, idx - 1))
    return src if src.exists() else None


def _get_blend_mode(seg: dict, effects: list[dict]) -> str | None:
    """Return the blend mode name for a segment, or None."""
    refs = set(seg.get("extra_material_refs") or [])
    for e in effects:
        if e.get("id") in refs and e.get("type") == "mix_mode":
            raw = (e.get("name") or "").lower().replace(" ", "-")
            if raw == "brighten":
                raw = "lighten"
            return raw if raw != "normal" else None
    return None


def _get_crop(seg: dict, video_materials: list[dict]) -> dict | None:
    """Return the crop rect {ulx, uly, lrx, lry} for a segment, or None."""
    mid = seg.get("material_id")
    for m in video_materials:
        if m.get("id") == mid:
            c = m.get("crop")
            if not c:
                return None
            ulx = c.get("upper_left_x", 0)
            uly = c.get("upper_left_y", 0)
            lrx = c.get("lower_right_x", 1)
            lry = c.get("lower_right_y", 1)
            if (ulx, uly, lrx, lry) == (0, 0, 1, 1):
                return None
            return {"ulx": ulx, "uly": uly, "lrx": lrx, "lry": lry}
    return None


def _apply_chroma(im, seg: dict, chromas: list[dict]):
    """Apply chroma keying to an RGBA image, making keyed pixels transparent."""
    refs = set(seg.get("extra_material_refs") or [])
    chroma = None
    for c in chromas:
        if c.get("id") in refs:
            chroma = c
            break
    if chroma is None:
        return im
    import numpy as np
    color_hex = (chroma.get("color") or "")[:7]
    if len(color_hex) != 7:
        return im
    kr, kg, kb = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
    intensity = float(chroma.get("intensity_value") or 0)
    if intensity <= 0:
        return im
    arr = np.asarray(im, dtype=np.float32)
    rgb = arr[:, :, :3]
    key = np.array([kr, kg, kb], dtype=np.float32)
    dist = np.sqrt(np.sum((rgb - key) ** 2, axis=2)) / 441.67  # max dist = sqrt(3*255^2)
    threshold = 1.0 - intensity
    alpha = np.clip((dist - threshold) / max(0.3, 1.0 - threshold), 0, 1)
    result = arr.copy()
    result[:, :, 3] = alpha * 255
    from PIL import Image
    return Image.fromarray(result.astype(np.uint8), "RGBA")


def _render_text_overlay(im, t: float, text_tracks: list[dict],
                         text_materials: dict, width: int, height: int):
    """Draw active text segments onto the frame."""
    from PIL import ImageDraw, ImageFont
    for track in text_tracks:
        seg = _find_active_seg(track, t)
        if seg is None:
            continue
        mid = seg.get("material_id")
        mat = text_materials.get(mid)
        if mat is None:
            continue
        try:
            content = json.loads(mat.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        text = content.get("text", "")
        if not text:
            continue
        styles = content.get("styles") or []
        draw = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
            font_bold = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
        except (OSError, IOError):
            font = ImageFont.load_default()
            font_bold = font

        # Render each styled range as a separate chunk, left to right
        x_cursor = None
        y_pos = height - 60
        for style in styles:
            rng = style.get("range", [0, len(text)])
            chunk = text[rng[0]:rng[1]]
            if not chunk:
                continue
            fill_data = style.get("fill", {}).get("content", {}).get("solid", {})
            color_arr = fill_data.get("color", [1, 1, 1])
            r, g, b = [int(c * 255) for c in color_arr[:3]]
            use_font = font_bold if style.get("bold") else font
            if x_cursor is None:
                bbox = draw.textbbox((0, 0), text, font=use_font)
                total_w = bbox[2] - bbox[0]
                x_cursor = (width - total_w) // 2
            draw.text((x_cursor, y_pos), chunk, fill=(r, g, b), font=use_font)
            bbox = draw.textbbox((0, 0), chunk, font=use_font)
            x_cursor += bbox[2] - bbox[0]
    return im


def asset_path(draft: Path, material: dict) -> Path | None:
    """Where a material's file actually is, or None if it cannot be found.

    A material names its file three different ways depending on who wrote the
    draft: CapCut's own `##_draftpath_placeholder_<uuid>_##/assets/...`, a plain
    absolute path, or nothing useful at all. Assuming `assets/<material_name>`
    covers only the flattest case -- `capcut compile` files video under
    `assets/video/`, so a draft eyecut built failed to render at all [proven].
    """
    raw = material.get("path") or ""
    if PLACEHOLDER.match(raw):
        candidate = draft / PLACEHOLDER.sub("", raw)
        if candidate.exists():
            return candidate
    if raw and Path(raw).is_absolute() and Path(raw).exists():
        return Path(raw)
    stem = Path(raw).name or (material.get("material_name") or material.get("name") or "")
    if not stem:
        return None
    flat = draft / "assets" / stem
    if flat.exists():
        return flat
    return next((p for p in (draft / "assets").rglob(stem) if p.is_file()), None)


def probe_fps(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"ffprobe failed on {path}: {out.stderr.strip()[:200]}")
    num, den = json.loads(out.stdout)["streams"][0]["r_frame_rate"].split("/")
    return float(num) / float(den)


def _sample(track: list[tuple[float, float]], t: float) -> float:
    """Linear interpolation over a keyframe list."""
    if t <= track[0][0]:
        return track[0][1]
    for (t0, v0), (t1, v1) in zip(track, track[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return track[-1][1]


def render(project: str | Path, out: Path, *, tmp: Path | None = None,
           store: Path | None = None, width: int = W, height: int = H,
           reuse_cache: bool = True, quiet: bool = False) -> Path:
    say = (lambda *a, **k: None) if quiet else print
    store = Path(store) if store else DRAFT_STORE
    P = Path(project) if Path(project).is_absolute() else store / str(project)
    if not (P / "draft_info.json").exists():
        raise FileNotFoundError(f"no draft_info.json at {P}")
    tmp = Path(tmp) if tmp else Path(os.environ.get("TMPDIR", "/tmp")) / f"eyecut-proxy-{P.name}"
    tmp.mkdir(parents=True, exist_ok=True)
    out = Path(out)

    d = json.loads((P / "draft_info.json").read_text())
    fps = float(d.get("fps") or 30)
    name = {m["id"]: (m.get("material_name") or m.get("name", ""))
            for m in d["materials"]["videos"]}
    located = {(m.get("material_name") or m.get("name", "")): asset_path(P, m)
               for m in d["materials"]["videos"]}
    video_tracks = [t for t in d["tracks"] if t.get("type") == "video"]
    if not video_tracks:
        raise RuntimeError("draft has no video track")
    all_track_segs = []
    for vt in video_tracks:
        ts = sorted(vt.get("segments") or [],
                    key=lambda s: s["target_timerange"]["start"])
        all_track_segs.append(ts)
    segs = all_track_segs[0]
    if not segs:
        raise RuntimeError("draft's video track is empty")
    overlay_tracks = all_track_segs[1:]
    effects = d["materials"].get("effects") or []
    video_materials = d["materials"].get("videos") or []
    chromas = d["materials"].get("chromas") or []
    text_tracks_raw = [t for t in d["tracks"] if t.get("type") == "text"]
    text_track_segs = []
    for tt in text_tracks_raw:
        ts = sorted(tt.get("segments") or [],
                    key=lambda s: s["target_timerange"]["start"])
        text_track_segs.append(ts)
    text_materials = {m["id"]: m for m in (d["materials"].get("texts") or [])}

    # --- decode each distinct source once, in parallel ---------------------
    all_segs_flat = [s for ts in all_track_segs for s in ts]
    files = sorted({name[s["material_id"]] for s in all_segs_flat})
    src_fps: dict[str, float] = {}
    jobs = []
    for f in files:
        path = located.get(f)
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"draft references a file that is not in {P / 'assets'}: {f}")
        src_fps[f] = probe_fps(path)
        dst = tmp / f.replace(".", "_")
        if reuse_cache and dst.is_dir() and len(list(dst.iterdir())) > 100:
            say(f"cached {f} ({len(list(dst.iterdir()))} frames)")
            continue
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        say(f"decoding {f} @ {src_fps[f]:.3f}fps", flush=True)
        jobs.append((f, dst, subprocess.Popen(
            ["ffmpeg", "-v", "error", "-hwaccel", "videotoolbox", "-i", str(path),
             "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
             "-q:v", "4", str(dst / "%06d.jpg"), "-y"])))
    for f, dst, pr in jobs:
        if pr.wait() != 0:
            raise RuntimeError(f"decode failed: {f}")
        say(f"  {f}: {len(list(dst.iterdir()))} frames", flush=True)

    # --- per-segment transforms -------------------------------------------
    xform: dict[int, tuple[float, dict]] = {}
    fades: list[tuple[float, float]] = []
    for i, s in enumerate(segs):
        base = s["target_timerange"]["start"] / 1e6
        tr: dict[str, list[tuple[float, float]]] = {}
        for g in s.get("common_keyframes") or []:
            pt = g.get("property_type")
            kl = sorted(g.get("keyframe_list") or [], key=lambda k: k["time_offset"])
            if pt in XFORM_KEYS and kl:
                tr[pt] = [(k["time_offset"] / 1e6, k["values"][0]) for k in kl]
            if pt == "KFTypeAlpha" and len(kl) == 2 and kl[0]["values"][0] > kl[1]["values"][0]:
                fades.append((base + kl[0]["time_offset"] / 1e6,
                              base + kl[1]["time_offset"] / 1e6))
        b = tr.get("KFTypeBrightness")
        if b and len({round(v, 4) for _, v in b}) == 1:
            tr.pop("KFTypeBrightness")      # flat = a plain grade, handled globally
        if tr:
            xform[i] = (base, tr)
    say(f"segments with transform keyframes: {len(xform)}")

    # --- assemble the timeline frame by frame ------------------------------
    seqd = tmp / "seq"
    shutil.rmtree(seqd, ignore_errors=True)
    seqd.mkdir(parents=True)
    total = round(int(d.get("duration") or 0) / 1e6 * fps)
    si, missing = 0, 0
    has_overlays = any(overlay_tracks)
    has_text = any(text_track_segs)
    need_pil = bool(xform) or has_overlays or has_text
    if need_pil:
        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError("this draft has overlays, text, or keyframes that "
                               "need Pillow to render. pip install pillow")

    if has_overlays:
        say(f"overlay tracks: {len(overlay_tracks)}")
    if has_text:
        say(f"text tracks: {len(text_track_segs)}")
    if chromas:
        say(f"chroma materials: {len(chromas)}")

    for p in range(total):
        t = p / fps
        while si + 1 < len(segs) and segs[si + 1]["target_timerange"]["start"] / 1e6 <= t:
            si += 1
        s = segs[si]
        src = _seg_source_frame(s, t, name, src_fps, tmp)
        if src is None:
            missing += 1
            continue
        dst = seqd / ("%06d.jpg" % p)

        # Determine if this frame needs Pillow
        has_overlay_at_t = False
        if has_overlays:
            for ov_segs in overlay_tracks:
                if _find_active_seg(ov_segs, t) is not None:
                    has_overlay_at_t = True
                    break
        has_text_at_t = False
        if has_text:
            for tt_segs in text_track_segs:
                if _find_active_seg(tt_segs, t) is not None:
                    has_text_at_t = True
                    break
        base_crop = _get_crop(s, video_materials)

        if si in xform or has_overlay_at_t or has_text_at_t or base_crop:
            im = Image.open(src).convert("RGB")
            w, h = im.size

            # Apply material-level crop
            if base_crop:
                cx0 = round(w * base_crop["ulx"])
                cy0 = round(h * base_crop["uly"])
                cx1 = round(w * base_crop["lrx"])
                cy1 = round(h * base_crop["lry"])
                im = im.crop((cx0, cy0, cx1, cy1)).resize((w, h), Image.LANCZOS)

            # Apply base track transforms
            if si in xform:
                base_t, tr = xform[si]
                rel = t - base_t
                z = _sample(tr["KFTypeScale"], rel) if "KFTypeScale" in tr else 1.0
                dx = _sample(tr["KFTypePositionX"], rel) if "KFTypePositionX" in tr else 0.0
                dy = _sample(tr["KFTypePositionY"], rel) if "KFTypePositionY" in tr else 0.0
                white = 0.0
                if "KFTypeBrightness" in tr:
                    bt = tr["KFTypeBrightness"]
                    white = max(0.0, min(1.0, _sample(bt, rel) - min(v for _, v in bt)))
                if z != 1.0 or dx or dy or white:
                    z = max(z, 1.0 + 2 * max(abs(dx), abs(dy)))
                    cw, ch = w / z, h / z
                    cx = min(max(0, (w - cw) / 2 - dx * w), w - cw)
                    cy = min(max(0, (h - ch) / 2 + dy * h), h - ch)
                    im = im.crop((round(cx), round(cy), round(cx + cw), round(cy + ch)))
                    im = im.resize((w, h), Image.LANCZOS)
                    if white:
                        im = Image.blend(im, Image.new("RGB", im.size, (255, 255, 255)), white)

            # Composite overlay tracks
            for ov_segs in overlay_tracks:
                ov_seg = _find_active_seg(ov_segs, t)
                if ov_seg is None:
                    continue
                ov_src = _seg_source_frame(ov_seg, t, name, src_fps, tmp)
                if ov_src is None:
                    continue
                ov_im = Image.open(ov_src).convert("RGBA")

                # Apply crop on overlay material
                ov_crop = _get_crop(ov_seg, video_materials)
                if ov_crop:
                    ow0, oh0 = ov_im.size
                    ov_im = ov_im.crop((
                        round(ow0 * ov_crop["ulx"]), round(oh0 * ov_crop["uly"]),
                        round(ow0 * ov_crop["lrx"]), round(oh0 * ov_crop["lry"])
                    ))

                # Apply chroma key
                if chromas:
                    ov_im = _apply_chroma(ov_im, ov_seg, chromas)

                clip = ov_seg.get("clip") or {}
                sx = clip.get("scale", {}).get("x", 1.0)
                sy = clip.get("scale", {}).get("y", 1.0)
                ox = clip.get("transform", {}).get("x", 0.0)
                oy = clip.get("transform", {}).get("y", 0.0)
                rot = clip.get("rotation", 0)

                ow = round(w * sx)
                oh = round(h * sy)
                if ow < 1 or oh < 1:
                    continue
                ov_im = ov_im.resize((ow, oh), Image.LANCZOS)
                if rot:
                    ov_im = ov_im.rotate(-rot, expand=True, resample=Image.BICUBIC)

                blend_mode = _get_blend_mode(ov_seg, effects)

                px = round((w - ov_im.width) / 2 + ox * w)
                py = round((h - ov_im.height) / 2 - oy * h)

                if blend_mode:
                    # Blend mode: composite the overlapping region
                    canvas = Image.new("RGB", (w, h), (0, 0, 0))
                    canvas.paste(ov_im.convert("RGB"), (px, py))
                    mask = Image.new("L", (w, h), 0)
                    ov_alpha = ov_im.split()[3] if ov_im.mode == "RGBA" else \
                        Image.new("L", ov_im.size, 255)
                    mask.paste(ov_alpha, (px, py))
                    blended = _blend_pil(im, canvas, blend_mode)
                    im = Image.composite(blended, im, mask)
                else:
                    im.paste(ov_im, (px, py), ov_im if ov_im.mode == "RGBA" else None)

            # Render text tracks
            if has_text_at_t:
                im = _render_text_overlay(im, t, text_track_segs,
                                          text_materials, w, h)

            im.save(dst, quality=88)
            continue

        try:
            os.link(src, dst)
        except OSError:
            shutil.copy(src, dst)
    say(f"assembled {len(list(seqd.iterdir()))} of {total} frames (missing {missing})")

    # --- global grade from flat keyframes ----------------------------------
    grades = []
    for s in segs:
        g = {x["property_type"]: x for x in (s.get("common_keyframes") or [])}
        if "KFTypeBrightness" in g:
            def last(key):
                kl = g.get(key, {}).get("keyframe_list") or [{"values": [0]}]
                return kl[-1]["values"][0]
            grades.append((last("KFTypeBrightness"), last("KFTypeContrast"),
                           last("KFTypeSaturation")))
    vf = []
    if grades:
        med = lambda i: sorted(x[i] for x in grades)[len(grades) // 2]
        vf.append("eq=brightness=%.3f:contrast=%.3f:saturation=%.3f"
                  % (med(0), 1 + med(1), 1 + med(2)))
        say(f"grade windows: {len(grades)}")
    for st_, en in fades:
        vf.append(f"fade=t=out:st={st_:.3f}:d={en - st_:.3f}")

    # --- audio: whatever the draft's audio track actually points at --------
    cmd = ["ffmpeg", "-v", "error", "-framerate", f"{fps}", "-i", str(seqd / "%06d.jpg")]
    audio_tracks = [t for t in d["tracks"]
                    if t.get("type") == "audio" and t.get("segments")]
    adur = None
    if audio_tracks:
        aseg = sorted(audio_tracks[0]["segments"],
                      key=lambda s: s["target_timerange"]["start"])[0]
        amat = {m["id"]: (m.get("material_name") or m.get("name") or m.get("path", ""))
                for m in d["materials"].get("audios", [])}
        amaterial = next((m for m in d["materials"].get("audios", [])
                          if m.get("id") == aseg.get("material_id")), {})
        afile = Path(amat.get(aseg.get("material_id"), "")).name
        apath = asset_path(P, amaterial) if amaterial else None
        if afile and apath and apath.exists():
            a0 = aseg["source_timerange"]["start"] / 1e6
            adur = aseg["target_timerange"]["duration"] / 1e6
            cmd += ["-ss", f"{a0:.6f}", "-t", f"{adur:.6f}", "-i", str(apath)]
            say(f"audio: {afile} from {a0:.3f}s for {adur:.3f}s")
        else:
            say(f"audio track references {afile or '?'}, not found in assets — silent proxy")
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if adur is not None:
        cmd += ["-af", f"afade=t=out:st={max(0, adur - 1.5):.3f}:d=1.5"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p"]
    if adur is not None:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd += [str(out), "-y"]
    subprocess.run(cmd, check=True)
    say(f"wrote {out}  ({len(segs)} clips, {fps:g}fps)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render a proxy of a CapCut draft.")
    ap.add_argument("project", help="draft folder name, or an absolute path")
    ap.add_argument("-o", "--out", type=Path, default=Path("proxy.mp4"))
    ap.add_argument("--tmp", type=Path, help="frame cache dir (reused between runs)")
    ap.add_argument("--store", type=Path, help="CapCut drafts dir")
    ap.add_argument("--no-cache", action="store_true", help="re-decode every source")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        render(a.project, a.out, tmp=a.tmp, store=a.store,
               reuse_cache=not a.no_cache, quiet=a.quiet)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
