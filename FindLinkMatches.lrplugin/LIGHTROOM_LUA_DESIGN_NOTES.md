# Lightroom Lua Plugin Design Notes

This document captures design decisions and lessons learned while building `FindLinkMatches.lrplugin`, especially where Lightroom SDK behavior was surprising.

## Why we use `pcall` / `LrTasks.pcall`

We use protected calls so plugin errors do not tear down the task and silently lose state mid-run.

- In Lightroom task contexts, `LrTasks.pcall(...)` is preferred over base Lua `pcall(...)` for yield-safe behavior.
- In `import_recovery_csv.lua`, write-access wrappers and catalog operations are intentionally protected so each row can fail independently and still produce logs/counts.
- Recent history:
  - `7dcf7a4` introduced write-access guarding and protected execution.
  - `642e267` switched wrapper calls from `pcall` to `LrTasks.pcall` for task/yield safety.

Practical effect: instead of aborting the whole import when one operation errors, we capture the error, continue processing, and emit actionable failure logs.

## Catalog write-access design

`catalog:withWriteAccessDo(...)` is centralized behind `withCatalogWriteAccess(...)` in `import_recovery_csv.lua`.

Reasons:

- Lightroom requires explicit write transactions for catalog mutations.
- We need consistent timeout handling (`status == "aborted"`).
- We want one place to convert SDK exceptions into normal `(ok, err)` flow.

This keeps create/import/add-to-collection operations behaviorally consistent and easier to reason about.

## Missing-photo detection: what we tried

The plugin currently uses multiple approaches in different scripts:

- `main.lua` and `check_same_or_better.lua`: `photo:checkPhotoAvailability()` (wrapped as `isPhotoPresent`/`isPhotoMissing`)
- `write_missing_csv.lua`: `photo:getRawMetadata("isMissing") == true`
- Operationally, we still rely on Lightroom’s own **Find All Missing Photos** and/or AnyFilter to produce the trusted working set.

## Current reliability finding (still unresolved)

We do **not** yet have high-confidence proof of which Lightroom Lua API is most reliable across real catalogs for missing-state detection:

- `checkPhotoAvailability()`
- `getRawMetadata("isMissing")`

At the moment, the safest workflow is:

1. Build the missing-photo list with Lightroom-native tools (Find All Missing Photos / AnyFilter).
2. Use plugin/script steps on that known-missing selection or exported CSV.

## Why this is hard in practice

- Lightroom metadata can lag UI state in some flows.
- Missing-state behavior can vary with smart previews, volume mount timing, and folder sync history.
- Candidate matching is sensitive to metadata quality (timestamps/camera/dimensions), so false negatives in “missing” classification ripple into later steps.

## Suggested validation work to close the gap

To settle missing-state reliability, run a repeatable comparison on the same catalog sample:

1. Use Find All Missing Photos (and optionally AnyFilter) as baseline.
2. On that same population, compare:
   - `checkPhotoAvailability()`
   - `getRawMetadata("isMissing")`
3. Log disagreement cases with path, file type, smart preview status, and whether volume was mounted.
4. Re-test after catalog restart and after folder sync.

Once disagreement rates are understood, we can standardize one method (or a fallback sequence) across plugin scripts.
