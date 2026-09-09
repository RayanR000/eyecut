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
- **Not a renderer.** CapCut renders; eyecut writes the project. `eyecut.proxy`
  is the one exception and stays one: it answers *is the timeline right* — which
  shot, in what order, for how long, framed how — for edits too dense for
  CapCut's own preview to scrub. It gets a feature only when the absence makes
  the timeline unreadable (an overlay that does not composite reads as a missing
  clip; text that does not draw reads as a gap), never when the feature only
  changes how a frame looks. Filters, effects, transitions, animations, masks and
  store assets are deliberately absent, and a proxy render is never evidence
  about CapCut — it shows what eyecut wrote, not what the app does with it. The
  rule is written out at the top of `eyecut/proxy.py`; without it this becomes a
  second compositor that must track everything `eyecut.draft` can write, forever,
  with CapCut as its only oracle — and if you are opening CapCut anyway, the
  proxy has not saved you the trip.
- Not a CapCut draft library. `capcut-cli` does that well and eyecut depends on it.

---

## The MCP surface

**Two tools.** The test a tool has to pass is not "is it useful" but **"can Claude
do it another way?"** Claude runs in a shell. Frame extraction, shot browsing and
proxy rendering are a few lines of ffmpeg away, so they are CLIs
(`eyecut-frames`, `eyecut-shots`, `eyecut-proxy`, `eyecut-serve`), not tools. Writing CapCut's format correctly is the thing no
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
which it rejects outright and eyecut builds afterwards — plus the `import-ass`
operation, which is stripped from the spec compile sees and executed against
the built draft instead). eyecut renames nothing
and wraps nothing, so a feature capcut-cli gains arrives here for free — the cost
is that compile's vocabulary is the vocabulary, warts and all.

**Verified working** **[proven]**: video / audio / text tracks · per-item `speed`,
`volume`, `scale`, `x`/`y`, `fontSize`, `color`, `sourceStart` · `transition` ·
`filter` · `effect` · `keyframe` on 11 properties · `audio-fade` ·
**overlay / picture-in-picture** via a second *named* video track ·
**captions** from an SRT (one text segment per cue, `sub_type: 1`) ·
**`import-ass`** (an `.ass`/`.ssa` file, inline styling carried, but as plain
text — see below) · **blend
modes** (all nine shipped shaders named back by the app's Blend panel, and
surviving the save) · **`cover`** (the image appears in the project list without
the draft ever being opened).

Masks are **no longer in this list**, and no longer accepted: nine shapes reach
the file and the Mask panel shows them, and nothing masks the picture. `mask` is
refused by `validate_spec` with the rest of the no-ops. See below.

**Seen in the app** **[proven]**: `crop` (both a ratio and an explicit rect —
the 9:16 slice and the middle-quarter zoom both render), `rotation`,
`textRanges` (one word gold and bold, the rest plain, in a single text segment),
and **two video tracks compositing simultaneously**, the overlay layered over the
base rather than appended after it. Also confirmed in the app: a mask on the base
of a two-video-track spec lands on the base, with the overlay untouched — the
`(type, track, item)` matcher fix, checked where the files had lied before. What
that confirms is *placement*, which is all it ever claimed; the mask still does
not draw.

Nothing is left in the **[untested]** column except the app half of the three
post-compile operations below: every other key and track type capcut-cli 0.21.1
can reach has now been seen in the app, or measured out of one.

**Post-compile operations** — added 2026-09-08, and seen in the app. They are spec
`operations`, not item keys: validated in `eyecut.spec`, stripped from the spec
compile sees, and executed in `write_draft` (`apply_post_ops`) after the track
ops, before media registration. `import-ass` is the only one, and imports an ASS/SSA file via
`capcut import-ass` (needs `path`, absolute; track name, font size, colour and
time offset options) as one text segment per Dialogue on a `subtitle` track, seeding the
size from the file's `[V4+ Styles]` line and turning inline overrides (`{\b1}`)
into per-range styles.

**`caption` and `tts` were here and are cut** — not because they failed. Both
worked. `caption` wrapped a whisper binary and `tts` wrapped `say`, and neither
is CapCut's format: the format half of each already had a supported route, an
`.srt` through the proven `captions` op and a wav on an ordinary `audio` track,
and Claude has a shell to produce either. That is the same test that cut beat
detection for being about music rather than about CapCut, and the surface here
is small on purpose. `eyecut.spec.OUT_OF_SCOPE` refuses both **by name**, with
the replacement route in the message — falling through to "unknown op" would
read as a typo to the one person who most needs the recipe.

Unmapped CLI flags (`--style-ref`...) are refused rather than dropped:
accepting one would build success around work the draft does not contain.

**`import-ass` renders, as text and not as captions** **[proven]**. Both lines of
a two-line `.ass` appear at their stated times with the inline `{\b1}` bold
carried through — but the segments come back from the save as `sub_type: 0`, the
plain-text value, where the `captions` op's are `1`. So the track is named
`subtitle` and the source file is a subtitle format, and what lands is still an
ordinary pair of text layers: absent from the Captions panel, and with none of
the bulk restyle or subtitle re-export that a caption set gets. Both suggest
otherwise, which is the reason to write it down.

**A thin material is not a broken one.** `import-ass` writes 15 keys where
`captions` writes 126 — no `font_path`, no `sub_type`, none of the background,
border or shadow blocks — and the file-level reading of that was that it could
not render. It renders: CapCut fills every default on load and writes all 126
back on quit. That was the fourth time file evidence has been misread in this
project in this direction, and the first outside masks. It stays the rule that
the app is the standard. The one real consequence is that the healed material
exists only after a first open, so a draft handed straight to another tool still
carries the stub.

**Five are refused by `validate_spec`** rather than merely documented, because
each one exits 0, lands in the file and lints clean, so nothing else in the build
would ever tell the user: `bgBlur`, `opacity`, `mask`, and `sticker` and `sfx`
tracks. `mask` is the newest and the one that had been believed working longest.
There were seven. `chroma` and `mix` fell to the `check_flag` discovery below and
`cover` to reading what CapCut's project list actually opens — all three by the
same method, which is to make the reference by hand and diff it.
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
   SRTs, `template` JSON and `import-ass` subtitle files alike.
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
- **The `text-style` operation is refused outright** — styling is per text item
  via `textStyle`, matched to its built segment after the compile, which a
  whole-spec operation cannot do. Set `textStyle` on the text item instead;
  see below.

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

A second post-compile path sits beside that table and is deliberately not part
of it: `import-ass` is a whole-spec *operation*, not a per-segment item key,
so `eyecut.draft.apply_post_ops` maps it to its `capcut` argv directly instead
of walking `eyecut.ops`. The one-table invariant — applicable in one place
means known in the other — does not cover it; its shape checks live in
`eyecut.spec` (the `FILE_OPS` entry and the `POST_OP_KEYS` allow-list it is
checked against, so an unmapped CLI flag fails validation instead of vanishing
between the spec and the argv).

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
  else, and a whole-spec `text-style` op cannot match styling per item. A caption with no border or shadow is
  unreadable over footage of any brightness, so `textStyle` is an item key on text
  items, applied afterwards with `capcut text-style`: `shadow` / `vertical` are
  flags, `shadowColor` / `borderColor` / `bgColor` take `"#RRGGBB"`, `preset`
  takes an absolute path to a `make-preset` file, and everything else is a number.

  ```json
  {"text": "TITLE", "start": 0, "duration": 3, "fontSize": 24,
   "textStyle": {"borderWidth": 0.08, "borderColor": "#000000",
                 "shadow": true, "shadowAlpha": 0.6}}
  ```

- **A blend mode is written where CapCut does not keep it** **[proven failure,
  now repaired]**. `capcut mix-mode` writes `mix_mode: "Screen"` as a string
  field onto the video *material*, the way speed is written. It lints clean, and
  CapCut strips it from every material on the first save — because it was never
  a field the app reads. A blend mode is its own material in `materials.effects`,
  referenced from the segment:

  ```json
  {"type": "mix_mode", "name": "Screen", "effect_id": "871339",
   "resource_id": "6758325170760323597", "value": 1.0, "visible": true,
   "path": ".../Resources/MixMode/d9c1d4ca7ab9…"}
  ```

  `repair_mix_modes` reads the CLI's string field, builds that material,
  references it, sets `check_flag` bit 8 and deletes the string.

  **The catalogue did not have to be harvested.** CapCut ships
  `Resources/MixMode/MixMode.json`, a manifest of all ten shaders with their
  `effectId`, `resourceId` and file — so unlike the sticker id, which had to be
  captured from a draft made by hand, the identities are simply there to read.
  Look for a manifest before building a harvesting ritual.

  Two consequences of ten shaders against the CLI's twelve slugs. **`difference`
  and `exclusion` are refused**: `capcut mix-mode` accepts them and CapCut ships
  no shader, so the material would name a resource that is not there — the
  failure `sticker` dies of, caught before the draft exists. And the bundle holds
  one shader the CLI has no slug for, Linear burn, which is out of reach for the
  same reason everything else upstream is.

  **The whole mapping is now confirmed in the app** [proven]. It was inferred
  except for Screen (`effect_id: 871339`, internal name `color_filter`), with the
  rest read across from the Chinese originals — `glare_pc` 强光 Hard Light,
  `darken_color` 颜色加深 Color Burn, `dark_en`/`bright_en` 变暗/变亮
  Darken/Lighten. `verify_coverage.py --only mix` builds one clip per mode, and
  CapCut's own Blend panel named all nine back in order: Multiply, Screen,
  Overlay, Soft light, Hard light, Color dodge, Color burn, Darken, Brighten.
  Note the last one: the `lighten` slug is right, but the app labels that mode
  **Brighten**, so the checklist asks for the name CapCut prints, not the slug.

  Verified past the save as well, which is where the string field died: opened,
  saved and re-read, the draft still carries all nine materials, every
  `extra_material_refs` entry, and `check_flag` 15 on every overlay segment.

- **A chroma key is written under names CapCut does not read, and gated behind a
  flag nothing sets** **[proven failure, now repaired]**. `capcut chroma` gets the
  hard part right — the material is created and referenced from the correct
  segment — and every field wrong:

  | | CapCut | capcut-cli 0.21.1 |
  |---|---|---|
  | `type` | `chroma` | `chromas` |
  | strength | `intensity_value` | `intensity` |
  | shadow | `shadow_value` | `shadow` |
  | `color` | `#0d1618ff` | `#0d1618` |
  | `path` | the in-bundle `Chroma2` shader | `""` |
  | absent | `should_transfer_color`, `edge_smooth_value`, `spill_value`, `version` | — |

  `repair_chroma_materials` rewrites all of it after the compile, verified field
  for field against a key applied by hand in the app. **That alone changed
  nothing on screen**: the app still showed the Chroma key box unticked over a
  draft byte-identical to the hand-keyed one but for its ids. The second half is
  `check_flag`, below. With both, the box is ticked, the colour and strength are
  populated, and the key renders **[proven]**.

  The lesson worth keeping is the shape of the mistake: "the file matches CapCut's
  own, field for field" was true, and the feature was still dead. The evidence
  that settles a question is the app.

- **`cover` sets a key the project list does not read** **[proven failure, now
  repaired]**. `capcut add-cover` exits 0 and writes `draft_info.cover` with the
  image path and `time_ms`, so nothing in the build reports a problem. But it
  produces no `draft_cover.jpg` in the draft folder, so the requested cover
  never appears.

  The fix was in the sentence that used to describe the problem: the meta leaves
  `draft_meta_info.draft_cover` "at the template's default name, pointing at a
  file that does not exist". That default name is `draft_cover.jpg`, and it is
  what CapCut's project list opens — confirmed against 28 CapCut-authored drafts,
  every one of them a 1920x1080 JPEG at that exact name. So the meta was never
  wrong and the CLI was never needed: `apply_cover` renders the caller's image to
  `draft_cover.jpg` with ffmpeg, letterboxed to 1920x1080, and the CLI is out of
  the path entirely. `time` is accepted and ignored — it addressed a frame for
  the key nothing reads.

  This also makes the draft self-contained, which the old route was not: the
  image is rendered *into* the draft rather than referenced wherever the caller's
  file happened to live. And it delivers what `cover` was for in the first place
  — a thumbnail without opening the project: **confirmed in the project list**
  [proven], where a draft built with `cover` shows the image and a control built
  without it, side by side, does not.

  One correction to what this entry used to claim. Without a cover the thumbnail
  does **not** stay black — CapCut synthesises one from the timeline's first
  frame, for a draft it has never opened. The old "identical to a draft that
  asked for no cover at all" reading came from a cover image that was itself
  near-black, which is a test asset proving nothing. So `cover` does not fill a
  blank; it *replaces* the frame CapCut would have picked.

- **`opacity` composites opaque** **[proven failure]**. The one that needed an
  export to catch, and the strongest argument for the rule below it. `clip.alpha`
  is written correctly by compile; it **survives** a CapCut save, unlike `mix`;
  and the app's own Blend panel shows the reduced value on its Opacity slider --
  three separate signals of health, all of them wrong. The export composites 100%
  of the top clip. Measured on the exported frame at 4s of
  `eyecut-verify-compositing-v2`, an `alpha: 0.4` overlay over a base
  reconstructed with ffmpeg from the same source and in-point: where the base
  behind read `(161,61,60)`, the base alone would contribute `0.6 x 161 = 97` to
  the red channel, and the export read `70` -- below the floor any blend could
  produce. No seam appears at either edge of the base's 9:16 strip anywhere
  inside the overlay quad.

  The `mix`/`chroma` question -- *is eyecut failing to write a key CapCut needs?*
  -- is settled here by the same diff method those two are still waiting on: an
  opacity edit **made by hand in CapCut** produces a **byte-identical segment**
  (`clip.alpha: 0.4`, no extra key, no blend material, no new
  `extra_material_refs`). Same file, same render. There is nothing to write.
  Refused. To layer two shots, cut between them, or composite outside CapCut.
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
- **A `sticker` track draws nothing** **[proven failure]**. Milder than `sfx` in
  the file and identical on screen. `capcut add-sticker` writes the material and
  the track correctly, CapCut keeps both across a save without rewriting a byte,
  and the segments carry the right timings -- but the sticker material's `path`
  is the literal token `##_material_placeholder_<uuid>_##`. There is no file, so
  both segments render as bare video and the app badges each with its
  unresolved-resource icon. This is `bubble`'s boundary approached from the other
  side, and it is why hand-harvesting a resource id was never going to be enough:
  the id resolves to a catalogue entry, and the catalogue is not on disk. Put the
  graphic on a video track as an image or overlay clip instead.
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
  **[proven, confirmed in the app]**. Re-diffed since against a second hand-made
  mask: still the only field that differs, and stamping it is still not enough to
  make a mask draw (see the mask entry under proven failures).
- **A mask reaches the segment, shows in the panel, and masks nothing**
  **[proven failure, measured out of an export]**. Found by opening the drafts instead
  of the files, which is the only way it could have been. In a mask-only draft
  and in a four-key draft alike, the masked clip renders **full-frame** at a
  playhead inside it — no cut, no canvas showing through — while Video > Mask
  reads `Mask1 Circle`, ticked, with the size the spec asked for. Save, quit,
  reopen: unchanged.

  **The control is what makes this worth writing down.** A Circle mask applied
  **by hand in the app**, on a neighbouring clip of the same draft, does not draw
  either. And saved out, the two entries are the same shape — same
  `resource_id` (`7374021188315517456`), same bundle `path`, same key set, both
  carrying a `constant_material_id`, differing only in `config.width` (0.28 for
  eyecut's `size: 0.5`, 0.21 for the app's default). So this is **not** eyecut
  writing a mask CapCut won't read, and it is not the `check_flag` story either:
  it is the preview declining to draw masks at all.

  Which means the earlier "masks work" reading was the panel, again — the same
  mistake the note at the end of this document already records twice, made a
  third time on the same feature.

  **Then it was exported and measured**, the standard `opacity` had to be held
  to, and the answer is the same as the preview's. A 2s draft of one clip under a
  centred circle (`config.width` 0.28, `height` 0.5) exported at 720p renders the
  **whole frame**: luminance at mid-height is 111 at the extreme left edge and 53
  at the extreme right, where a 360px-wide ellipse would leave 0. The only black
  in the frame is the letterbox above and below a 2.40:1 source — which is
  exactly the trap here, because sampling the corners alone reads 0 and looks
  like a working mask. So a mask does nothing at all: not in the preview, not in
  the file's effect on the render, not in the export.

  What is *not* yet known is why a mask CapCut itself wrote is equally inert,
  which suggests the missing piece is somewhere neither the material nor the
  segment ref — the shape of the next question, not of this answer.
- **`tm_duration`**, which compile leaves at 0, listing the draft as 00:00.

### `check_flag`, and why a perfect material can do nothing

A **video material**'s `check_flag` is a bitmask of which effects CapCut will
honour on the segments using it. Everything compile and capcut-cli write leaves
it at `7`, and the app then ignores decoration it otherwise reads correctly.
Read off one hand-edited draft, three segments **[proven]**:

| segment | `check_flag` | |
|---|---|---|
| untouched | `7` | the baseline everything is built with |
| blend mode applied by hand | `15` | `7｜8` |
| chroma key applied by hand | `39` | `7｜32` |

This is what hid `chroma`. Its material was rewritten field-for-field into
CapCut's own shape, referenced from the right segment at the right position in
`extra_material_refs` — a draft *byte-identical* to the hand-keyed one except for
ids — and the app still showed the Chroma key box unticked. Setting bit 32 turned
it on. `repair_chroma_materials` now does both halves.

Two warnings. The flag lives on the **material**, not the segment, so segments
sharing a video material share it — compile writes one per segment, but a draft
CapCut has re-saved can collapse them, which is `collapse_videos`' problem. And
the bits are only known for these two: everything else is unmapped.

It is not a universal key. `opacity` was retried with bit 8 on the strength of
this discovery and got *worse*, not better — see its entry above.

### Reading the app, not the files

A draft that lints clean can still be wrong. `capcut lint` reported 0 errors on a
mask that had not been confirmed in the app, and file-level evidence was read as
proof twice — once concluding masks worked when unverified, once concluding they
were broken from a screenshot that showed the mask *selected for editing*, where
CapCut draws the full frame plus a guide rather than the cropped result.

**The app is the standard.** Lint and file structure are necessary and not
sufficient; a claim is `[proven]` only once it has been seen in CapCut.

And *seen* sometimes means measured. `opacity` passed every check short of that:
it survived the app's save, and the app's own inspector displayed the value back.
What caught it was an export, one frame, and arithmetic against a reconstructed
base -- because a screenshot of a dark overlay over a dark base is exactly the
kind of evidence already misread twice above.

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

**`speech` — voice-activity detection.** Scored every 25ms frame of a source on
three signals (voice-band energy ratio, spectral flatness, level above the file's
own 40th-percentile noise floor) and merged the runs, on plain `wave` and numpy
so it needed neither scipy nor librosa. It worked. It is cut by the same test
that cut `music_grid` and, later, the `caption` and `tts` ops: **it is about
audio, not about CapCut.** Finding a spoken line is a signal-processing job any
shell can do; writing the format is not. Being a CLI rather than an MCP tool was
not enough of a defence — `caption` was reachable that way too and went anyway.
Removed 2026-09-08; the implementation is in git history if the argument comes
back.

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

The `text-style` **operation** is refused for shape and scope: styling is per
text item via `textStyle`, matched to its built segment after the compile,
which a whole-spec operation cannot do. Its own shapes mislead on top of that:
flat keys (`{"op": "text-style", "target": ..., "bold": true}`, no `style`
wrapper) fail compile with `Cannot read properties of undefined (reading
'alpha')` **[proven, exit 1]**, because compile passes `operation.style`
straight to `setTextStyle` and it arrives `undefined`; and `style: {"bold":
true}` compiles clean (`ok: true`) and changes nothing — the material keeps
`bold: false` **[proven]** — because bold/italic/underline are `text-ranges`
vocabulary, which `setTextStyle` never reads. So eyecut refuses the op and
reaches the styling through the `textStyle` item key instead, applied after the
compile with the standalone `capcut text-style <project> <segment>` command.

The better end state for `register_media` is still upstreaming: `capcut fixture
<project> --out <dir>` on a CapCut-authored draft produces exactly the evidence
bundle `capcut-cli` says it lacks. Contributing it could move that tool out of
eyecut entirely.
