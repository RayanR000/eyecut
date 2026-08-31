# Where this came from

Ground truth for stage 5 (`build`). Captured 2026-08-31 from CapCut project
`0830`, authored by hand in **CapCut International 9.1.0 on macOS** — the exact
build on which missing `draft_materials` registration causes the relink-every-clip
prompt. Produced with `capcut fixture` (capcut-cli 0.21.1), then scrubbed further
by hand:

- media filenames replaced with `source_1080p.mp4` / `track.mp3`
- `out_dir` in `SANITIZE_REPORT.json` redacted (it held a local username)

`capcut fixture`'s own redaction covers home paths, emails and device ids, but
**not filenames** — check those before sharing any future capture.

## Why it's here

`draft_meta_info.json` → `draft_materials` holds two real, app-written entries
(one `video`, one `music`). Neither capcut-cli nor VectCutAPI writes that section
(see `docs/SPEC.md`, stage 5), so this is the only attestation of the correct
entry shape. `tests/fixtures/draft_materials_entries.json` is the distilled form
for tests; this bundle is the unabridged original, including the surrounding
timeline files and the nested `Timelines/` layout.

Read-only reference. Nothing here is loaded at runtime.

`README.md` in this folder is capcut-cli's own, addressed to its maintainers —
its instructions about attaching the bundle to upstream issues are theirs, not
ours. Nothing has been sent upstream.
