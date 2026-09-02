# Complete CapCut coverage — design

Every capability capcut-cli 0.21.1 can reach becomes reachable from a
`write_draft` spec. Twelve additions, one bug fix, and a post-compile layer
restructured so the thirteenth is cheap.

Scope is the **CLI's** surface, not the app's. Fonts (`capcut enums --fonts`
returns `[]`), store-downloaded assets, speed curves, motion tracking and
compositing stay unreachable and stay documented as such.

## The bug this starts with

`_segment_ids` keys on `(track type, index within track)` while iterating every
built track, so two video tracks collide: the second overwrites the first at
`(video, 0)`, `(video, 1)`, … `apply_masks` likewise enumerates items per track
and discards which track they came from.

A mask on a two-video-track spec is therefore applied to the **wrong segment**,
silently — the counts still agree, so the mismatch guard never fires. Latent
because every proven mask ran on a single video track; live because overlay and
picture-in-picture are built from a second named video track and are `[proven]`.

The key becomes `(type, track_index, item_index)`, where `track_index` counts
built tracks of that type. This lands first, alone, with a test that masks the
overlay of a two-video-track spec and asserts the base segment is untouched.

Twelve ops about to depend on this matcher is the reason it is fixed before
they are written, not after.

## Vocabulary

**Per-segment item keys**, applied after the compile:

| Key | Value | Tracks | CLI |
|---|---|---|---|
| `mix` | one of 12 blend modes | video | `mix-mode` |
| `chroma` | `{color, intensity}` | video | `chroma` |
| `bgBlur` | 1–4 | video | `bg-blur` |
| `crop` | `{ratio}` or `{rect}` | video | `crop` |
| `textRanges` | array of range styles | text | `text-ranges` |
| `bubble` | slug | text | `bubble-text` |

`opacity` and `rotation` are **compile item fields**, listed `[untested]` in
SPEC.md. They are validated here and proven in the verification drafts — not
routed through `capcut opacity`, which would be a second way to say what compile
already writes.

**New track types** `sticker` and `sfx`, alongside video/audio/text. Compile
builds neither, so eyecut creates them from the track's items after the compile.
They are the only additions that *add* segments rather than decorate them, so
they run after every per-segment op has matched — otherwise they shift the
positions the matcher depends on. Per-segment keys on their items are a
validation error, not a silent no-op.

**Top-level key** `cover: {path, time}` → `add-cover`.

`duplicate` is deliberately omitted. It clones a segment onto a new track; in a
declarative spec you write the second track, so it would be a second way to say
one thing.

**Stickers are asymmetric.** `add-sticker` takes a raw `<resource-id>` and there
is no `enums --stickers`, so the op is only usable with an id from
`harvest-enums` against a draft where one was placed by hand. Built and
documented as such rather than papered over.

## The ops layer

New module `eyecut/ops.py`, one table entry per per-segment op:

```python
ItemOp(key, command, tracks, positional, options, flags, validate, after)
```

This is the generalization of what already exists: `MASK_OPTIONS`/`MASK_FLAGS`,
`TEXT_STYLE_OPTIONS`/`TEXT_STYLE_FLAGS`, `ANIM_OPTIONS`. Those constants become
entry contents, so nothing about existing masks, text styles or animations
changes on the wire.

One walker, `apply_item_ops`, replaces `apply_masks`, `apply_text_styles` and
`apply_animations`. It builds the segment index once (each of the three
currently re-reads and re-parses `draft_info.json`), walks tracks and items in
spec order, and emits argv per key present. The two rules the three functions
each restate — skip-and-warn on a missing segment, `failed: {stderr[:120]}` on
non-zero — are stated once. `after` hooks fire once per op if anything applied,
which is how `stamp_mask_ids` keeps working.

`resync_speeds` stays separate: it repairs something compile wrote wrong rather
than applying something the spec asked for, and matches by segment id.

Without the table, twelve near-identical bodies each restate the count-mismatch
rule — the one rule SPEC.md says must never be guessed at.

### Order in `write_draft`

1. compile
2. `resync_speeds`
3. `apply_item_ops` — positions as compile left them
4. `apply_track_ops` — `sticker` / `sfx`, which add segments
5. `apply_cover`
6. `stamp_mask_ids` / `tm_duration` / mirror / prune

## Validation

`spec.py` loops over the same table `ops.py` exports, so a key cannot exist in
one and not the other — the drift that already put `text-style` and `textStyle`
at odds.

Closed lists are enforced: the 12 blend modes, `bgBlur` 1–4, `crop`'s six
ratios. Slugs for `sfx` and `bubble` are not, for the reason animation slugs are
not: `enums` is not the whole world once the app's store is involved. An unknown
slug exits non-zero and becomes a warning naming the item.

The existing distinct-`name` rule extends to the two new track types rather than
gaining a second check of its own.

## Verification

Four drafts under `scripts/`, each exercising a cluster in one open of CapCut:

1. **compositing** — two video tracks with `mix`, `chroma`, `bgBlur`, `crop`,
   `opacity`, `rotation`
2. **text** — `textRanges`, `bubble`, over existing `textStyle`
3. **tracks** — `sticker`, `sfx`, `cover`
4. **regression** — a mask on a two-video-track spec: the bug above

Everything ships `[untested]`; one commit promotes what the app confirms. Per
SPEC.md's own standard, lint and file structure are necessary and not
sufficient — file-level evidence has misread masks twice.

## SPEC.md corrections

- **Not reachable** lists blend modes and compositing. `capcut mix-mode` exists.
- `text-ranges` moves out of **[untested]**.
