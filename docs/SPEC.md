# eyecut — spec

*CapCut support for Claude. Claude does the editing; eyecut writes the files.*

Rewritten 2026-08-31, narrowing an earlier spec that described a five-stage
autonomous pipeline. **[proven]** marks a claim tested on real footage;
**[untested]** means the pieces exist but the whole was never run.

---

## What it does

Claude can already look at footage and have an opinion about it. What it cannot do
is put that opinion into CapCut. eyecut is the layer that closes that gap: an MCP
server that registers media, writes a draft, and hands back a project that opens
ready to adjust.

The editing decisions — which shot, where, how long — happen in the conversation,
with the user's taste in the loop. That is not a limitation to engineer away.
Algorithmic clip selection was tried twice in the prototype and rejected both
times; the user's judgment was the necessary signal **[proven]**.

## What it is not

- **Not an editing pipeline.** No `scan`, no `arrange`, no shot-description
  database. Claude has vision; batching frames through a separate model to produce
  descriptions Claude then reads is a reimplementation of Claude as a cron job.
  See *Out of scope* below.
- Not a renderer. CapCut renders; eyecut writes the project.
- Not a CapCut draft library. `capcut-cli` does that well and eyecut depends on it.

---

## The MCP surface

Five tools. Everything else is Claude.

### `register_media(project, paths)`

**The reason this project exists.** Writes entries into `draft_meta_info.json` →
`draft_materials`. Without them CapCut 9.x prompts to relink every clip, which
makes a generated draft worse than useless.

No other generator does this, verified 2026-08-31 against both candidates:
`capcut-cli` 0.21.1 puts the write **deliberately out of scope** for want of a
captured entry shape (`dist/store.js`, `assessMediaRegistrationRaw`) and only
observes the three empty states via `diagnose`/`lint`; VectCutAPI ships a template
with every `draft_materials` group empty and no code that ever populates it.

The entry shape is captured in `tests/fixtures/draft_materials_entries.json` from
CapCut-authored projects — note `file_Path` (capital P), `metetype` (misspelled),
microsecond durations, and that every real entry sat in the `type: 0` group
regardless of `metetype` **[proven]**.

Upstreaming is the better end state: `capcut fixture <project> --out <dir>` on a
CapCut-authored draft produces exactly the evidence bundle `capcut-cli` says it is
missing. Contributing it could move this tool out of eyecut entirely.

### `write_draft(spec)`

Delegates to `capcut-cli compile` (JSON spec in, draft out), then calls
`register_media` on every source the spec references, then registers the project
in `root_meta_info.json` — `capcut-cli` covers that last part correctly (adds the
entry, creates the file if absent, never rewrites the whole index), so delegate it
**[proven]**.

**Integer microseconds throughout.** Float seconds cause 1µs phantom overlaps that
the draft layer rejects **[proven failure]**.

### `extract_frames(path, times | every)`

ffmpeg wrapper that writes JPEGs Claude looks at directly. This is how eyecut
"sees" footage — Claude reads the frames in the conversation. Optionally tiles
them into contact sheets (~35 rows, 3 frames each), which is what makes reading a
long source affordable **[proven]**.

Also reports what would make a source unusable before anyone spends time on it:
fps ≠ 24 on anime means frame-interpolated (every "clips for editing" pack online
is), codec AV1 means CapCut cannot read it, and watermarks cluster in the first and
last seconds **[proven]** — all three were hit in one session.

Scene boundaries are available (`ffmpeg scdet`, permissive threshold ~3.0) as a
convenience for "show me one frame per shot". They are a sampling aid, not a
detector: see *Out of scope*.

### `music_grid(path)`

The one piece of real judgment that belongs in code rather than in the
conversation, because it is DSP and Claude cannot hear.

- Tempo via `librosa.beat.beat_track`, with a second method for cross-check
- **Report octave ambiguity rather than resolving it.** 68 vs 136 BPM is a false
  choice — the grids are nested. Expose *cut density* (every 1/2/4 beats) as the
  control **[proven]**
- **Anchor phase to a detected event, not a global estimate.** The drop is a
  strong, reliable onset; anchoring there agreed with independent phase estimation
  to within 0.019s **[proven]**
- Confidence = agreement between methods, not energy-on-grid. The energy metric is
  biased toward slow tempos and picked 64.6 BPM (29% alignment) over 95.7 (71%)
  **[proven failure]**
- Surface disagreement to Claude, which asks the user. Methods agreed on 6 of 7
  test tracks; failures were soft-transient material (cloud rap, processed edit
  audio)

### `preview(project)`

`capcut-cli render` so the user judges without opening CapCut **[untested]**.

---

## Safety

Non-negotiable, and the reason they are here: an earlier session destroyed a
user's finished edit with an `rm -rf` on a directory that also held their
originals.

- Never delete. Not files, not directories, not ever.
- Write only into project folders eyecut created.
- Back up `root_meta_info.json` and `draft_meta_info.json` before touching them.
- Atomic writes with `.bak`, matching capcut-cli's contract.
- **Refuse to write while CapCut is running** — it caches both files in memory and
  overwrites on quit **[proven]**
- Never write media into a temp directory — CapCut needs the paths to persist, and
  a scratchpad wipe took out generated audio mid-session **[proven failure]**

## Dependencies

`capcut-cli` (Node, shelled out, JSON interface) · `ffmpeg`/`ffprobe` · `librosa` ·
`numpy`. No vision model dependency: the client is the vision model.

## Build order

1. `register_media` — the differentiator, and the only thing here nobody else has.
2. `write_draft` — needed to see a result at all.
3. `extract_frames` — mostly written; `scripts/scan_experiment.py` has the ffmpeg
   invocations to lift.
4. `music_grid` — mostly written in the prototype, port it.
5. `preview` — thin, last.

---

## Out of scope

These were stages in the previous spec. They are recorded here because the
findings cost real time to obtain, and because someone will propose them again.

**`scan` — batch shot description.** A vision pass over contact sheets producing
`{description, subject, composition, lighting}` per shot, stored in `shots.json`
for a later stage to query. Cut because the later stage is Claude, and Claude can
look at the sheet itself. The pipeline only makes sense if selection is automated,
and selection is not automated. `scripts/scan_experiment.py` and
`scripts/describe_sheets.py` remain as experiments; neither is a build target.

**`arrange` — automated shot placement.** Slot carving, energy matching, minimum
source separation. Rejected twice on quality in the prototype **[proven failure]**;
the conversation does this better and the user is present anyway.

**`profile` — style extraction from a reference edit.** Recovered `shot_len_median`
0.93s, RGB percentile grade, black/white points, 46% push detection, and 93% beat
discipline from a 21.6s reference **[proven]** — real results, but it is a style
opinion Claude can hold in context from watching frames. Revisit only if
conversation-held style proves too vague in practice. `dissolve_ratio` was never
soundly derived; see below.

**Scene-boundary detection as a *detector*.** `ffmpeg scdet` finds 176 cuts in 405s
**[proven]**, but:

- **The threshold cannot be derived from the score distribution [proven failure].**
  `scdet` scores visual dissimilarity, not cuts: a well-matched cut scores low, a
  whip pan inside one shot scores high. On a full film (17,620 frames, 921
  candidates above 1.0) the distribution runs smooth from 36.6 down to 1.0 with no
  gap, and gap-hunting locked onto the sparse top of the range and *under*-detected
  — 95 shots against 106 from a plain fixed threshold.
- **Cluster boundaries, don't threshold frame-by-frame.** One cut trips `scdet` on
  2+ adjacent frames and a dissolve across many — 326 raw qualifying frames for 215
  shots on Sintel. Collapse each run to its peak frame.
- **`scdet` cannot detect dissolves at all [proven failure]**, in three stages.
  Width alone mislabelled 36.3-scoring hard cuts as dissolves (fast motion blurs a
  cut across extra frames). A ≥6-frame-run rule could never fire — the widest
  above-threshold run is 4 frames in Sintel, 5 in Tears of Steel, since a dissolve
  by definition changes little frame to frame. The sustained *sub*-threshold bands
  where dissolves must therefore live gave 10 and 12 candidates, of which both
  checked by eye were false positives: a slow push-in through fog, and a credit
  crawl. Frame-to-frame difference cannot separate "two images superimposed" from
  "one image moving slowly". This is also why `profile`'s `dissolve_ratio` (8 of 14
  recovered) should not be trusted — whatever produced it, it was not this signal.
- **Discard the first ~0.5s of scores on a clip extracted with `-ss`.** Seeking
  starts the decoder mid-GOP and the opening frames score spuriously high — 4
  invented boundaries in half a second.

**Demucs stem separation** — quality too poor to be useful **[proven failure]**.

**Writing our own draft library** — `capcut-cli` is better than what we would build.

**Identifying specific CapCut effect IDs from a reference.** You can see a flash;
saying *which* of 345 effects it is, is guesswork.
