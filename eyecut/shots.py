"""Build a browsable shot picker for a source file.

    python -m eyecut.shots FOOTAGE.mp4 --windows shots.txt --out ./browser/name

Writes `index.html` plus a poster and a short hover-preview per shot, and a
full-length scrub proxy the page seeks into. Open the page, click shots to star
them, shift-click to take a whole run, then Export to get a JSON list of spans.

Two deliberate design decisions, both learned the hard way:

* **No quality scoring or sorting.** Shots are listed in chronological order.
  Four automatic clip-selection metrics were tried on real footage -- drawing
  density, frame-change count, frame interpolation, and motion-snapped selection
  -- and every one of them lost to a human looking at the clips. The tool's job is
  to make picking fast, not to pick.
* **Previews play the first seconds at true speed**, rather than sampling across
  the whole shot. Sampling made anything longer than the cap play faster than
  life, which reads as wrong motion and makes good shots look bad.

Consecutive windows are assumed to tile the source, so shift-clicking a run
yields one continuous span. `eyecut.static_server` serves the result; the plain
`http.server` cannot, because it ignores Range requests and `<video>` needs them
to seek.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PREV_FPS = 24       # preview playback rate
PREV_MAXS = 3.0     # cap a preview at this many seconds of source
THUMB_W = 320       # poster/preview width; 16:9 -> 320x180
PROXY_W = 640       # scrub proxy width


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(" ".join(str(c) for c in cmd[:6]) + "\n" + p.stderr[-800:])
    return p


def probe(path: Path) -> tuple[float, float]:
    """(fps, duration_seconds) of a video file."""
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=r_frame_rate,duration", "-of", "json", path]).stdout
    st = json.loads(out)["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    return float(num) / float(den), float(st["duration"])


def read_windows(path: Path) -> list[tuple[float, float]]:
    """Shot windows, one `start end` pair of seconds per line."""
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            a, b = (float(x) for x in line.split()[:2])
            out.append((a, b))
    return out


def build(source: Path, windows: list[tuple[float, float]], out: Path,
          *, title: str | None = None, quiet: bool = False) -> Path:
    """Render the picker for `source` into `out`. Returns the index.html path."""
    source, out = Path(source), Path(out)
    title = title or source.stem
    say = (lambda *a, **k: None) if quiet else print

    fps, dur = probe(source)
    say(f"{title}: {len(windows)} shots, {dur:.1f}s @ {fps:.3f} fps")

    media = out / "media"
    shutil.rmtree(out, ignore_errors=True)
    media.mkdir(parents=True)

    # One decode pass to small jpgs; posters and previews both come from these.
    tmp = out / ".frames"
    tmp.mkdir()
    say("decoding...", flush=True)
    run(["ffmpeg", "-v", "error", "-i", source, "-vf", f"scale={THUMB_W}:-2",
         "-q:v", "5", tmp / "%06d.jpg", "-y"])
    nframes = len(list(tmp.iterdir()))

    def frame(i: int) -> Path:
        return tmp / ("%06d.jpg" % max(1, min(nframes, i)))

    meta = []
    for idx, (a, b) in enumerate(windows):
        length = b - a
        shutil.copyfile(frame(int(round((a + length / 2) * fps)) + 1),
                        media / ("%03d.jpg" % idx))

        span = min(length, PREV_MAXS)
        n = max(2, int(round(span * PREV_FPS)))
        seqd = out / ".seq"
        shutil.rmtree(seqd, ignore_errors=True)
        seqd.mkdir()
        for k in range(n):
            os.link(frame(int(round((a + span * k / n) * fps)) + 1),
                    seqd / ("%05d.jpg" % (k + 1)))
        # Mux the matching stretch of source audio onto the frame sequence --
        # hearing a shot is half of recognising it.
        run(["ffmpeg", "-v", "error",
             "-framerate", PREV_FPS, "-i", seqd / "%05d.jpg",
             "-ss", a, "-t", span, "-i", source,
             "-map", "0:v:0", "-map", "1:a:0?", "-shortest",
             "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
             "-movflags", "+faststart", media / ("%03d.mp4" % idx), "-y"])
        shutil.rmtree(seqd, ignore_errors=True)
        meta.append({"i": idx, "start": round(a, 3), "end": round(b, 3),
                     "dur": round(length, 3)})
        if not quiet and (idx + 1) % 20 == 0:
            say(f"  {idx + 1}/{len(windows)}", flush=True)

    shutil.rmtree(tmp, ignore_errors=True)

    say("building scrub proxy...", flush=True)
    run(["ffmpeg", "-v", "error", "-i", source, "-vf", f"scale={PROXY_W}:-2",
         "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-g", "30",
         "-c:a", "aac", "-b:a", "96k", "-ac", "2",
         "-movflags", "+faststart", out / "proxy.mp4", "-y"])

    (out / "shots.json").write_text(json.dumps(meta))
    index = out / "index.html"
    index.write_text(HTML.replace("__NAME__", title))
    say(f"wrote {index}  ({len(meta)} shots)")
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a shot picker for a video file.")
    ap.add_argument("source", type=Path)
    ap.add_argument("--windows", type=Path,
                    help="shot windows file ('start end' seconds per line). "
                         "Defaults to <source-dir>/free_<stem>.txt")
    ap.add_argument("--out", type=Path, help="output dir (default ./browser/<stem>)")
    ap.add_argument("--title", help="label shown in the page header")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    if not a.source.exists():
        print(f"no such source: {a.source}", file=sys.stderr)
        return 1
    win = a.windows or a.source.parent / f"free_{a.source.stem}.txt"
    if not Path(win).exists():
        print(f"no windows file: {win}\n"
              f"one 'start end' pair of seconds per line, one line per shot",
              file=sys.stderr)
        return 1
    out = a.out or Path("browser") / a.source.stem
    build(a.source, read_windows(win), out, title=a.title, quiet=a.quiet)
    return 0


HTML = r"""<!doctype html><meta charset=utf-8>
<title>__NAME__ - shot browser</title>
<style>
:root{--bg:#0e0e11;--fg:#e8e8ec;--dim:#83838f;--star:#ffcf3f;--line:#26262e;--blue:#5b8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.45 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:20;background:rgba(14,14,17,.97);
       backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
       padding:12px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.02em}
.count{color:var(--dim);font-variant-numeric:tabular-nums}
.count b{color:var(--star)}
button{background:#1c1c22;color:var(--fg);border:1px solid var(--line);
       border-radius:7px;padding:7px 13px;font:inherit;font-size:13px;cursor:pointer}
button:hover{background:#262630;border-color:#3a3a46}
button.pri{background:var(--star);color:#1a1400;border-color:var(--star);font-weight:600}
.hint{color:var(--dim);font-size:12px;margin-left:auto;text-align:right}
#player{position:sticky;top:53px;z-index:15;display:none;gap:14px;
        background:#08080a;border-bottom:1px solid var(--line);padding:12px 20px}
#player.on{display:flex}
#player video{width:340px;aspect-ratio:16/9;background:#000;border-radius:8px}
.pinfo{font-size:13px;color:var(--dim);display:flex;flex-direction:column;gap:6px;
       justify-content:center}
.pinfo b{color:var(--fg);font-size:14px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
      gap:12px;padding:16px 20px 90px}
.card{position:relative;border:2px solid transparent;border-radius:10px;
      overflow:hidden;background:#000;cursor:pointer;line-height:0}
.card img,.card .pv{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}
.card .pv{position:absolute;inset:0;opacity:0;transition:opacity .12s}
.card:hover .pv{opacity:1}
.card.on{border-color:var(--star)}
.card.on .tag{display:block}
.tag{display:none;position:absolute;top:6px;right:7px;background:var(--star);
     color:#1a1400;font-size:10px;font-weight:700;letter-spacing:.04em;
     padding:2px 6px;border-radius:5px;line-height:1.4}
.play{position:absolute;top:6px;left:7px;background:rgba(0,0,0,.72);color:#fff;
      border:0;border-radius:5px;padding:2px 7px;font-size:11px;line-height:1.5;
      opacity:0;cursor:pointer}
.card:hover .play{opacity:1}
.play:hover{background:var(--blue)}
.lab{position:absolute;left:0;bottom:0;right:0;padding:4px 7px;line-height:1.3;
     font-size:11px;font-variant-numeric:tabular-nums;color:#cfcfd8;
     background:linear-gradient(transparent,rgba(0,0,0,.85));display:flex;gap:8px}
.lab .n{color:var(--dim)}
.card.cur{outline:2px solid var(--blue);outline-offset:2px}
</style>
<header>
  <h1>__NAME__</h1>
  <span class=count><b id=nsel>0</b> shots in <b id=nscene>0</b> scenes</span>
  <button id=exp class=pri>Export picks</button>
  <button id=only>Show starred</button>
  <button id=clr>Clear</button>
  <button id=mute>&#128266; hover sound: on</button>
  <span class=hint>click to star &middot; <b>shift-click to extend</b> (stars or un-stars a run) &middot; &#9654; to watch <b>with sound</b></span>
</header>
<div id=player>
  <video id=vid preload=metadata playsinline controls></video>
  <div class=pinfo>
    <b id=ptitle></b>
    <span id=pmeta></span>
    <span><button id=pclose>close</button> <button id=pstar>star this scene</button></span>
  </div>
</div>
<div id=grid></div>
<script>
const NAME='__NAME__', KEY='picks:'+NAME;
let hoverMuted=localStorage.getItem('hovermute')==='1';
let shots=[], sel=new Set(JSON.parse(localStorage.getItem(KEY)||'[]')),
    cur=0, anchor=null, anchorAdds=true, onlyStarred=false, span=null;
const tc=s=>{const m=Math.floor(s/60),r=(s%60).toFixed(2).padStart(5,'0');return m+':'+r};
const pad=i=>String(i).padStart(3,'0');

fetch('shots.json').then(r=>r.json()).then(d=>{shots=d;draw();});

/* consecutive starred shots collapse into one scene */
function scenes(){
  const ids=[...sel].sort((a,b)=>a-b), out=[];
  for(const i of ids){
    const last=out[out.length-1];
    if(last && i===last.shots[last.shots.length-1]+1) last.shots.push(i);
    else out.push({shots:[i]});
  }
  return out.map((s,n)=>{
    const a=shots[s.shots[0]], b=shots[s.shots[s.shots.length-1]];
    return {scene:n+1, start:a.start, end:b.end,
            dur:+(b.end-a.start).toFixed(3), shots:s.shots};
  });
}
function sceneOf(i){return scenes().find(s=>s.shots.includes(i))}

function draw(){
  const g=document.getElementById('grid'); g.innerHTML='';
  shots.forEach(s=>{
    if(onlyStarred && !sel.has(s.i)) return;
    const c=document.createElement('div');
    c.className='card'+(sel.has(s.i)?' on':'')+(s.i===cur?' cur':'');
    c.dataset.i=s.i;
    c.innerHTML=`<img src="media/${pad(s.i)}.jpg">
      <video class=pv loop playsinline preload=none src="media/${pad(s.i)}.mp4"></video>
      <button class=play>&#9654;</button><span class=tag></span>
      <div class=lab><span class=n>#${s.i}</span><span>${tc(s.start)}</span><span>${s.dur.toFixed(2)}s</span></div>`;
    c.onmouseenter=()=>{const v=c.querySelector('.pv');
      document.querySelectorAll('.pv').forEach(o=>{if(o!==v){o.pause();o.muted=true}});
      v.currentTime=0; v.muted=hoverMuted; v.volume=1;
      v.play().catch(()=>{v.muted=true;v.play().catch(()=>{})});};
    c.onmouseleave=()=>{const v=c.querySelector('.pv');v.pause();v.muted=true};
    c.querySelector('.play').onclick=e=>{e.stopPropagation();
      play(sceneOf(s.i)||{scene:0,start:s.start,end:s.end,dur:s.dur,shots:[s.i]})};
    c.onclick=e=>{
      if(e.shiftKey && anchor!==null){
        // extend the range doing whatever the last plain click did, so the
        // same gesture that stars a scene also un-stars one
        const a=Math.min(anchor,s.i), b=Math.max(anchor,s.i);
        for(let k=a;k<=b;k++) anchorAdds?sel.add(k):sel.delete(k);
        save();
      } else {
        anchorAdds=!sel.has(s.i);      // remember the direction of this click
        toggle(s.i); anchor=s.i;
      }
      setCur(s.i); paint();
    };
    g.appendChild(c);
  });
  paint();
}
/* repaint stars + scene badges without rebuilding the grid */
function paint(){
  const sc=scenes(), of={};
  sc.forEach(s=>s.shots.forEach((i,k)=>of[i]=
      s.shots.length>1?('S'+s.scene+' '+(k+1)+'/'+s.shots.length):('S'+s.scene)));
  document.querySelectorAll('.card').forEach(c=>{
    const i=+c.dataset.i;
    c.classList.toggle('on',sel.has(i));
    const t=c.querySelector('.tag'); if(t)t.textContent=of[i]||'';
  });
  document.getElementById('nsel').textContent=sel.size;
  document.getElementById('nscene').textContent=sc.length;
}
function save(){localStorage.setItem(KEY,JSON.stringify([...sel]))}
function toggle(i){sel.has(i)?sel.delete(i):sel.add(i);save()}
function card(i){return document.querySelector('.card[data-i="'+i+'"]')}
function setCur(i){const o=card(cur); if(o)o.classList.remove('cur');
  cur=i; const n=card(i); if(n){n.classList.add('cur');
    n.scrollIntoView({block:'nearest',behavior:'smooth'})}}

/* --- player: scrubs the real footage, stops at the end of the span --- */
const vid=document.getElementById('vid');
function play(sp){
  span=sp;
  document.getElementById('player').classList.add('on');
  document.getElementById('ptitle').textContent =
    sp.shots.length>1 ? ('Scene of '+sp.shots.length+' shots  (#'+sp.shots[0]+'-#'+sp.shots[sp.shots.length-1]+')')
                      : ('Shot #'+sp.shots[0]);
  document.getElementById('pmeta').textContent =
    tc(sp.start)+' → '+tc(sp.end)+'   ·   '+sp.dur.toFixed(2)+'s';
  if(!vid.src) vid.src='proxy.mp4';
  const go=()=>{
    vid.currentTime=sp.start;
    vid.muted=false;
    const p=vid.play();
    if(p&&p.catch)p.catch(()=>{           // autoplay policy can refuse sound
      vid.muted=true; vid.play().catch(()=>{});
      document.getElementById('pmeta').textContent+='   ·   click ► to unmute';
    });
  };
  vid.readyState>0?go():vid.addEventListener('loadedmetadata',go,{once:true});
}
vid.addEventListener('timeupdate',()=>{
  if(span && vid.currentTime>=span.end){vid.currentTime=span.start}   // loop the span
});
document.getElementById('pclose').onclick=()=>{
  vid.pause(); document.getElementById('player').classList.remove('on'); span=null};
document.getElementById('pstar').onclick=()=>{
  if(!span)return; span.shots.forEach(i=>sel.add(i)); save(); paint()};

addEventListener('keydown',e=>{
  if(e.target.tagName==='BUTTON')return;
  const vis=shots.filter(s=>!onlyStarred||sel.has(s.i)).map(s=>s.i);
  let p=vis.indexOf(cur); if(p<0)p=0;
  if(e.key==='ArrowRight'){setCur(vis[Math.min(vis.length-1,p+1)]);if(!e.shiftKey)anchor=cur;
    if(e.shiftKey){sel.add(cur);save();paint()}}
  else if(e.key==='ArrowLeft'){setCur(vis[Math.max(0,p-1)]);if(!e.shiftKey)anchor=cur;
    if(e.shiftKey){sel.add(cur);save();paint()}}
  else if(e.key===' '){e.preventDefault();anchorAdds=!sel.has(cur);
    toggle(cur);anchor=cur;paint()}
  else if(e.key==='Enter'){const s=shots[cur];
    play(sceneOf(cur)||{scene:0,start:s.start,end:s.end,dur:s.dur,shots:[cur]})}
});

const mb=document.getElementById('mute');
const paintMute=()=>mb.innerHTML=(hoverMuted?'&#128263; hover sound: off':'&#128266; hover sound: on');
paintMute();
mb.onclick=()=>{hoverMuted=!hoverMuted;localStorage.setItem('hovermute',hoverMuted?'1':'0');
  paintMute(); document.querySelectorAll('.pv').forEach(v=>v.muted=true)};
document.getElementById('only').onclick=e=>{onlyStarred=!onlyStarred;
  e.target.textContent=onlyStarred?'Show all':'Show starred';draw()};
document.getElementById('clr').onclick=()=>{if(confirm('Clear all stars?')){
  sel.clear();anchor=null;save();draw()}};
document.getElementById('exp').onclick=()=>{
  const sc=scenes();
  const blob=new Blob([JSON.stringify(
    {source:NAME, shots:sel.size, scenes:sc.length, picks:sc}, null, 1)],
    {type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='picks_'+NAME+'.json'; a.click();
};
</script>
"""

if __name__ == "__main__":
    raise SystemExit(main())
