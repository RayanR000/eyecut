# eyecut

CapCut support for Claude: Claude does the editing, eyecut writes the files.

The point of this repo is that the hard-won rules about CapCut's draft format live
in code with tests, not in a notes file somebody has to remember to read. Every
check in `eyecut.timeline` stands for a bug that already cost real work once.

## The MCP surface

Two tools. The test is not "is it useful" but "can Claude do it another way":
Claude has a shell, so frame extraction, shot browsing and proxy rendering are a
few lines of ffmpeg and stay CLIs. Writing CapCut's format is what no shell gets
you.

| Tool | What it does |
|---|---|
| `register_media` | Write the entries that stop CapCut prompting to relink. |
| `write_draft` | Compile a spec into a draft that opens ready to adjust. |

## Modules

| Module | What it does |
|---|---|
| `eyecut.timeline` | Edit an existing draft's timeline **in place**, with the rules enforced on save. |
| `eyecut.draft` | Build a draft from a spec via `capcut-cli compile`, then finish what the compiler leaves undone. |
| `eyecut.spec` | Reject a spec compile would accept but silently mis-build. |
| `eyecut.media` | Register media in `draft_meta_info.json` so CapCut does not prompt to relink. |
| `eyecut.frames` | Extract JPEGs and contact sheets from a source, plus what makes it unusable. |
| `eyecut.shots` | Build a browsable shot picker for a source file. |
| `eyecut.proxy` | Render a watchable proxy of a draft without opening CapCut. |
| `eyecut.speech` | Find spoken lines in footage. |
| `eyecut.static_server` | Static server with HTTP Range support, so `<video>` can seek. |
| `eyecut.template` / `probe` | Supporting pieces for the above. |

## The rules `timeline` enforces

These are not style preferences. Each one is a way a draft breaks.

- **Array order is timeline order.** CapCut lays out the main video track by the
  order of `tracks[].segments`, not by `target_timerange.start`. Save them out of
  order and every clip from that point on silently re-times when the project opens.
- **Boundaries land on whole project frames.** Dividing a span into N equal parts
  puts boundaries a fraction of a frame apart; CapCut re-quantizes on save, so a
  run of identical clips opens as an uneven mix of frame counts. Use `snap_us`.
- **Per-segment materials get pruned.** Every segment owns entries in seven other
  `materials` lists. Replace segments without dropping the old entries and the
  file grows every rebuild — one edit accumulated 6,286 orphans.
- **`materials.videos` holds one entry per file, not per segment.** CapCut's own
  re-save expands it; 990 entries for 4 files was most of a 6.3MB draft.
- **CapCut must not be running.** It reads the draft on open, holds it in memory
  for the whole app session, and flushes it back on quit. A write made while it is
  open is invisible and then destroyed. Closing the project is not enough — only
  quitting releases it.

Plus referential integrity: no overlapping segments, no segment pointing at a
material that is not there, and no segment reading past the end of its source.

```python
from eyecut.timeline import Timeline, snap_us

tl = Timeline("MY_PROJECT")
tl.collapse_videos()
tl.replace_segments(my_segments)      # sorts and prunes for you
warnings = tl.save(backup_tag="v1")   # raises unless every fatal check passes
```

`save()` refuses rather than corrupting. `restore(tag)` copies a backup back, which
is what makes a rebuild script safe to re-run: restore, apply, save.

## Writing a draft

Times are in seconds. The one that costs you an afternoon:

- **`start` is the timeline position. `sourceStart` is the in-point into the
  source file.** Three 4-second shots taken from 120s, 300s and 610s of a film
  compiled to a 615-second draft with two long gaps, because each `start` was read
  as a timeline position. `capcut compile --check` accepts an unknown key without
  complaint, so nothing catches the mistake until you watch the result.

```python
items, at = [], 0.0
for start_s, end_s in spans:                 # in-points into the source
    items.append({"path": str(src), "start": at,
                  "duration": end_s - start_s, "sourceStart": start_s})
    at += end_s - start_s                    # where it lands on the timeline

write_draft({"name": "MY_PROJECT", "tracks": [{"type": "video", "items": items}]},
            store / "MY_PROJECT", [probe_media(src)])
```

### What the spec reaches

Everything `capcut compile` takes, because `write_draft` passes the spec straight
through: video, **audio** and **text** tracks, and nine operations targeted at
items by `ref` — `transition`, `filter`, `effect`, `keyframe`, `audio-fade`,
`text-style`, `text-ranges`, `template`, `captions`. eyecut renames nothing, so a
feature capcut-cli gains arrives here for free.

Effects, transitions and filters are named by slug from CapCut's own catalogue —
116 transitions, 345 scene effects, 95 character effects, 76 text intros, 10
filters, 9 masks. List them with `capcut enums --scene-effects` and friends.
Choosing one by name is easy; identifying which one made the flash in someone
else's video is not, and remains out of scope.

Two things `write_draft` finishes after the compile, because compile does not:
**speed** (it writes `segment.speed` but leaves the speed material at 1, and the
app reads the material) and **masks** (compile has no mask operation at all, so
`mask` is the one key eyecut adds to compile's vocabulary — nine shapes, applied
with `capcut mask` and matched to the segment by position, then stamped with the
`constant_material_id` CapCut gives its own masks and `capcut mask` leaves empty).

```python
{"path": src, "start": 0, "duration": 2, "sourceStart": 120, "speed": 2.0}
{"path": src, "start": 2, "duration": 4, "mask": {"slug": "circle", "size": 0.7}}
```

`validate_spec` runs before the compile and rejects what compile accepts but
mis-builds. Each rule below is a mistake that cost a real debugging session:

| Rule | What goes wrong without it |
|---|---|
| Keyframes are one operation per point, each with `time` and `value` | `from`/`to` compiles, lints clean, and writes `time_offset: null, values: [null]` — an animation that does nothing |
| A whole-frame zoom is `uniform_scale` | `scale` is not one of the 11 property names |
| Easings are hyphenated: `ease-in-out` | `ease_in_out` is rejected, but only after the draft directory exists |
| `audio-fade` targets an audio item | Same: a failed build that leaves an orphan folder |
| `text-style` is refused outright | It crashes capcut-cli 0.21.1: `Cannot read properties of undefined (reading 'alpha')`. Set `fontSize` and `color` on the text item instead |
| `filter`/`effect` need `start`, `duration` and `slug`, and take no `target` | A missing duration writes `target_timerange.duration: null`, which nulls the whole draft's duration and breaks reading it back |
| `intensity` is 0–1 | Written verbatim: `5.0` lands in the draft as five times what the CapCut UI can express |
| Template text does not resize to fit | The template keeps the font size it was designed at; a much longer line runs off both edges of the frame |
| Every path must be absolute | compile resolves relative paths against the spec file, which eyecut writes into the drafts store — the error then names a path you never wrote |
| Two tracks of one type need distinct `name`s | compile keys a built track on (type, name), so unnamed tracks merge — a base clip and an overlay land on the same track on top of each other, and lint calls it clean |
| Items on one track may not overlap | The detectable half of the `start`/`sourceStart` mistake above |

```python
spec = {"name": "MY_PROJECT", "tracks": [
    {"type": "video", "items": [
        {"path": src, "start": 0, "duration": 4, "sourceStart": 120, "ref": "shot0"}]},
    {"type": "audio", "items": [
        {"path": music, "start": 0, "duration": 4, "volume": 0.25, "ref": "bed"}]},
    {"type": "text", "items": [
        {"text": "TITLE", "start": 0, "duration": 3, "fontSize": 24, "color": "#FFD700"}]}],
  "operations": [
    {"op": "transition", "target": "shot0", "slug": "dissolve", "duration": 0.5},
    {"op": "keyframe", "target": "shot0", "property": "uniform_scale", "time": 0, "value": 1.0},
    {"op": "keyframe", "target": "shot0", "property": "uniform_scale", "time": 4, "value": 1.15,
     "easing": "ease-in-out"},
    {"op": "audio-fade", "target": "bed", "fadeIn": 0.2, "fadeOut": 0.8}]}
```

## Command line

```bash
eyecut-shots FOOTAGE.mp4 --windows shots.txt --out browser/name
eyecut-serve --root browser --port 8731
eyecut-proxy MY_PROJECT -o preview.mp4
eyecut-speech FOOTAGE.mp4 --windows shots.txt
```

## What is deliberately *not* here

**Clip selection.** No scoring, ranking, or automatic picking of which shots to
use. Four metrics were tried on real footage — drawing density, frame-change
count, frame interpolation, and motion-snapped selection — and each one scored
well and then lost to a human looking at the clips. At the clip lengths these
edits use, the viewer perceives a still image anyway, so "does the drawing change
inside this clip" measures something nobody can see, and chasing it selects the
smear frames. `eyecut.shots` therefore lists shots in chronological order and
makes picking fast instead of making it automatic.

**Frame interpolation.** It wins every measurement and reads as AI-generated.

## Tests

```bash
python -m pytest tests/ -q
```

Tests are written from the failure, not the code: each builds the draft shape that
broke a real edit and asserts the save is refused.
