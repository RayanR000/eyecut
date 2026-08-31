# Sanitized CapCut draft bundle

This folder was produced by `capcut fixture`. It contains **only** the timeline
JSON files from a real project, with user home paths, email addresses and the
device identifiers CapCut stamps into every draft (`device_id`,
`mac_address`, `hard_disk_id`) redacted. No media from `assets/` was copied.

- Detected app version: 9.1.0
- Modern storage layout (CapCut >= 8.7): yes
- Nested Timelines/ layout captured (issue #50): Timelines/project.json, Timelines/797B3727-6267-4B64-8D5F-0B41780A1977/draft_info.json

## What to do with it

1. Open the files and confirm nothing private remains (names in titles, custom
   absolute paths the redactor may not know about, etc.). Edit freely — only the
   storage *structure* matters for the bug, not the content.
2. Attach this folder to the relevant issue (CapCut 8.7 Windows: issue #35).
3. With a real app-created bundle committed as a fixture, the version can move
   from "synthetic-tested" to "fixture-tested" in docs/version-support.md.

## Mask-keyframe harvest (issue #44)

`mask-keyframe-report.json` maps every mask material and keyframe structure
found in this bundle. The mask-geometry keyframe encoding has no public ground
truth, so the CLI cannot write mask keyframes yet — a real app-authored capture
is the missing artifact.

No mask-keyframe structures were found in this draft. To help #44: animate a
mask in the desktop app (two position keyframes are enough), save, and re-run
`capcut fixture` on that draft.

## What this does NOT prove

A sanitized bundle proves the *on-disk shape*. It does not prove the CLI's
edits open correctly in the CapCut desktop app on your version — that still
needs a manual open-in-CapCut check on the real machine.
