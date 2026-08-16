
# Personal Workflow for RawPhotos / Lightroom Recovery

## Purpose

This document describes the intended personal workflow for recovering missing files in the Lightroom catalog, especially files expected under `RawPhotos`.

The immediate goal is to make practical progress in Lightroom by reducing the number of missing photos. The workflow deliberately avoids turning the project into an endless tooling effort. We will use the existing recovery outputs, make cautious tests, restore files where possible, and defer duplicate cleanup until later.

The guiding principle is:

> Restore files to the exact paths Lightroom already expects whenever possible. Import new candidate files only when restoring/relinking is not practical. Defer duplicate deletion until after the missing-photo count is much lower.

At this stage, the priority is to get Lightroom back into a healthier state. Cleanup can happen later, ideally as a separate, more manual phase that Debbie can help with.

---

## High-level order

1. Run `relink_good_matches.sh`.
2. Test a few entries from `higher_resolution.sh`.
3. If Lightroom handles them well, run the full `higher_resolution.sh`.
4. Parse the shell files to identify date folders that probably contain scan/import duplicates.
5. Use those folder lists to guide `Check For Same or Better`.
6. Re-export a fresh missing-photo list from Lightroom.
7. Search Dropbox on Giantsteps and the Canmore Data Centre for the remaining missing files.
8. Copy/relink recoverable external files into the Lightroom folder.
9. Decide how to handle original vs combined import CSVs.
10. Process `import_same_format_higher_resolution.csv`.
11. Process `import_other_formats.csv`.
12. Defer duplicate deletion and cleanup to a later pass.

---

# Phase 1 — Apply the safest relinks first

## 1. Run `relink_good_matches.sh`

Start with the exact/good matches.

These are the safest because they should restore files at the exact paths Lightroom already expects, using confirmed matches.

```bash
bash relink_good_matches.sh
```

After running this:

1. Open Lightroom.
2. Run **Library → Find All Missing Photos** again.
3. Confirm that the number of missing photos drops.
4. Spot-check a few recovered photos.

Things to check:

- Does Lightroom show the photos as no longer missing?
- Are edits, ratings, collections, and metadata still present?
- Are previews acceptable?
- Does Lightroom behave normally in Loupe view?
- Does Lightroom behave normally when opening the files in Develop?

This phase should provide the first real Lightroom-side progress.

---

# Phase 2 — Carefully test higher-resolution replacements

## 2. Test one or two lines from `higher_resolution.sh`

Before running the whole `higher_resolution.sh`, test a very small sample manually.

The purpose is to see how Lightroom reacts when the file at the expected path is restored, but the actual file has a higher resolution than Lightroom’s current catalog metadata says.

Questions to answer:

- Does Lightroom automatically detect the new dimensions?
- Does it update metadata after viewing the file?
- Does it update only after entering Develop?
- Does it require building previews?
- Does it show any warning?
- Does it get confused because the dimensions differ?
- Does it keep edits and catalog associations intact?

Suggested approach:

1. Open `higher_resolution.sh`.
2. Pick one or two simple, representative entries.
3. Copy those commands into a temporary test script, or paste them manually into Terminal.
4. Run only those one or two commands.
5. Open Lightroom and inspect those specific photos.

Example temporary test script:

```bash
#!/bin/bash

# Paste one or two selected commands from higher_resolution.sh here.
```

Run it with:

```bash
bash test_higher_resolution_sample.sh
```

---

## 3. Lightroom checks after the small test

For each tested photo:

1. Locate the photo in Lightroom.
2. Confirm it is no longer missing.
3. Check the Metadata panel:
   - width
   - height
   - file type
   - capture date
4. Open it in Loupe view.
5. Open it in Develop.
6. If needed, try preview-related actions:
   - **Library → Previews → Build Standard-Sized Previews**
   - **Library → Previews → Build 1:1 Previews**

Be cautious with:

- **Metadata → Read Metadata from File**

That may be useful in some cases, but it can affect catalog-side metadata. Use it only deliberately.

The main question is whether Lightroom’s catalog record remains healthy even though the restored file is higher resolution than what Lightroom previously knew.

---

## 4. If the test is successful, run all of `higher_resolution.sh`

If Lightroom behaves well with the test entries, proceed with the full higher-resolution relink batch.

```bash
bash higher_resolution.sh
```

Then repeat the Lightroom checks:

1. Run **Library → Find All Missing Photos**.
2. Confirm the missing count drops.
3. Spot-check a range of restored files.
4. Confirm that previews, metadata, edits, and Develop behavior remain healthy.

---

# Phase 3 — Identify scan/import-date folders that probably contain duplicates

## 5. Analyze where scan-directory files were linked into date folders

After processing both:

- `relink_good_matches.sh`
- `higher_resolution.sh`

we should look for cases where files from scan/storage/archive folders were used to restore files in date-specific folders.

Important source folders may include:

- `Photos/OldPhotos`
- `RawPhotos/RawScans`
- `JpgFromTiff/RawScans`
- other old scan/import/archive folders

These cases likely mean that Lightroom had catalog entries in date-specific folders corresponding to files that were scanned or processed on those dates, rather than true capture dates.

The goal is **not** to delete anything yet.

The goal is to make a prioritized list of date-specific folders that deserve review with **Check For Same or Better**.

---

## 6. Build the folder list by processing the shell files

We should build this list by processing the generated shell files, not by manually reading them.

Input shell files:

```text
relink_good_matches.sh
higher_resolution.sh
```

Possibly also:

```text
resolution_mismatch.sh
```

if we want to include all resolution-mismatch cases, not just the higher-resolution subset.

The helper scripts should parse each active `ln` or `cp` command and extract:

```text
source_path
destination_path
destination_folder
source_category
command_file
```

Then they should filter for rows where:

- the **source path** is from a scan/archive/import holding area, and
- the **destination path** is a date-specific Lightroom folder.

Example:

```text
source:      /Volumes/Ladyhawke/RawPhotos/RawScans/...
destination: /Volumes/Ladyhawke/RawPhotos/2008/2008-10-01/...
```

This should produce a summary grouped by destination folder:

```text
destination_folder,count,example_source_category
/Volumes/Ladyhawke/RawPhotos/2008/2008-10-01,47,RawPhotos/RawScans
/Volumes/Ladyhawke/RawPhotos/2008/2008-11-26,18,RawPhotos/RawScans
/Volumes/Ladyhawke/RawPhotos/...,12,Photos/OldPhotos
```

The count matters because it tells us where the biggest clusters are. Those high-count date folders are probably the best candidates for **Check For Same or Better**.

---

## 7. Optional Excel / pivot-table workflow

A useful intermediate output would be a CSV like:

```text
source_path,destination_path,destination_folder,source_category,command_file
```

Then it could be opened in Excel and summarized with a pivot table.

Suggested pivot table:

- Rows: `destination_folder`
- Columns or filters: `source_category`
- Values: count of files
- Optional filter: `command_file`

For example, the command file filter could distinguish:

- `relink_good_matches.sh`
- `higher_resolution.sh`
- `resolution_mismatch.sh`

This gives a quick way to inspect clusters and decide which folders are worth processing first.

However, the preferred workflow should still be repeatable:

1. Parse shell files.
2. Write a detailed CSV.
3. Write a summarized folder-count CSV.
4. Use the summarized list to guide Lightroom work.

---

## 8. Use `Check For Same or Better` before deleting anything

For each high-priority folder on the generated list:

1. In Lightroom, select the photos in that folder.
2. Run:

   **Library → Plug-in Extras → Check For Same or Better**

3. Review the generated **Has Same Or Better** collection.
4. Do **not** bulk-delete automatically.
5. Defer duplicate deletion to a later cleanup phase.

The principle here is:

> Restore missing entries first. Delete duplicates later.

This prevents accidentally removing catalog records before we are confident the restored/relinked images are safe.

---

# Phase 4 — Generate a fresh missing-photo list

## 9. Re-run Lightroom’s missing-photo scan

After the local relinks and higher-resolution restores, do not rely only on the old `Still_Missing_Photos.csv`.

Instead, create a fresh list directly from Lightroom.

In Lightroom:

1. Run **Library → Find All Missing Photos**.
2. Select all photos in the Missing Photographs collection.
3. Run the CSV export plugin.
4. Save the new file as something like:

```text
data/Missing_Photos_after_local_relinks.csv
```

This becomes the new authoritative missing-photo list.

The old `Still_Missing_Photos.csv` may still be useful for comparison, but Lightroom’s current state is what matters.

---

# Phase 5 — Search Dropbox and Canmore Data Centre for remaining missing files

## 10. Search the Dropbox folder on Giantsteps

Next, search the Dropbox folder on Giantsteps for remaining missing photos.

There are two likely approaches.

### Option A — Mount Giantsteps Dropbox on Alpenglow

If the Dropbox folder can be mounted on Alpenglow, then it may be possible to run the normal missing-photo search against that mounted path.

Things to check:

- Is the mounted Dropbox folder visible as a normal filesystem path?
- Can files be read normally?
- Does Spotlight indexing work on the mounted volume?
- Is `mdfind` usable there?
- Is it fast enough?
- Are hardlinks possible, or will copies be required?

If it is mounted as a different volume, then restore operations may need to use copy rather than hardlink.

### Option B — Run the search locally on Giantsteps

If the Dropbox folder is best accessed directly on Giantsteps, then it may be better to run the search there and generate candidate lists, then copy the output CSV/scripts back to Alpenglow for review.

---

## 11. Search the Canmore Data Centre folders

Then do the same for the Canmore Data Centre drives.

Open questions:

- Can the Canmore drives be mounted directly on Alpenglow?
- If mounted, are they readable as normal paths?
- Does Spotlight work on those mounted paths?
- Are they on different volumes, requiring copies instead of hardlinks?
- If direct mounting is not practical, should files be copied via `scp`, `rsync`, or another mechanism?

The goal is to search those locations for the current missing files and produce new candidate outputs.

Keep external search results separate at first.

Example output organization:

```text
outputs/dropbox_giantsteps/relink_good_matches.sh
outputs/dropbox_giantsteps/higher_resolution.sh
outputs/dropbox_giantsteps/import_same_format_higher_resolution.csv
outputs/dropbox_giantsteps/import_other_formats.csv
outputs/dropbox_giantsteps/Still_Missing_Photos.csv

outputs/canmore/relink_good_matches.sh
outputs/canmore/higher_resolution.sh
outputs/canmore/import_same_format_higher_resolution.csv
outputs/canmore/import_other_formats.csv
outputs/canmore/Still_Missing_Photos.csv
```

This makes it easier to understand where each recovery candidate came from.

---

## 12. Copy recovered files into the Lightroom folder before moving on to CSV imports

After searching Dropbox and Canmore, but before moving on to the import CSV workflows, first handle files that can restore Lightroom’s existing missing paths.

This means processing the generated relink/copy shell files from those searches.

Because these files are likely on different machines or volumes, hardlinks may not be possible. Depending on access method, the operation may need to use:

- `cp`
- `rsync`
- `scp`
- a mounted-volume copy
- or a generated copy script run from the machine that can see both source and destination

The desired end state is still:

> A valid file exists at the exact path Lightroom already expects inside the Lightroom photo folder.

Process external-source matches in this order:

1. Review the external `relink_good_matches.sh`.
2. Convert commands to safe copy commands if needed.
3. Copy files into the expected `/Volumes/Ladyhawke/...` Lightroom paths.
4. Test with a small batch.
5. Re-run **Find All Missing Photos** in Lightroom.
6. If successful, process the larger batch.
7. Repeat for external `higher_resolution.sh`, again starting with a small test.

Only after these copy/relink recoveries are complete should we move on to importing extra candidate files from CSVs.

---

## 13. Open question: original CSVs vs concatenated CSVs from all searches

Before processing the import CSVs, decide whether to use the original local CSV outputs or combine additional results from Giantsteps and Canmore.

### Option A — Use original local CSV outputs only

Use the original files generated from the Ladyhawke/local search:

```text
import_same_format_higher_resolution.csv
import_other_formats.csv
```

This is simpler and avoids mixing sources.

### Option B — Concatenate additional results from Giantsteps and Canmore

Combine equivalent CSVs from multiple searches:

```text
local/import_same_format_higher_resolution.csv
dropbox_giantsteps/import_same_format_higher_resolution.csv
canmore/import_same_format_higher_resolution.csv
```

Similarly combine:

```text
local/import_other_formats.csv
dropbox_giantsteps/import_other_formats.csv
canmore/import_other_formats.csv
```

This could be useful because the external searches may find better candidates than the original local search.

However, if we concatenate, we should add a column such as:

```text
search_source
```

Possible values:

```text
local_ladyhawke
dropbox_giantsteps
canmore
```

That way, when the rows are reviewed in Excel or split into smaller CSVs, it remains clear where each candidate came from.

Recommended approach:

1. Keep original outputs separate.
2. Copy/relink anything that can restore an existing missing path.
3. Re-run Lightroom missing scan.
4. Then decide whether to concatenate import CSVs.
5. If concatenating, add `search_source` before combining.
6. Sort and split the combined CSVs manually before import.

---

# Phase 6 — Process `import_same_format_higher_resolution.csv`

## 14. Import same-format higher-resolution files in place

After relinks/copies are exhausted, process:

```text
import_same_format_higher_resolution.csv
```

or a combined/split version of it if using results from Giantsteps and Canmore.

These are cases where there is a same-format file with higher resolution than the one that got matched or restored.

Most of these are likely good candidates for importing directly in place.

Use the Lightroom plugin workflow:

**Library → Plug-in Extras → Import photos from recovery CSV**

Advantages:

- Files stay where they already are.
- Lightroom imports them without moving or copying.
- Imported files are placed into a recovery collection.
- The collection can be used to compare against the missing/recovered entries.
- The old duplicate entries can later be reviewed and deleted manually.

This is probably better than gathering them into a temporary import folder, because these are same-format higher-resolution candidates and may already be in meaningful locations.

---

## 15. Rename the import collection after each run

After each run of **Import photos from recovery CSV**, rename the generated collection before running the import again.

Example collection names:

```text
new-files-imported — same format higher res — local Ladyhawke
new-files-imported — same format higher res — Dropbox Giantsteps
new-files-imported — same format higher res — Canmore
new-files-imported — other formats — JpgFromTiff
new-files-imported — other formats — RawPhotos in place
```

This preserves each previous review set and allows the next import run to create or reuse a fresh working collection.

The purpose is to keep the Lightroom review workflow organized:

- one collection per import batch
- no accidental mixing of sources
- easier comparison against old/missing entries
- easier manual cleanup later

---

## 16. Review the collection after each import

After each import:

1. Rename the import collection to describe the batch.
2. Open that collection.
3. Compare imported higher-resolution files with the old/recovered entries.
4. Confirm they are really better versions.
5. Defer deletion of old entries until the later duplicate-cleanup phase.

Again, the immediate goal is recovery and visibility in Lightroom, not final cleanup.

---

# Phase 7 — Process `import_other_formats.csv`

## 17. Manually sort and split `import_other_formats.csv`

This file needs more manual handling because it contains cross-format candidates.

Examples:

- A missing RAW may have a JPEG candidate.
- A missing TIFF may have a JPEG derived from it.
- Files may come from `JpgFromTiff`.
- Some may be in normal photo folders.
- Some may be in `RawPhotos`.
- Some may be in archive or scan folders.
- Some may come from Dropbox or Canmore if external search results are concatenated.

This CSV should probably be split into multiple working files.

Suggested categories:

```text
import_other_formats__jpgfromtiff_gather.csv
import_other_formats__ladyhawke_photos_import_in_place.csv
import_other_formats__ladyhawke_rawphotos_import_in_place.csv
import_other_formats__dropbox_or_canmore_import_in_place.csv
import_other_formats__manual_review.csv
```

If the CSVs are concatenated from multiple searches, include `search_source` in the split criteria.

---

## 18. Handle `JpgFromTiff` entries by gathering files

Many entries in `JpgFromTiff` probably should not be imported in place from that folder.

Instead, likely workflow:

1. Use the gather workflow to collect them into a temporary import folder.
2. Import them using Lightroom’s normal Import dialog.
3. Let Lightroom place/copy them into the desired date folder according to normal import settings.

Conceptual command:

```bash
python3 gather_import_files.py import_other_formats__jpgfromtiff_gather.csv \
  --copy-across-volumes false \
  --tempdir /path/to/import_folder
```

Then in Lightroom:

1. Use the normal **Import** dialog.
2. Import from that gathered folder.
3. Apply normal import settings/presets.
4. Confirm the files land in the desired date-specific locations.

This is useful where `JpgFromTiff` is more of a derived/export holding area than a final archival location.

After importing, rename the resulting collection or otherwise record the batch, so that it remains clear which files came from the `JpgFromTiff` workflow.

---

## 19. Import some `Ladyhawke/Photos` and `Ladyhawke/RawPhotos` entries in place

For candidates already located in meaningful final locations, especially:

```text
/Volumes/Ladyhawke/Photos/...
/Volumes/Ladyhawke/RawPhotos/...
```

it may be better to import them directly in place.

This is especially attractive for files in:

```text
/Volumes/Ladyhawke/RawPhotos/...
```

because importing in place allows Lightroom to catalog them where they already live, and the plugin-created collections make it easier to compare them with the missing entries.

Use:

**Library → Plug-in Extras → Import photos from recovery CSV**

on a split CSV containing only the rows intended for in-place import.

After each run, rename the generated collection to describe the batch before running the next import.

---

# Phase 8 — Defer deletion and cleanup

## 20. Do not delete duplicates yet

At this stage, avoid turning this into a full duplicate-management project.

Do not yet try to automatically remove:

- duplicate scan entries
- duplicate date-folder entries
- old smart-preview placeholders
- lower-resolution catalog records
- redundant imported copies

Instead, build review collections and folder lists.

Deletion can be a separate later phase, after:

1. The missing-photo count is much lower.
2. Lightroom has been tested with the restored files.
3. Higher-resolution replacements are confirmed safe.
4. Debbie can help with manual review.

---

# Working checklist

## Immediate next steps

1. Run:

   ```bash
   bash relink_good_matches.sh
   ```

2. In Lightroom:
   - run **Find All Missing Photos**
   - confirm the missing count drops
   - spot-check restored files

3. Test one or two commands from:

   ```text
   higher_resolution.sh
   ```

4. Check Lightroom’s behavior with those higher-resolution files.

5. If successful, run:

   ```bash
   bash higher_resolution.sh
   ```

6. Re-run **Find All Missing Photos** and export a fresh CSV.

---

## Next batch of work

7. Parse `relink_good_matches.sh` and `higher_resolution.sh` with scripts.

8. Generate:
   - detailed source/destination CSV
   - summarized destination-folder count CSV

9. Use the summarized count list to pick folders for **Check For Same or Better**.

10. Run **Check For Same or Better** on selected high-count folders, but do not delete yet.

11. Use the fresh missing-photo CSV to search:
    - Dropbox on Giantsteps
    - Canmore Data Centre folders

12. Keep external search outputs separate by source.

13. Copy/relink external matches into Lightroom’s expected folders before importing from CSVs.

14. Re-run **Find All Missing Photos** again.

15. Decide whether to:
    - use original import CSVs only, or
    - concatenate local + Giantsteps + Canmore import CSVs with a `search_source` column

16. Process `import_same_format_higher_resolution.csv` mostly by importing in place.

17. Rename the import collection after each import run.

18. Manually split `import_other_formats.csv`:
    - gather/import `JpgFromTiff` files
    - import in place for good `Photos` / `RawPhotos` candidates
    - separately handle Dropbox/Canmore candidates
    - leave questionable rows for manual review

19. Defer duplicate deletion to a later cleanup pass.

---

# Key principle

The workflow should stay focused:

> First restore missing Lightroom references. Then import better candidates where relinking is not possible. Only after that, clean up duplicates.

This avoids spending more time building tooling while Lightroom still has tens of thousands of missing files.
