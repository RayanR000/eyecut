"""Pick cut timings off a song's beat grid.

    python -m eyecut.beatgrid SONG.mp3 --out ./browser/song
    python -m eyecut.beatgrid SONG.mp3 --bpm 145 --phase 0.372 --offset 31.466666

Writes `index.html` plus the grid and a waveform envelope. Open the page, drag
across beats to paint a region, set its cut spacing, then Export to get a JSON
list of slots -- `start` and `dur` per cut, ready to fill with clips.

Why a beat grid and not a waveform: an amplitude plot looks informative but you
cannot see a downbeat in it, so every range drawn by hand lands a few frames off
the beat. Here the waveform is only a landmark -- selection snaps to beats, and
a region is described as "these bars, one cut every N beats" rather than as
pixels. The envelope is drawn so you can find the drop, not so you can aim at it.

You paint *regions*, not individual cuts. A 50-second burst is 172 cuts and
nobody wants to place those by hand, but it is only a dozen regions -- and the
edits that work are long holds with short fast accents, which is a shape you can
see at region scale and cannot see at cut scale.

Tempo detection is the same two-method check as the gojo build's `measure.py`:
an onset-flux grid search plus a bar-range autocorrelation. They have to agree
within 1.5 BPM or the page says so. Tempo inferred from an existing edit's cut
spacing once read 143.6 against a true 145.00 -- an error too small to hear over
five cuts and large enough to lose the beat over a minute. Pass `--bpm` when you
have a number you trust more than a detector.

`--offset` is for when the edit's audio starts partway into the file: times are
displayed and exported relative to it, so they drop into a builder unchanged.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 22050          # decode rate for detection
HOP = 256           # onset envelope hop
ENV_HZ = 100        # waveform envelope samples per second
BPM_LO, BPM_HI = 100.0, 180.0


def say(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)


def decode(path: Path) -> np.ndarray:
    """Mono float32 PCM at SR."""
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True)
    if p.returncode or not p.stdout:
        raise RuntimeError(f"could not decode {path}\n{p.stderr.decode()[-600:]}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def onset_flux(y: np.ndarray) -> tuple[np.ndarray, float]:
    """Half-wave-rectified log-energy difference, and its frame rate."""
    nfr = len(y) // HOP
    fr = y[:nfr * HOP].reshape(nfr, HOP)
    e = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    e = np.log1p(e / (e.mean() + 1e-9))
    flux = np.diff(e, prepend=e[0])
    flux[flux < 0] = 0
    flux /= (flux.max() + 1e-9)
    return flux, SR / HOP


def detect(y: np.ndarray, lo: float = BPM_LO, hi: float = BPM_HI) -> dict:
    """(bpm, phase, score, bpm_autocorr) measured two independent ways.

    The grid search sets the answer; the autocorrelation is the check. The range
    is searched coarsely and then refined, because scanning it at the 0.02 step
    the answer needs is a minute of pointless work.

    Half-tempo is the failure mode to watch. Scoring a comb by its *mean* flux
    rewards sparseness -- the comb at half the true tempo hits every other beat
    and skips the weak ones, so it can outscore the truth. Measured on
    eyelash.mp3 over 60-200 BPM, the raw search returned 72.50 against a known
    145.00. So each octave of the winner is scored too, and the densest grid
    still within `OCTAVE_TOL` of the best score wins.
    """
    flux, frate = onset_flux(y)

    def score(bpm: float, phase: float) -> float:
        step = 60.0 / bpm * frate
        idx = np.arange(phase * frate, len(flux) - 1, step).astype(int)
        return float(flux[idx].mean()) if len(idx) > 20 else 0.0

    def search(bpms, pstep):
        best = (0.0, 0.0, 0.0)
        for bpm in bpms:
            for ph in np.arange(0, 60.0 / bpm, pstep):
                s = score(bpm, ph)
                if s > best[0]:
                    best = (s, float(bpm), float(ph))
        return best

    OCTAVE_TOL = 0.88
    s, bpm, ph = search(np.arange(lo, hi + 0.01, 0.25), 0.01)
    cands = []
    for m in (0.5, 1.0, 2.0):
        c = bpm * m
        if lo - 0.5 <= c <= hi + 0.5:
            cands.append(search(np.arange(max(lo, c - 0.5), min(hi, c + 0.5), 0.02),
                                0.005))
    best = max(c[0] for c in cands)
    s, bpm, ph = max((c for c in cands if c[0] >= best * OCTAVE_TOL),
                     key=lambda c: c[1])

    # The check: the strongest period in the onset envelope should be an exact
    # small-integer multiple of the beat the search found.
    #
    # This started as "autocorrelate over the bar range, divide by 4", copied
    # from the gojo build. That is not a check. On eyelash.mp3 the true bar lag
    # scores 0.058 while the peak at 1.2414s scores 0.195 -- more than 3x
    # stronger -- and 1.2414s is 3.000 beats, not 4. The dominant period here is
    # three beats, so the bar method only ever agreed because its 138-152 BPM
    # window hid every stronger peak. Given a fair window it answered 193.20.
    #
    # Asking instead "is the strongest period an integer number of beats" needs
    # no assumption about the metre, and it confirms the tempo rather than
    # rediscovering it: 1.2414 / 0.413793 = 3.0003.
    f = flux - flux.mean()
    ac = np.correlate(f, f, "full")[len(f) - 1:]
    beat = 60.0 / bpm
    i0, i1 = int(frate * beat * 0.8), min(int(frate * beat * 8.5), len(ac) - 1)
    if i1 > i0:
        peak = (i0 + int(np.argmax(ac[i0:i1]))) / frate
        ratio = peak / beat
        mult = max(1, round(ratio))
        bpm2 = 60.0 / (peak / mult)
        err = abs(ratio - mult)
    else:
        peak, mult, bpm2, err = 0.0, 0, 0.0, 9.9
    return {"bpm": bpm, "phase": ph, "score": s, "bpm_autocorr": bpm2,
            "ac_period": peak, "ac_beats": mult, "ac_err": err}


def envelope(y: np.ndarray) -> list[int]:
    """Peak amplitude per 1/ENV_HZ second, as 0-255. Drawn, never measured."""
    hop = SR // ENV_HZ
    nfr = len(y) // hop
    fr = np.abs(y[:nfr * hop].reshape(nfr, hop)).max(axis=1)
    fr /= (np.percentile(fr, 99.5) + 1e-9)
    return np.clip(fr * 255, 0, 255).astype(int).tolist()


def build(audio: Path, out: Path, *, bpm=None, phase=None, offset=0.0, fps=60.0,
          rng=(BPM_LO, BPM_HI), duration=None, title=None, quiet=False) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    title = title or audio.stem

    y = decode(audio)
    dur = len(y) / SR
    if bpm is None:
        say("  detecting tempo...", quiet)
        d = detect(y, lo=rng[0], hi=rng[1])
        bpm, det = d["bpm"], d
        say(f"  grid search : {d['bpm']:.2f} BPM   score {d['score']:.4f}", quiet)
        say(f"  strongest period: {d['ac_period']:.4f}s = {d['ac_period']/(60/bpm):.3f} "
            f"beats -> {d['ac_beats']} beats, {d['bpm_autocorr']:.2f} BPM", quiet)
        gap = abs(d["bpm"] - d["bpm_autocorr"])
        say(f"  disagreement: {gap:.2f} BPM" + ("   OK" if gap < 1.5 and d["ac_err"] < .06
            else "   <- CHECK THIS BY EAR"), quiet)
        if phase is None:
            phase = d["phase"]
    else:
        det = None
        if phase is None:
            phase = 0.0
        say(f"  given: {bpm:.2f} BPM  phase {phase:.4f}s", quiet)

    beat = 60.0 / bpm
    # Everything below is in DISPLAY time = file time - offset, which is the
    # timeline the builder works in. Detection ran in file time, so rebase.
    ph_disp = ((phase - offset) % beat) if det else phase % beat
    span = min(duration, dur - offset) if duration else dur - offset
    n_beats = int((span - ph_disp) / beat)

    shutil.copy(audio, out / ("audio" + audio.suffix))
    grid = {
        "audio": "audio" + audio.suffix,
        "title": title,
        "bpm": round(bpm, 4), "beat": round(beat, 6), "phase": round(ph_disp, 6),
        "offset": round(offset, 6), "fps": fps,
        "duration": round(dur, 3), "beats": n_beats,
        "env_hz": ENV_HZ, "env": envelope(y),
        "detected": {k: round(v, 4) for k, v in det.items()} if det else None,
    }
    (out / "grid.json").write_text(json.dumps(grid))
    (out / "index.html").write_text(HTML.replace("__NAME__", title))
    say(f"  wrote {out/'index.html'}  ({n_beats} beats, {n_beats//4} bars, "
        f"beat {beat:.6f}s, phase {ph_disp:.4f}s)", quiet)
    return out / "index.html"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a beat-grid cut picker for a song.")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--bpm", type=float, help="skip detection, use this tempo")
    ap.add_argument("--phase", type=float,
                    help="seconds from the start of the file to beat 0 "
                         "(with --offset, from the start of the edit)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="where the edit's audio starts in the file; times are "
                         "displayed and exported relative to it")
    ap.add_argument("--range", type=float, nargs=2, metavar=("LO", "HI"),
                    default=[BPM_LO, BPM_HI], help="tempo search range")
    ap.add_argument("--duration", type=float,
                    help="only grid this many seconds from --offset "
                         "(default: to the end of the file)")
    ap.add_argument("--fps", type=float, default=60.0, help="project frame rate")
    ap.add_argument("--out", type=Path, help="output dir (default ./browser/<stem>)")
    ap.add_argument("--title")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    if not a.audio.exists():
        print(f"no such audio: {a.audio}", file=sys.stderr)
        return 1
    out = a.out or Path("browser") / a.audio.stem
    try:
        build(a.audio, out, bpm=a.bpm, phase=a.phase, offset=a.offset,
              fps=a.fps, rng=tuple(a.range), duration=a.duration,
              title=a.title, quiet=a.quiet)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


HTML = r"""<!doctype html><meta charset=utf-8>
<title>__NAME__ - beat grid</title>
<style>
:root{--bg:#0e0e11;--fg:#e8e8ec;--dim:#83838f;--star:#ffcf3f;--line:#26262e;
      --blue:#5b8cff;--hold:#4bc08a;--burst:#ff6b5b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.45 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:20;background:rgba(14,14,17,.97);
       backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
       padding:12px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.02em}
.count{color:var(--dim);font-variant-numeric:tabular-nums}
.count b{color:var(--star)}
button{background:#1c1c22;color:var(--fg);border:1px solid var(--line);
       border-radius:7px;padding:7px 13px;font:inherit;font-size:13px;cursor:pointer}
button:hover{background:#262630;border-color:#3a3a46}
button.pri{background:var(--star);color:#1a1400;border-color:var(--star);font-weight:600}
button.on{background:var(--blue);border-color:var(--blue);color:#fff}
.hint{color:var(--dim);font-size:12px;margin-left:auto;text-align:right;max-width:42ch}
#bar2{position:sticky;top:53px;z-index:19;display:flex;gap:8px;align-items:center;
      flex-wrap:wrap;background:#08080a;border-bottom:1px solid var(--line);padding:9px 20px}
#bar2 .lbl{color:var(--dim);font-size:12px}
.sp{font-variant-numeric:tabular-nums;padding:6px 10px;font-size:12px}
#warn{color:#1a1400;background:var(--burst);padding:6px 20px;font-size:12.5px;display:none}
#warn.on{display:block}
#wrap{padding:14px 20px 120px}
canvas{display:block;cursor:crosshair}
#panel{position:fixed;left:0;right:0;bottom:0;z-index:25;background:rgba(8,8,10,.97);
       backdrop-filter:blur(8px);border-top:1px solid var(--line);padding:10px 20px;
       display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:13px}
#panel .k{color:var(--dim)}
#panel b{font-variant-numeric:tabular-nums}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}
</style>
<header>
  <h1>__NAME__</h1>
  <span class=count id=grid></span>
  <span class=count><b id=nreg>0</b> regions &middot; <b id=nslot>0</b> cuts</span>
  <button id=exp class=pri>Export cuts</button>
  <button id=clr>Clear</button>
  <span class=hint>drag across beats to paint a region &middot; <b>1-6</b> sets what it holds &middot; <b>space</b> plays &middot; <b>enter</b> plays the region</span>
</header>
<div id=bar2>
  <span class=lbl>a region is</span>
  <span id=spacings></span>
  <button id=play>&#9654; play</button>
  <button id=stop>stop</button>
  <span class=lbl id=pos>0.000s &middot; beat 0</span>
</div>
<div id=warn></div>
<div id=wrap><canvas id=cv></canvas></div>
<div id=panel><span class=k>no region selected &mdash; drag across the grid to make one</span></div>
<script>
const NAME='__NAME__', KEY='beatgrid:'+NAME;
const BPR=16, GUT=54, PADR=18, WAVE=56, BAND=20, ROWH=WAVE+BAND+16;
// spacing 0 = the whole region is one clip: drag a start pin to an end pin and
// that span is a single cut. The subdivisions exist because a fast passage is
// hundreds of cuts nobody wants to pin by hand.
const SPACINGS=[0,4,2,1,.5,.25], SPNAME={0:'1 clip',4:'4 beats',2:'2 beats',
  1:'1 beat',.5:'1/2 beat',.25:'1/4 beat'};
let G=null, env=[], regions=[], sel=-1, spacing=0, drag=null, rows=0, W=0;
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
const audio=new Audio(); audio.preload='auto';
let stopAt=null;

const tOf=b=>G.phase+b*G.beat;                 // display time of a beat
const bOf=t=>(t-G.phase)/G.beat;
const kindOf=s=>(s===0||s>=1)?'hold':'burst';
const stepOf=r=>r.spacing||(r.b1-r.b0);
const fmt=t=>t.toFixed(3)+'s';

function save(){localStorage.setItem(KEY,JSON.stringify(regions))}

/* ---- layout ---------------------------------------------------------- */
function resize(){
  // Observed, not read once: booting into a pane that had not laid out yet gave
  // clientWidth 40, and the whole grid drew into a 40px canvas until something
  // happened to fire a window resize.
  const w=document.getElementById('wrap').clientWidth;
  if(w<200) return;
  W=w;
  rows=Math.max(1,Math.ceil(G.beats/BPR));
  const dpr=devicePixelRatio||1;
  cv.width=W*dpr; cv.height=rows*ROWH*dpr;
  cv.style.width=W+'px'; cv.style.height=rows*ROWH+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  draw();
}
const usable=()=>W-GUT-PADR;
const xOf=b=>GUT+((b%BPR)/BPR)*usable();
const yOf=b=>Math.floor(b/BPR)*ROWH;

function beatAt(px,py){
  const r=Math.floor(py/ROWH); if(r<0||r>=rows) return null;
  let f=(px-GUT)/usable(); f=Math.max(0,Math.min(1,f));
  return Math.max(0,Math.min(G.beats,Math.round(r*BPR+f*BPR)));
}

/* ---- drawing --------------------------------------------------------- */
function draw(){
  ctx.clearRect(0,0,W,rows*ROWH);
  for(let r=0;r<rows;r++) drawRow(r);
  drawRegions(); drawPending(); drawHead();
}

function drawRow(r){
  const y=r*ROWH, b0=r*BPR, mid=y+WAVE/2, u=usable();
  // waveform: a landmark, not a target
  ctx.fillStyle='#3b3b48';
  for(let px=0;px<u;px++){
    const b=b0+(px/u)*BPR; if(b>G.beats) break;
    const i=Math.floor(tOf(b)*G.env_hz+G.offset*G.env_hz);
    const v=(env[i]||0)/255, h=Math.max(1,v*(WAVE/2-2));
    ctx.fillRect(GUT+px,mid-h,1,h*2);
  }
  // beat ticks
  for(let b=b0;b<=Math.min(b0+BPR,G.beats);b++){
    const x=b===b0+BPR?GUT+u:xOf(b), bar=b%4===0;
    ctx.strokeStyle=bar?'#6f6f85':'#33333f'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x+.5,y+2); ctx.lineTo(x+.5,y+WAVE+BAND); ctx.stroke();
    if(bar&&b<G.beats){
      ctx.fillStyle='#7b7b8c'; ctx.font='10px ui-monospace,monospace';
      ctx.fillText((b/4+1),x+3,y+WAVE+BAND+12);
    }
  }
  ctx.fillStyle='#5c5c6b'; ctx.font='11px ui-monospace,monospace';
  ctx.fillText('b'+b0,6,y+WAVE/2+4);
  ctx.fillStyle='#3a3a46';
  ctx.fillText(tOf(b0).toFixed(1)+'s',6,y+WAVE/2+18);
}

function bandRects(b0,b1,cb){          // a beat span can wrap across rows
  for(let b=b0;b<b1;){
    const rowEnd=Math.min(b1,(Math.floor(b/BPR)+1)*BPR);
    cb(xOf(b),yOf(b),(rowEnd-b)/BPR*usable(),b,rowEnd);
    b=rowEnd;
  }
}

function drawRegions(){
  regions.forEach((rg,i)=>{
    const c=kindOf(rg.spacing)==='hold'?'#4bc08a':'#ff6b5b', on=i===sel;
    bandRects(rg.b0,rg.b1,(x,y,w)=>{
      ctx.fillStyle=c+'22'; ctx.fillRect(x,y,w,WAVE);
      ctx.fillStyle=c; ctx.fillRect(x,y+WAVE+3,w,BAND-6);
      if(on){ctx.strokeStyle='#fff';ctx.lineWidth=1;
             ctx.strokeRect(x+.5,y+.5,w-1,WAVE+BAND-1)}
    });
    // cut boundaries inside the region
    ctx.strokeStyle='rgba(8,8,10,.85)'; ctx.lineWidth=1;
    const st=stepOf(rg);
    for(let b=rg.b0+st;b<rg.b1-1e-6;b+=st){
      const x=xOf(b),y=yOf(b);
      ctx.beginPath(); ctx.moveTo(x+.5,y+WAVE+3); ctx.lineTo(x+.5,y+WAVE+BAND-3); ctx.stroke();
    }
    const lx=xOf(rg.b0)+4, ly=yOf(rg.b0)+WAVE+BAND-7;
    ctx.fillStyle='#08080a'; ctx.font='600 10px ui-monospace,monospace';
    ctx.fillText(rg.spacing?kindOf(rg.spacing)+' '+SPNAME[rg.spacing]
                             :'1 clip · '+(stepOf(rg)*G.beat).toFixed(2)+'s',lx,ly);
  });
}

function drawPending(){
  if(!drag||drag.b1===drag.b0) return;
  const [a,b]=[Math.min(drag.b0,drag.b1),Math.max(drag.b0,drag.b1)];
  bandRects(a,b,(x,y,w)=>{ctx.strokeStyle=var_blue;ctx.lineWidth=2;
    ctx.strokeRect(x+1,y+1,w-2,WAVE+BAND-2)});
}
const var_blue='#5b8cff';

function drawHead(){
  const t=audio.currentTime-G.offset; if(t<0) return;
  const b=bOf(t); if(b<0||b>G.beats) return;
  const x=GUT+((b%BPR)/BPR)*usable(), y=Math.floor(b/BPR)*ROWH;
  ctx.strokeStyle='#fff'; ctx.lineWidth=1.5;
  ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x,y+WAVE+BAND); ctx.stroke();
}

/* ---- regions --------------------------------------------------------- */
function addRegion(b0,b1){
  if(b1<=b0) return;
  regions=regions.flatMap(r=>{            // painting over a region replaces it
    if(r.b1<=b0||r.b0>=b1) return [r];
    const out=[];
    if(r.b0<b0) out.push({...r,b1:b0});
    if(r.b1>b1) out.push({...r,b0:b1});
    return out;
  });
  regions.push({b0,b1,spacing});
  regions.sort((a,b)=>a.b0-b.b0);
  sel=regions.findIndex(r=>r.b0===b0&&r.b1===b1);
  commit();
}
function slotsOf(rg){return Math.max(1,Math.round((rg.b1-rg.b0)/stepOf(rg)))}
function commit(){save();draw();paint()}

function paint(){
  const n=regions.reduce((a,r)=>a+slotsOf(r),0);
  document.getElementById('nreg').textContent=regions.length;
  document.getElementById('nslot').textContent=n;
  const p=document.getElementById('panel');
  if(sel<0||!regions[sel]){
    p.innerHTML='<span class=k>no region selected &mdash; drag across the grid to make one</span>';
    return;
  }
  const r=regions[sel], k=kindOf(r.spacing), c=k==='hold'?'#4bc08a':'#ff6b5b';
  p.innerHTML=`<span><i class=dot style="background:${c}"></i><b>${k}</b></span>
    <span class=k>bars</span> <b>${(r.b0/4+1).toFixed(2).replace('.00','')}
      &ndash; ${(r.b1/4+1).toFixed(2).replace('.00','')}</b>
    <span class=k>beats</span> <b>${r.b0}&ndash;${r.b1} (${r.b1-r.b0})</b>
    <span class=k>time</span> <b>${fmt(tOf(r.b0))} &ndash; ${fmt(tOf(r.b1))}</b>
    <span class=k>${r.spacing?'cut every':'one clip of'}</span>
      <b>${SPNAME[r.spacing]} = ${(stepOf(r)*G.beat*1000).toFixed(0)}ms</b>
    <span class=k>clips</span> <b>${slotsOf(r)}</b>
    <button id=pr>&#9654; play region</button>
    <button id=del>delete</button>`;
  document.getElementById('pr').onclick=()=>playRange(tOf(r.b0),tOf(r.b1));
  document.getElementById('del').onclick=()=>{regions.splice(sel,1);sel=-1;commit()};
}

/* ---- audio ----------------------------------------------------------- */
function seek(t){audio.currentTime=Math.max(0,t+G.offset)}
function playRange(t0,t1){stopAt=t1;seek(t0);audio.play()}
function toggle(){if(audio.paused){stopAt=null;audio.play()}else audio.pause()}
function tick(){
  const t=audio.currentTime-G.offset;
  document.getElementById('pos').textContent=
    fmt(Math.max(0,t))+' · beat '+Math.max(0,Math.floor(bOf(t)));
  if(stopAt!==null&&t>=stopAt){audio.pause();stopAt=null}
  if(!audio.paused) draw();
  requestAnimationFrame(tick);
}

/* ---- input ----------------------------------------------------------- */
function xy(e){const r=cv.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]}
cv.addEventListener('mousedown',e=>{
  const [x,y]=xy(e), b=beatAt(x,y); if(b===null) return;
  drag={b0:b,b1:b,moved:false}; e.preventDefault();
});
addEventListener('mousemove',e=>{
  if(!drag) return;
  const [x,y]=xy(e), b=beatAt(x,y); if(b===null) return;
  if(b!==drag.b0) drag.moved=true;
  drag.b1=b; draw();
});
addEventListener('mouseup',e=>{
  if(!drag) return;
  const d=drag; drag=null;
  if(d.moved){addRegion(Math.min(d.b0,d.b1),Math.max(d.b0,d.b1)); return}
  const hit=regions.findIndex(r=>d.b0>=r.b0&&d.b0<r.b1);
  if(hit>=0){sel=hit;commit()} else {sel=-1;seek(tOf(d.b0));commit()}
});
addEventListener('keydown',e=>{
  if(e.key===' '){e.preventDefault();toggle();return}
  if(e.key==='Enter'&&regions[sel]){playRange(tOf(regions[sel].b0),tOf(regions[sel].b1));return}
  if((e.key==='Backspace'||e.key==='Delete')&&regions[sel]){
    e.preventDefault();regions.splice(sel,1);sel=-1;commit();return}
  const i=SPACINGS.indexOf(SPACINGS[+e.key-1]);
  if(+e.key>=1&&+e.key<=SPACINGS.length){setSpacing(SPACINGS[+e.key-1])}
});
function setSpacing(s){
  spacing=s;
  if(regions[sel]) regions[sel].spacing=s;
  [...document.querySelectorAll('#spacings button')].forEach(b=>
    b.classList.toggle('on',+b.dataset.s===s));
  commit();
}

/* ---- export ---------------------------------------------------------- */
function build(){
  const f=G.fps, snap=t=>Math.round(t*f)/f, slots=[];
  regions.forEach((r,ri)=>{
    const n=slotsOf(r);
    for(let i=0;i<n;i++){
      const st=r.spacing||(r.b1-r.b0);
      const a=snap(tOf(r.b0+i*st)), b=snap(tOf(r.b0+(i+1)*st));
      slots.push({i:slots.length, region:ri, kind:kindOf(r.spacing),
                  beat:r.b0+i*st, beats:st,
                  start:+a.toFixed(6), dur:+(b-a).toFixed(6)});
    }
  });
  return {song:NAME, bpm:G.bpm, beat:G.beat, phase:G.phase, offset:G.offset, fps:f,
          regions:regions.map((r,i)=>({i, beat0:r.b0, beat1:r.b1,
            spacing:r.spacing||(r.b1-r.b0),
            kind:kindOf(r.spacing), start:+tOf(r.b0).toFixed(6),
            end:+tOf(r.b1).toFixed(6), cuts:slotsOf(r)})),
          cuts:slots.length, slots};
}
document.getElementById('exp').onclick=()=>{
  const blob=new Blob([JSON.stringify(build(),null,1)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='cuts_'+NAME+'.json'; a.click();
};
document.getElementById('clr').onclick=()=>{
  if(confirm('Clear all regions?')){regions=[];sel=-1;commit()}};
document.getElementById('play').onclick=toggle;
document.getElementById('stop').onclick=()=>{audio.pause();seek(0)};

/* ---- boot ------------------------------------------------------------ */
fetch('grid.json').then(r=>r.json()).then(g=>{
  G=g; env=g.env; audio.src=g.audio;
  regions=JSON.parse(localStorage.getItem(KEY)||'[]');
  document.getElementById('grid').textContent=
    `${g.bpm.toFixed(2)} BPM · beat ${g.beat.toFixed(6)}s · `+
    `${g.beats} beats / ${Math.floor(g.beats/4)} bars`;
  const d=g.detected;
  if(d&&(Math.abs(d.bpm-d.bpm_autocorr)>1.5||d.ac_err>=0.06)){
    const w=document.getElementById('warn'); w.classList.add('on');
    w.textContent=`tempo unconfirmed: the grid search says ${d.bpm} BPM, but the `+
      `strongest period in the track is ${d.ac_period.toFixed(4)}s = `+
      `${(d.ac_period/g.beat).toFixed(2)} beats, which is not a whole number of them `+
      `(${d.bpm_autocorr.toFixed(2)} BPM implied). Check by ear before painting `+
      `regions, or pass --bpm.`;
  }
  document.getElementById('spacings').innerHTML=SPACINGS.map(s=>
    `<button class="sp" data-s="${s}">${SPNAME[s]}</button>`).join(' ');
  [...document.querySelectorAll('#spacings button')].forEach(b=>
    b.onclick=()=>setSpacing(+b.dataset.s));
  setSpacing(spacing);
  resize(); paint(); tick();
});
new ResizeObserver(()=>{if(G)resize()}).observe(document.getElementById('wrap'));
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
