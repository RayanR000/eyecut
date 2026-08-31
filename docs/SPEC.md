# eyecut — spec

*AI video editing for CapCut. Footage and a song in, an editable CapCut project out.*

Written 2026-08-30, from a working prototype. Every claim marked **[proven]** was
tested on real footage this session; **[untested]** means the pieces exist but the
whole was never run.

---

## What it does

You give it a folder of footage, a song, and optionally a reference edit whose style
you want. It returns a CapCut project, already cut, that opens ready to adjust.

The differentiator is that it *looks* at the footage. Existing tools take instructions
("put clip A at 3s"); eyecut decides which clip and when.

## What it is not

- Not a renderer. CapCut does the final render; eyecut writes the project.
- Not a CapCut draft library. `capcut-cli` already does that well — eyecut depends on it.
- Not an autonomous editor. The user picks the source footage, approves the tempo when
  detection is uncertain, and edits the result. **[proven]** — algorithmic clip selection
  was tried twice and rejected both times; the user's taste was the necessary signal.

---

## Pipeline

```
footage ──► scan ──► shots.json      (what each shot contains)
                        │
reference ─► profile ──► style.json  (cut rhythm, grade, transitions)
                        │
song ─────► music ───► grid.json     (tempo, downbeat, structure)
                        │
                        ▼
                     arrange ──► cutlist.json
                        │
                        ▼
                     build ──► CapCut project + preview.mp4
```

Each stage writes JSON and can run alone. That matters for iteration: re-running
`arrange` after changing one shot must not re-scan 400 seconds of video.

---

## Stages

### 1. `scan` — read the footage

Split into scenes, then look at them.

- `ffmpeg scdet` for scene boundaries **[proven]** — 176 cuts from a 405s source
- **The threshold cannot be derived from the score distribution** — tried 2026-08-31
  on Sintel and Tears of Steel, and it fails **[proven failure]**. `scdet` scores
  visual dissimilarity, not cuts: a well-matched cut (same palette, motion carried
  across it) scores low, a whip pan inside one shot scores high, and no threshold
  separates them. A 49s span showed a clean 2.8 → 1.8 break, but that was
  small-sample noise; over a full film (17,620 frames, 921 candidates above 1.0)
  the distribution runs smooth from 36.6 down to 1.0 with no gap at all, and
  gap-hunting locked onto the sparse top of the range and *under*-detected —
  95 shots against 106 from a plain fixed threshold.
- **Run permissive and let over-segmentation happen** — threshold 3.0, ~22 cuts/min,
  with each boundary keeping its score and a high/medium/low confidence. The error
  costs are asymmetric: splitting one shot in two is nearly free, since both halves
  describe the same content, while a missed cut leaves a 48s "shot" whose 3 sampled
  frames misrepresent most of its own span. Merging neighbours the vision model
  describes identically is the cheap correction.
- **Cluster boundaries, don't threshold frame-by-frame.** One cut trips `scdet` on
  2+ adjacent frames and a dissolve trips it across many, so raw qualifying frames
  overcount badly — 326 of them for 215 shots on Sintel, with the surplus silently
  swallowed by the minimum-shot-length filter. Collapse each run to its peak-scoring
  frame and **keep the run's width and shape**. That width is the same frame-diff
  signal `profile` uses to count dissolves, so `scan` produces it for free.
- **`scdet` scores cannot detect dissolves at all** **[proven failure]**, in three
  stages. Width alone mislabelled 36.3-scoring hard cuts as dissolves (fast motion
  blurs a cut across extra frames). A shape rule requiring ≥6-frame runs could never
  fire — the widest run above threshold is 4 frames in Sintel, 5 in Tears of Steel,
  since a dissolve by definition changes little frame to frame. And the sustained
  *sub*-threshold bands where dissolves must therefore live gave 10 and 12
  candidates, of which both checked by eye were false positives: a slow push-in
  through fog, and a credit crawl. The cause is structural — frame-to-frame
  difference cannot separate "two images superimposed" from "one image moving
  slowly". Detecting dissolves needs a different measurement entirely.
- **Discard the first ~0.5s of boundary scores on a clip that was itself extracted
  with `-ss`.** Seeking starts the decoder mid-GOP, so the opening frames score
  spuriously high and invent boundaries — 4 of them in the first half-second of a
  test extraction. Relevant because pre-cut clip packs are a normal input.
- Sample **3 frames per shot**, not 1. A single frame says nothing about motion, and a
  midpoint sample landed on a black frame during the prototype **[proven failure]**
- Tile frames into contact sheets (~35 per sheet) and read them with a vision model.
  Batching is what makes this affordable **[proven]**
- Reject sources before spending tokens: fps ≠ 24 for anime means frame-interpolated
  (every "clips for editing" pack online is), codec AV1 means CapCut cannot read it,
  and watermarks cluster in the first and last seconds **[proven]** — all three were
  hit in one session

Output per shot: `{start, end, description, subject, composition, lighting,
readable_at_speed}`, plus what the detector knew about the boundary that opened it —
`{boundary_score, boundary_kind, boundary_width_frames, boundary_confidence}`.
`arrange` should prefer high-confidence boundaries when placing accents; a `low` one
is as likely to be camera movement as an edit.

### 2. `profile` — read a reference edit

Extract a style recipe from a video the user likes. **[proven]** on a 21.6s reference:

| Field | Method | Recovered |
|---|---|---|
| `shot_len_median` | scene detect | 0.93s |
| `dissolve_ratio` | frame-diff width at boundaries | 8 of 14 |
| `grade` | RGB/luma percentiles | mean RGB [.272,.220,.162] → warm red |
| `black_point` / `white_point` | luma 5th/95th pct | 0.0 / 0.73 (crushed blacks) |
| `push` | scale correlation, shot start vs end | 46% of shots, mean 1.085× |
| `beat_discipline` | cuts vs beat grid | 93% on beat |

`dissolve_ratio` still needs its own method — see stage 1's failures. `scdet` scores
cannot yield it: a dissolve and a slow camera move produce the same sustained
frame-to-frame difference. Whatever recovered 8 of 14 on the reference edit, it was
not this signal, and that method should be re-derived before the field is trusted.

Not recoverable: specific CapCut effect IDs. You can see a flash; identifying *which*
of 345 effects it is, is guesswork. Imitate the feel, not the project.

### 3. `music` — build the grid

- Tempo via `librosa.beat.beat_track`, with a second method for cross-check
- **Report octave ambiguity rather than resolving it.** 68 vs 136 BPM is a false
  choice — the grids are nested. Expose *cut density* (every 1/2/4 beats) as the
  control instead **[proven]**
- **Anchor phase to a detected event, not a global estimate.** The drop is a strong,
  reliable onset; anchoring there agreed with independent phase estimation to within
  0.019s **[proven]**
- Confidence = agreement between methods, not energy-on-grid. The energy metric is
  biased toward slow tempos and picked 64.6 BPM (29% alignment) over 95.7 (71%)
  **[proven failure]**
- Ask the user only when methods disagree. They agreed on 6 of 7 test tracks;
  failures were soft-transient material (cloud rap, processed edit audio)

### 4. `arrange` — choose and place

- Carve the timeline into bar-aligned slots; density from the style profile, or from
  section energy when there's no reference
- Assign shots to slots: strongest imagery to highest-energy slots, minimum separation
  in source time so it doesn't drain one sequence **[proven]**
- Structure around the anchor event — build, drop, resolve
- **Integer microseconds throughout.** Float seconds cause 1µs phantom overlaps that
  the draft layer rejects **[proven failure]**

### 5. `build` — write the project

- Delegate draft writing to `capcut-cli compile` (JSON spec in, draft out)
- **Register media in `draft_meta_info.json` → `draft_materials`. eyecut writes this
  itself — it is not delegated.** Without it CapCut 9.x prompts to relink every clip.
  Entry shape captured from a CapCut-authored draft; note `file_Path` (capital P),
  `metetype` (misspelled), microsecond durations **[proven]**.
  This is the piece no other generator has, verified 2026-08-31 against both
  candidates: `capcut-cli` 0.21.1 puts the write **deliberately out of scope** for want
  of a captured entry shape (`dist/store.js`, `assessMediaRegistrationRaw`) and only
  observes the three empty states via `diagnose`/`lint`; VectCutAPI ships a template
  with every `draft_materials` group empty and no code that ever populates it.
  Upstream asks for the artifact we already have — `capcut fixture <project> --out <dir>`
  on a CapCut-authored draft is the evidence bundle a registration write is built from,
  and contributing it could move this step out of eyecut entirely.
- Register the project in `root_meta_info.json` or it never appears in the UI **[proven]**.
  `capcut-cli` covers this (adds the entry, creates the file if absent, never rewrites
  the whole index), so delegate it.
- **Refuse to write while CapCut is running** — it caches both files in memory and
  overwrites on quit **[proven]**
- Preview via `capcut-cli render` so the user judges without opening CapCut **[untested]**

---

## Safety

Non-negotiable, and the reason they're here: this session destroyed a user's finished
edit with an `rm -rf` on a directory that also held their originals.

- Never delete. Not files, not directories, not ever.
- Write only into project folders eyecut created.
- Back up `root_meta_info.json` and `draft_meta_info.json` before touching them.
- Atomic writes with `.bak`, matching capcut-cli's contract.
- Never write media into a temp directory — CapCut needs the paths to persist, and
  a scratchpad wipe took out generated audio mid-session **[proven failure]**

## Dependencies

`capcut-cli` (Node, shelled out, JSON interface) · `ffmpeg`/`ffprobe` · `librosa` ·
`numpy` · a vision-capable model for `scan` and `profile`.

Rejected: Demucs stem separation — quality too poor to be useful **[proven failure]**.
Rejected: writing our own draft library — capcut-cli is better than what we'd build.

## Interface

Python package, plus an MCP server exposing `scan`, `profile`, `music`, `arrange`,
`build`. MCP is a thin shell; all quality lives in the library beneath it.

## Build order

1. `scan` — the differentiator, and the riskiest. Nothing else matters if this is weak.
2. `build` — needed to see results at all.
3. `music` — mostly written already.
4. `arrange` — mostly written already.
5. `profile` — proven in prototype, port it.
6. MCP layer — last, thin.

## Open question

Whether contact-sheet selection beats manual picks on footage the model hasn't already
been walked through. Everything else is engineering; this is the actual bet. Test it on
unfamiliar footage before building anything else.
