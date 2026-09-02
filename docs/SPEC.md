# eyecut — spec

*CapCut support for Claude. Claude does the editing; eyecut writes the files.*

Rewritten 2026-09-01, narrowing a spec that still described five MCP tools and a
`music_grid` that was never built. **[proven]** marks a claim tested on real
footage or confirmed in the CapCut app; **[untested]** means the pieces exist but
nobody has run them.

---

## What it does

Claude can already look at footage and have an opinion about it. What it cannot do
is put that opinion into CapCut. eyecut is the layer that closes that gap: an MCP
server that registers media and writes a draft, and hands back a project that
opens ready to adjust.

The editing decisions — which shot, where, how long — happen in the conversation,
with the user's taste in the loop. That is not a limitation to engineer away.
Algorithmic clip selection was tried twice in the prototype and rejected both
times; the user's judgment was the necessary signal **[proven]**.

## What it is not

- **Not an editing pipeline.** No `scan`, no `arrange`, no shot-description
  database. Claude has vision; batching frames through a separate model to produce
  descriptions Claude then reads is a reimplementation of Claude as a cron job.
- Not a renderer. CapCut renders; eyecut writes the project.
- Not a CapCut draft library. `capcut-cli` does that well and eyecut depends on it.

---

## The MCP surface

**Two tools.** The test a tool has to pass is not "is it useful" but **"can Claude
do it another way?"** Claude runs in a shell. Frame extraction, shot browsing and
proxy rendering are a few lines of ffmpeg away, so they are CLIs
(`eyecut-frames`, `eyecut-shots`, `eyecut-proxy`, `eyecut-serve`,
`eyecut-speech`), not tools. Writing CapCut's format correctly is the thing no
shell gets you.

`tests/test_server.py` asserts the surface is *exactly* these two, so adding a
third fails a test and forces the argument.

### `register_media(project, paths)`

**The reason this project exists.** Writes entries into `draft_meta_info.json` →
`draft_materials`. Without them CapCut 9.x prompts to relink every clip, which
makes a generated draft worse than useless.

No other generator does this, verified 2026-08-31 against both candidates:
`capcut-cli` 0.21.1 puts the write **deliberately out of scope** for want of a
captured entry shape (`dist/store.js`, `assessMediaRegistrationRaw`); VectCutAPI
ships a template with every `draft_materials` group empty.

The entry shape is captured in `tests/fixtures/draft_materials_entries.json` from
CapCut-authored projects — note `file_Path` (capital P), `metetype` (misspelled),
microsecond durations, and that every real entry sat in the `type: 0` group
regardless of `metetype` **[proven]**.

Register the **copy inside the draft** (`./assets/video/x.mp4`), not the original
absolute path: registering the original leaves the media panel saying "Media lost"
**[proven, probes D vs E]**. Audio sources register the same way with no special
handling **[proven]**.

### `write_draft(spec, project_dir)`

Validates the spec, delegates the timeline to `capcut compile`, then applies
everything compile leaves undone — the post-compile layer below. Confirmed end to end: a draft built from
Sintel footage opened in CapCut with no relink prompt, correct durations, and the
`Speed 2.0X` badge on the right clip **[proven]**.

**Integer microseconds throughout.** Float seconds cause 1µs phantom overlaps that
the draft layer rejects **[proven failure]**.

---

## The spec, and what it reaches

`write_draft` passes the spec through to `capcut compile` untouched, except for
the parts compile has no vocabulary for (`sticker` / `sfx` tracks and `cover`,
which it rejects outright and eyecut builds afterwards). eyecut renames nothing
and wraps nothing, so a feature capcut-cli gains arrives here for free — the cost
is that compile's vocabulary is the vocabulary, warts and all.

**Verified working** **[proven]**: video / audio / text tracks · per-item `speed`,
`volume`, `scale`, `x`/`y`, `fontSize`, `color`, `sourceStart` · `transition` ·
`filter` · `effect` · `keyframe` on 11 properties · `audio-fade` · masks (nine
shapes) · **overlay / picture-in-picture** via a second *named* video track ·
**captions** from an SRT (one text segment per cue, `sub_type: 1`).

**Seen in the app** **[proven]**: `crop` (both a ratio and an explicit rect —
the 9:16 slice and the middle-quarter zoom both render), `rotation`,
`textRanges` (one word gold and bold, the rest plain, in a single text segment),
and **two video tracks compositing simultaneously**, the overlay layered over the
base rather than appended after it. Also confirmed in the app: a mask on the base
of a two-video-track spec lands on the base, with the overlay untouched — the
`(type, track, item)` matcher fix, checked where the files had lied before.

**Written, reaching the draft, not yet seen** **[untested]**: `opacity` and
`sticker`. Confirmed by a test against the real CLI to land in the draft; the app
has not confirmed either.

**Five are refused by `validate_spec`** rather than merely documented, because
each one exits 0, lands in the file and lints clean, so nothing else in the build
would ever tell the user: `mix`, `chroma`, `cover`, `bgBlur` and `sfx` tracks.
`bubble` is refused for a different reason — the store boundary below. Every one
is a **[proven failure]** with the evidence recorded further down.

**Templates — the reuse loop.** Style a title once in CapCut, then apply it
anywhere: `capcut save-template <project> <segment-id> <name> --out t.json`
captures a segment and its materials, and the `template` op clones them with fresh
ids. Passing `text` swaps the words *and* recomputes the style's character range,
so the styling still covers the new string. This is how a look designed by hand
gets reused without describing it in JSON **[proven]**.

**Replacement text does not fit itself to the frame.** The template carries the
font size it was designed at, and nothing wraps or shrinks: a 27-character line
dropped into a template built for an 8-character title runs off both edges of the
canvas, with only the middle visible **[proven]**. Keep replacement text near the
length of the original, or save a template per length of line.

**Not reachable**, and not because eyecut has not got to it — capcut-cli 0.21.1
cannot reach these either: fonts (`capcut enums --fonts` returns `[]`) ·
store-downloaded assets (`harvest-enums` is a path, not a built one) · speed
curves, motion tracking, and anything AI-driven in the app.

**Reachable, but not by slug**: stickers. `add-sticker` takes a raw
`<resource-id>` and there is no `capcut enums --stickers` to look one up in, so a
`sticker` item names an id — placed by hand in CapCut once, then read out with
`capcut harvest-enums`. Every other catalogue here is addressed by slug; this one
is the exception and a slug-shaped value is refused with that recipe.

Effects are named by slug from CapCut's own catalogue — 116 transitions, 345 scene
effects, 95 character effects, 76 text intros, 10 filters, 9 masks — listed with
`capcut enums --scene-effects` and friends. **Choosing one by name is easy;
identifying which one produced a flash in someone else's video is guesswork** and
stays out of scope.

### What `eyecut.spec` rejects

compile validates plenty on its own, and where it does, eyecut stays out of the
way: a duplicated check drifts out of step with upstream. These are the mistakes
compile accepts silently, or rejects only after seeding a draft directory. Every
one cost a real debugging session.

- **`start` is the timeline position; `sourceStart` is the in-point into the
  source.** Three 4-second shots taken from 120s, 300s and 610s of a film compiled
  to a **615-second draft** with two long gaps. `--check` accepts an unknown key
  without complaint **[proven failure]**. The detectable half — two items
  overlapping on one track — is refused.
- **Keyframes are one operation per point, each with `time` and `value`.**
  `from`/`to` is the natural guess; it compiles, `capcut lint` calls it clean, and
  the draft holds `time_offset: null, values: [null]` — an animation that does
  nothing **[proven failure]**.
- **A whole-frame zoom is `uniform_scale`**, not `scale`. Eleven property names.
- **Easings are hyphenated**: `ease-in-out`, not `ease_in_out`.
- **`filter` and `effect` cover a span of timeline**, taking `start`, `duration`
  and `slug` — and no `target`. Without a duration compile writes
  `target_timerange.duration: null`, which nulls the whole draft's duration and
  makes eyecut die reading it back **[proven failure]**.
- **`intensity` is 0–1.** Written verbatim otherwise: `5.0` lands in the draft as
  five times what the CapCut UI can express **[proven]**.
- **Every path must be absolute.** compile resolves a relative path against the
  *spec file*, and eyecut writes the spec into the drafts store — so
  `footage/a.mp4` resolves inside `com.lveditor.draft/` and compile reports a path
  the caller never wrote **[proven failure]**. Applies to item `path`, `captions`
  SRTs and `template` JSON alike.
- **`audio-fade` targets an audio item.**
- **Two tracks of one type need distinct `name`s.** compile keys a built track on
  (type, name), so unnamed tracks of the same type merge into one. A spec that
  reads as a base track plus an overlay becomes a single track with segments on
  top of each other — the main-track corruption `eyecut.timeline` exists to
  prevent — and `capcut lint` reports it clean **[proven failure]**. What CapCut
  actually does with the merged result, seen in the app: the overlay is **appended
  after** the base clip instead of layered over it, and the draft's stated duration
  no longer matches its content. Named tracks are how an overlay is built, so
  overlapping across them is allowed.
- **The `text-style` operation is refused outright** — it crashes the compiler.
  Set `textStyle` on the text item instead; see below.

### The post-compile layer

Compile builds a timeline and stops. Everything else CapCut can do to a segment
is a separate capcut-cli command against a segment id, so eyecut applies it
afterwards. `eyecut/ops.py` is the list of those keys as **one table** —
`eyecut.draft.apply_item_ops` walks it and `eyecut.spec` validates against the
same rows, so a key cannot be applicable in one and unknown in the other. Written
as a function per key it was three near-identical bodies each restating the
count-mismatch rule; at twelve it would be the first thing to drift.

Two things run in a deliberate order after it. `sticker` and `sfx` tracks **add**
segments rather than decorate them, so they are built last: earlier, they would
shift the positions every per-segment op is matched on. And because compile
rejects a track type it does not know ("tracks[1].type must be one of
video|audio|text"), those tracks and the top-level `cover` are stripped from the
spec compile sees — the one place eyecut no longer passes the spec through
untouched.

**Matching is by (type, track, item).** The nth item of the spec's nth track of a
type is the nth segment of the built nth track of that type. Keying on
(type, item) alone — as this did until 2026-09-01 — collapses every video track
onto one set of positions, so the last track of a type wins every key and a mask
meant for the base clip lands on the overlay. Silently: the counts still agree,
so the mismatch guard never fires, and `capcut lint` reports it clean **[proven
failure, against the real CLI]**. Two named video tracks are how an overlay is
built, so that was not a corner case.

### What `write_draft` repairs after the compile

- **Animation.** compile has no animation operation, so every cut is a hard cut
  and every caption simply appears. `anim` is an item key on video and text items:
  `intro`, `outro` and (video only) `combo`, each with an optional
  `<slot>Duration` in seconds, applied with `capcut text-anim` on a caption and
  `capcut image-anim` on a clip. The slugs are **not** validated against a list —
  `capcut enums` carries 318 across the five animation catalogues and the app's
  store adds more, so a whitelist would reject valid ones. An unknown slug makes
  the CLI exit non-zero and becomes a warning naming the item.

  ```json
  {"text": "TITLE", "start": 0, "duration": 3,
   "anim": {"intro": "typewriter", "introDuration": 0.6, "outro": "fade-out"}}
  ```

- **Text look.** compile offers a text item `fontSize` and `color` and nothing
  else, and its `text-style` op crashes. A caption with no border or shadow is
  unreadable over footage of any brightness, so `textStyle` is an item key on text
  items, applied afterwards with `capcut text-style`: `shadow` / `vertical` are
  flags, `shadowColor` / `borderColor` / `bgColor` take `"#RRGGBB"`, `preset`
  takes an absolute path to a `make-preset` file, and everything else is a number.

  ```json
  {"text": "TITLE", "start": 0, "duration": 3, "fontSize": 24,
   "textStyle": {"borderWidth": 0.08, "borderColor": "#000000",
                 "shadow": true, "shadowAlpha": 0.6}}
  ```

- **Blend modes and chroma keys are written, then thrown away by the app**
  **[proven failure, reproduced on two independent drafts]**. Both are refused by
  `validate_spec` — `DISCARDED_BY_APP` in `eyecut/spec.py`. Their entries stay in
  the `ITEM_OPS` table and their shape checks stay written, so the day capcut-cli
  writes a struct CapCut keeps, deleting two dict entries turns them back on. `capcut mix-mode`
  writes `mix_mode: "Screen"` onto the video *material* (as speed does) and
  `capcut chroma` writes `{type: "chromas", intensity: 0.6}`. Both are present
  and correct in the draft eyecut hands over, in the root file and the timeline
  mirror, and `capcut lint` reports it clean. **Open the draft in CapCut once and
  save, and `mix_mode` is gone from every material while the chroma entry is
  rewritten to CapCut's own struct with the effect off** — `{type: "none",
  intensity_value: 0.0}`, keeping only the colour.

  So these two are reachable on paper and useless in practice: a spec can ask for
  them, the files say they applied, and the first time the user opens the project
  the app discards them. This is not a general "CapCut rewrites everything": the
  `canvas_blur` from `bgBlur`, written in the same pass, survives the same save
  untouched. It just draws nothing — see below.

  Found only because the drafts were opened and the files re-read afterwards. The
  test suite is green on both keys — it asserts against the file eyecut wrote,
  which is exactly the evidence SPEC.md says is necessary and not sufficient.

- **`cover` sets a key the project list does not read** **[proven failure]**.
  Refused by `validate_spec`; `DISCARDED_COVER` in `eyecut/spec.py` carries the
  reason, and the shape it used to check was `{path, time}` for when the key
  becomes worth writing again.
  `capcut add-cover` exits 0 and writes `draft_info.cover` with the image path
  and `time_ms`, so nothing in the build reports a problem. But it produces no
  `draft_cover.jpg` in the draft folder and leaves `draft_meta_info.draft_cover`
  at the template's default name, pointing at a file that does not exist —
  **the thumbnail in CapCut's project list stays black, identical to a draft
  that asked for no cover at all.**

  Two further reasons not to lean on it even if the app is later taught to read
  the key: the path written is wherever the caller's image happened to live
  (`verify_coverage.py` leaves it pointing into the repo's `scratch/`), so the
  reference breaks the moment that file is cleaned up; and nothing copies the
  image into the draft, so the draft is not self-contained.

  The whole value of `cover` was setting a thumbnail *without* opening the
  project. It does not do that, and once the project is opened CapCut generates
  its own thumbnail anyway — which is the state every eyecut draft is already in.

  Found only because the drafts were opened and the files re-read afterwards.

  **This is a wrong-shape problem, not a proven dead end.** The entry CapCut
  writes back carries `spill_value`, `edge_smooth_value` and `version` — fields
  capcut-cli never wrote, so CapCut *read* the entry, kept the colour and rebuilt
  the rest in its own struct. It lost the strength because capcut-cli writes
  `intensity` where CapCut reads **`intensity_value`**. If that is the whole
  story, both keys are repairable the way `constant_material_id` on masks
  already is: stamp the native shape after the compile.

  What settles it is the method that produced the `register_media` fixture and
  the mask `constant_material_id` — **apply a blend mode and a chroma key by hand
  in CapCut, save, and diff the two entries.** No draft on this machine has ever
  used either (59 scanned, zero hits), which is why nothing caught it earlier and
  why the reference has to be made rather than found. Until that diff exists,
  where CapCut 9.x keeps a blend mode is simply unknown: it is not on the video
  material, and it is nowhere else in the saved file.

- **`bgBlur` renders black, not a blurred fill** **[proven failure]**. The
  `canvas_blur` material is written with `blur: 0.75` for level 3, attached to the
  right segment's `extra_material_refs`, and it *survives* a CapCut save — which
  made it look healthier than `mix` and `chroma` right up until someone looked.
  On screen the frame either side of a 9:16 cropped clip is solid black, not a
  blurred copy of the footage. Refused. To fill that space, put the footage on a
  second video track behind the cropped one.
- **A `bubble` shape is a store asset** **[proven failure]**. The
  `bubble_effect_id` / `bubble_resource_id` pair and the matching `text_shape`
  filter are all written and correctly referenced from the caption; CapCut renders
  bare text. The shape was never downloaded, and **no slug fixes this** —
  `capcut enums` lists what the catalogue has, not what the local app has. The
  same boundary that makes `add-sticker` need a hand-harvested id. `textStyle`
  gives a caption a background box and works.
- **An `sfx` track cannot make a sound** **[proven failure]**. Two bugs stacked.
  capcut-cli's `add-sfx` pushes its `sound_effect` material into
  `materials.audio_effects` and points the segment's `material_id` at it, but
  CapCut resolves audio segments through `materials.audios` — so the segment has
  no material and **CapCut deletes the whole track on save**. `repair_sfx_materials`
  fixes that half (see `eyecut/draft.py`), and with it the track and both segments
  survive a save. But the effect ships with `path: ""` — no audio file, because
  the sound is a store asset — so CapCut rewrites the material to `type: "none"`
  and the clip is silent and undrawable. Put the effect on an ordinary `audio`
  track naming a local sound file, which is **[proven]**.
- **`bgBlur` is a level, not the fraction it stands for.** 1–4 map to 0.0625 /
  0.375 / 0.75 / 1.0. Passing `0.75` is the natural guess and gets a
  level-shaped error only after the draft exists, so it is refused up front.

- **A `crop` rect is 0–1 fractions of the source frame**, not pixels. Written
  verbatim otherwise, landing far outside the frame — the same class as
  `intensity`.

- **Speed.** compile writes `segment.speed` and leaves the speed *material* at 1.
  **CapCut reads the material**, so a clip asked for 2× plays at 1× while its trim
  is still cut for 2× — an edit wrong in a way that looks like bad footage.
  `capcut lint` reports `speed-material-mismatch` and cannot fix it; `capcut speed`
  re-syncs both without disturbing the timeranges **[proven, confirmed in the app]**.
- **Masks.** compile has no mask operation at all, so `mask` is the one key eyecut
  adds to compile's vocabulary, applied afterwards with `capcut mask` and matched
  to the segment **by position** — the nth item of the spec's nth track of a type
  is the nth segment of the built track of that type. Compile preserves both
  orders and the filter/effect tracks it appends carry no items. If the counts
  disagree the masks are skipped with a warning: a mask on the wrong shot is worse
  than no mask.
- **`constant_material_id` on every mask.** `capcut mask` leaves it empty; a
  CapCut-authored mask carries a UUID that appears once in the draft and
  references nothing. Found by asking the user to apply a circle mask by hand and
  diffing the two entries — the same method that produced the `register_media`
  fixture, and the only method that works for this class of question
  **[proven, confirmed in the app]**.
- **`tm_duration`**, which compile leaves at 0, listing the draft as 00:00.

### Reading the app, not the files

A draft that lints clean can still be wrong. `capcut lint` reported 0 errors on a
mask that had not been confirmed in the app, and file-level evidence was read as
proof twice — once concluding masks worked when unverified, once concluding they
were broken from a screenshot that showed the mask *selected for editing*, where
CapCut draws the full frame plus a guide rather than the cropped result.

**The app is the standard.** Lint and file structure are necessary and not
sufficient; a claim is `[proven]` only once it has been seen in CapCut.

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
  overwrites on quit. Closing the project is not enough; only quitting releases it
  **[proven]**.
- Never write media into a temp directory — CapCut needs the paths to persist, and
  a scratchpad wipe took out generated audio mid-session **[proven failure]**.
- Validate the spec *before* the running-CapCut guard, so a rejected spec leaves
  no half-built project behind.

## Dependencies

`capcut-cli` (Node, shelled out, JSON interface) · `ffmpeg`/`ffprobe` · `numpy` ·
`pillow`. No vision model dependency: the client is the vision model. No librosa —
see `music_grid` below.

---

## Out of scope

Recorded because the findings cost real time, and because someone will propose
them again.

**`music_grid` — beat detection.** Was build step 4 of the previous spec and is
cut. Not because it does not work — `~/editing/gojo/build/measure.py` finds a beat
grid with two independent methods and cross-checks them, and the octave-ambiguity
and drop-anchoring findings are real **[proven]**. It is cut because **it is about
music, not about CapCut.** eyecut's one differentiator is writing a format nobody
else writes; a beat detector belongs in the build scripts where it already lives,
or in a tool of its own. The same test that cut it also cut `extract_frames` from
the MCP surface.

**`extract_frames`, `browse_shots`, `preview` as MCP tools.** Built, tested, and
moved to CLIs. Claude has a shell; a tool that wraps five lines of ffmpeg is
surface area without capability.

**`scan` — batch shot description.** A vision pass producing `{description,
subject, composition, lighting}` per shot for a later stage to query. Cut because
the later stage is Claude, and Claude can look at the sheet itself.

**`arrange` — automated shot placement.** Rejected twice on quality in the
prototype **[proven failure]**; the conversation does this better and the user is
present anyway.

**`profile` — style extraction from a reference edit.** Recovered `shot_len_median`
0.93s, RGB percentile grade, 46% push detection and 93% beat discipline from a
21.6s reference **[proven]** — real results, but a style opinion Claude can hold in
context from watching frames. `dissolve_ratio` was never soundly derived.

**Scene-boundary detection as a *detector*.** `ffmpeg scdet` finds 176 cuts in
405s **[proven]**, but:

- **The threshold cannot be derived from the score distribution [proven failure].**
  `scdet` scores visual dissimilarity, not cuts: a well-matched cut scores low, a
  whip pan inside one shot scores high. On a full film (17,620 frames, 921
  candidates above 1.0) the distribution runs smooth from 36.6 down to 1.0 with no
  gap, and gap-hunting *under*-detected — 95 shots against 106 from a fixed
  threshold.
- **Cluster boundaries, don't threshold frame-by-frame.** One cut trips `scdet` on
  2+ adjacent frames — 326 raw qualifying frames for 215 shots on Sintel. Collapse
  each run to its peak.
- **`scdet` cannot detect dissolves at all [proven failure]**, in three stages.
  Width alone mislabelled 36.3-scoring hard cuts as dissolves. A ≥6-frame-run rule
  could never fire — the widest above-threshold run is 4 frames in Sintel, 5 in
  Tears of Steel. The sustained *sub*-threshold bands gave 10 and 12 candidates, of
  which both checked by eye were false positives: a slow push-in through fog, and a
  credit crawl. Frame-to-frame difference cannot separate "two images superimposed"
  from "one image moving slowly".
- **Discard the first ~0.5s of scores on a clip extracted with `-ss`.** Seeking
  starts the decoder mid-GOP — 4 invented boundaries in half a second.

**Demucs stem separation** — quality too poor to be useful **[proven failure]**.

**Writing our own draft library** — `capcut-cli` is better than what we would build.

**Identifying specific CapCut effect IDs from a reference.** You can see a flash;
saying *which* of 345 effects it is, is guesswork.

---

## Upstream

The `text-style` **operation** crashes capcut-cli 0.21.1 with `Cannot read
properties of undefined (reading 'alpha')` on
`{"op": "text-style", "target": ..., "bold": true}`. Only the compile path is
broken: the standalone `capcut text-style <project> <segment>` command applies the
same border and shadow and returns `{"ok":true,"applied":["shadow","border"]}`
**[proven]**. So eyecut refuses the op and reaches the styling through the
`textStyle` item key instead, applied after the compile. The refusal should be
deleted when upstream fixes it. **Not yet reported.**

The better end state for `register_media` is still upstreaming: `capcut fixture
<project> --out <dir>` on a CapCut-authored draft produces exactly the evidence
bundle `capcut-cli` says it lacks. Contributing it could move that tool out of
eyecut entirely.
