# eyecut

CapCut support for Claude: Claude does the editing, eyecut writes the files.

The point of this repo is that the hard-won rules about CapCut's draft format live
in code with tests, not in a notes file somebody has to remember to read. Every
check in `eyecut.timeline` stands for a bug that already cost real work once.

## Modules

| Module | What it does |
|---|---|
| `eyecut.timeline` | Edit an existing draft's timeline **in place**, with the rules enforced on save. |
| `eyecut.draft` | Build a draft from a spec via `capcut-cli compile`, then finish what the compiler leaves undone. |
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
