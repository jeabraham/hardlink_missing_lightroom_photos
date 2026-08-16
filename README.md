# Lightroom Missing Photo Recovery

Tools for recovering Lightroom Classic photos whose originals are reported as missing,
while preserving catalog records, edits, collections, and cloud relationships.

The guiding principle is: **restore or hardlink a valid file to the exact pathname
Lightroom already expects** — rather than importing a replacement as a new photo.

See [DESIGN.md](DESIGN.md) for the full rationale, background, and planned future phases.

---

## Recovery workflow

```
0. Pre-process: identify and remove redundant catalog entries  ← Check For Same or Better (Lightroom plugin)
1. Export missing-photo paths from Lightroom (see below)
2. Recover originals from Time Machine           ← recover_from_timemachine.py
3. Re-run Lightroom "Find All Missing Photos"
4. Find identical copies elsewhere and hardlink  ← relink_missing_photos.py
5. Handle remaining gaps manually
```

---

## Step 0 — Pre-processing: identify and remove redundant catalog entries

Before starting the main recovery workflow you may want to clean up catalog entries
that are no longer needed.  A common situation: you previously exported a folder of
**smart previews** (lower-resolution safety copies) called, for example,
`downloaded-smart-previews`.  Many of those entries may now be redundant because:

- the higher-resolution original already exists elsewhere in the catalog, or
- you copied the emergency copy back into its proper folder.

In these cases there is no value in keeping the missing-entry; you can simply delete
it from the catalog.

**Plugin:** `FindLinkMatches.lrplugin`

1. Select all photos in the folder you want to clean up (e.g. `downloaded-smart-previews`).
2. Run **Library → Plug-in Extras → Check For Same or Better**.

The plugin searches the active catalog for non-missing photos whose:
- filename stem matches (extension-agnostic — smart-preview JPEGs match original ORF/NEF/etc.),
- capture timestamp is within 5 minutes,
- camera model matches, and
- resolution is the same or higher.

Photos that meet these criteria are added to a special collection called
**"Has Same Or Better"** and both those and their matches are added to a collection
called **"Same Or Better Comparisons"**. You can then review those collections and
copy develop settings, investigate other metadata, and then delete the
redundant catalog entries manually — especially entries that are already missing,
since there is no need to keep a catalog record pointing to a missing file when a
same-or-better image already exists in a proper folder.

A debug log is written to `~/Desktop/check_same_or_better_debug.log`.

---

## Step 1 — Export missing-photo paths from Lightroom

Use Lightroom Classic's built-in **"Find All Missing Photos"** (Library menu) 
or the excellent [AnyFilter plugin](https://johnrellis.com/lightroom/anyfilter.htm) to
populate the `Missing Photographs` smart collection.

Then export the list using this repository's Lightroom plugin:

1. Select all photos in the `Missing Photographs` collection.
2. Run **Library → Plug-in Extras → Write CSV File for Photos**.
3. The plugin writes `Missing_Photos.csv` to your Desktop with the columns:
   - `Photo`
   - `URL`
   - `Filename`
   - `Date/Time Original (Capture)`
   - `Width`
   - `Height`
   - `Camera Make`

Save the file as `data/Missing_Photos.csv` (or pass a custom path to the scripts).

---

## Phase 1 — Recover originals from Time Machine

**Script:** `recover_from_timemachine.py`  
**Status:** Implemented and tested against APFS snapshots from Time Machine and from Carbon Copy Cloner
**Dependencies:** Python 3.9+, standard library only.

### inspect — discover the backup structure

```bash
python3 recover_from_timemachine.py inspect /Volumes/iMacBackup3
```

Walks the Time Machine volume and reports the layout type, host/computer names,
available snapshots, and which snapshots contain a `Ladyhawke` volume directory.
Read-only; modifies nothing.

### scan — find missing originals in Time Machine

```bash
python3 recover_from_timemachine.py scan \
    data/Missing_Photos.csv \
    /Volumes/iMacBackup3
```

For each entry in the missing-photos CSV, derives the relative path under
`/Volumes/Ladyhawke` and searches snapshots (newest first).

Search strategies:

- `--search-mode full` (default): exhaustive search across all snapshots.
- `--search-mode anchored`: two-phase search for faster scans on large backup
  histories:
  1. Probe monthly anchor snapshots (first chronological snapshot in each month),
     stepping by `--month-interval` months (`1` = every month, `2` = every other
     month, etc.).
  2. Once a monthly anchor contains the file, scan forward in time toward newer
     snapshots to find the newest available copy.

Example anchored scan:

```bash
python3 recover_from_timemachine.py scan \
    data/Missing_Photos.csv \
    /Volumes/iMacBackup3 \
    --search-mode anchored \
    --month-interval 1
```

Each record is classified as one of:

| Status | Meaning |
|--------|---------|
| `FOUND_IN_TIME_MACHINE` | Exactly one snapshot contains the file |
| `MULTIPLE_TIME_MACHINE_VERSIONS` | Multiple snapshots match; newest is used |
| `NOT_FOUND_IN_TIME_MACHINE` | File absent from all inspected snapshots |
| `CURRENTLY_PRESENT` | File already exists on Ladyhawke — no recovery needed |
| `NON_LADYHAWKE_PATH` | Expected path is on a different volume; out of scope here |

Writes `data/timemachine_recovery_candidates.csv` (columns: `status`,
`expected_path`, `relative_path`, `backup_path`, `backup_date`, `file_size`,
`mtime`, `notes`) and prints a summary.

Use `--output <path>` to write the report somewhere else.

Tradeoff:
- `full` mode is exhaustive and best when you want complete historical coverage.
- `anchored` mode usually runs faster but may skip matches that only exist in
  non-anchor months before the first anchored hit.

### restore — copy originals back (dry-run by default)

```bash
# Preview only — no files are written:
python3 recover_from_timemachine.py restore \
    --report data/timemachine_recovery_candidates.csv

# Actually copy (requires explicit flag):
python3 recover_from_timemachine.py restore \
    --report data/timemachine_recovery_candidates.csv \
    --execute
```

For every `FOUND_IN_TIME_MACHINE` or `MULTIPLE_TIME_MACHINE_VERSIONS` record,
copies the backup file to the exact pathname Lightroom expects, using a normal
copy (`shutil.copy2`) — never a hardlink to the backup volume.

Safety guarantees:
- `--dry-run` is the default; `--execute` must be passed explicitly.
- The Time Machine backup is never modified.
- Existing destination files are never overwritten.
- Source is verified to be a regular file before any copy.
- Missing destination directories are created automatically.
- File metadata (timestamps) is preserved.
- Every operation is logged to `data/restore_log.csv`.

Add `--verify-hash` to calculate a SHA-256 digest for each file copied.

### hash — manual integrity check

```bash
python3 recover_from_timemachine.py hash <file1> [<file2> ...]
```

Prints the SHA-256 hash of each file.  Useful for comparing a backup copy against
a restored copy or another candidate.

---

## Phase 2 — Find identical copies and hardlink them

After recovering everything possible from Time Machine, re-run
**Library → Find All Missing Photos** in Lightroom, then export a fresh CSV
using **Write CSV File for Photos**.

### Step 2a — Find candidates with Python

**Script:** `relink_missing_photos.py`  
**Status:** Implemented; tested in various scenarios, yet the restore functions (shell commands) have not yet been tested fully within Lightroom.  
**Dependencies:** Python 3.9+, `pandas`, `python-dateutil`, `exiftool` (CLI tool).

```bash
# Install Python dependencies:
source setup.env   # or: pip install pandas python-dateutil

# Walk a directory tree to find candidates (--search-root required in this mode):
python3 relink_missing_photos.py data/Missing_Photos.csv \
    --search-root /Volumes/Ladyhawke

# Alternative: use macOS Spotlight (mdfind) instead of walking the tree:
python3 relink_missing_photos.py data/Missing_Photos.csv --mdfind
```

Matches each missing photo against candidates using `exiftool`-extracted EXIF data
(timestamp, camera make, width, height).  Ignores `.xmp` sidecars and requires
extension/type matching (case-insensitive) before generating hardlink commands.

Unlike the plugin, this step is not limited to what Lightroom currently returns
from catalog search and emits separate outputs for exact relinks, resolution
mismatches, and still-missing records.

#### Candidate discovery modes

| Mode | Flag | Description |
|------|------|-------------|
| Directory walk (default) | `--search-root PATH` | Indexes all files under `PATH` by filename stem at startup, then looks up candidates in memory. Fast for repeated runs over a large tree. |
| Spotlight | `--mdfind` | Uses macOS `mdfind -name` to find candidates per photo. `--search-root` is optional (constrains the Spotlight scope). No upfront indexing; useful when the search root is very large or unknown. |

#### Options

```
--search-root PATH       Root directory to index for candidates (required unless --mdfind).
--mdfind                 Use macOS Spotlight instead of a directory walk.
--copy-across-volumes    When a candidate is on a different volume than the target,
                         emit a 'cp' command instead of 'ln'.
--output-dir DIR         Write all output files to DIR instead of the current directory.
--test-n N               Process a random sample of N rows (implies --debug when N < 20).
--exclude-sources PATH   Exclude directory subtrees from candidate indexing/search.
--exclude-targets PATH   Skip missing-photo entries whose path contains this string.
--skip-rows N            Skip the first N data rows in the CSV (used when resuming).
--rows-to-process N      Process at most N rows after skipping (used when resuming).
--append-outputs         Append to existing output files instead of overwriting.
--debug                  Print full candidate match details and rejection reasons.
--verbose-debug          Print timestamped trace messages around each blocking call
                         (mdfind, exiftool, dateparser) to pinpoint hangs.
--allow-timezone-mismatches
                         Accept candidates whose timestamp differs by an even half-hour
                         offset (±30-min granularity, up to ±26 h) with a residual ≤1 min.
                         Useful for cameras that store local time without timezone info.
```

#### Outputs (written to `--output-dir`, default: current directory)

| File | Contents |
|------|----------|
| `relink_good_matches.sh` | `ln` (or `cp` with `--copy-across-volumes`) commands for confirmed matches (exact resolution). Best match is the active command; alternate candidates appear as `# Alt:` comment lines. |
| `resolution_mismatch.sh` | Best-guess links where resolution differs (same file extension/type only). Each entry shows the Lightroom-known resolution as `LR:WxH` in the comment — note that Lightroom sometimes only knows the Smart Preview resolution, so the matched file on disk may have a *higher* resolution than what Lightroom reports, which is normal and desirable. Comment lines include alternate candidates prefixed `# Alt:`. Entries where the candidate dimensions exceed the `LR:` dimensions are tagged with `HIGHER_RESOLUTION` in the comment. |
| `higher_resolution.sh` | Automatically extracted subset of `resolution_mismatch.sh` containing only entries where the matched file is higher resolution than Lightroom's record (tagged `HIGHER_RESOLUTION`). These are particularly safe to apply — the matched file likely has more detail than the Smart Preview Lightroom knows about. This file is generated automatically; you can also regenerate it manually with `grep -A 1 'HIGHER_RESOLUTION' resolution_mismatch.sh > higher_resolution.sh`. |
| `import_other_formats.csv` | Ranked cross-format candidates for future import/relink handling (only when no same-extension candidate was found) |
| `import_same_format_higher_resolution.csv` | Same-extension candidates whose resolution *exceeds* the matched file that was linked in `relink_good_matches.sh`. This happens when Lightroom only knows the Smart Preview resolution and the relinked file was matched by resolution — but there is another copy of the same format at a higher (likely original) resolution. Columns: `missing_file`, `matched_file`, `new_file`, `lr_width`, `lr_height`, `matched_width`, `matched_height`, `new_width`, `new_height`, `rank`. |
| `Still_Missing_Photos.csv` | Records with no match found |

The summary counts each input photo exactly once in its primary outcome category
(`Relink commands (best)`, `Resolution mismatches (match)`, `Import other formats`, or
`Still missing`).  Alternate candidates and comment lines are counted separately so
the total primary outcomes always equals the number of photos processed.

### Step 2d — Choose how to process recommendation CSV rows

`import_other_formats.csv` and `import_same_format_higher_resolution.csv` are
recommendation lists, not single all-or-nothing batches. You can inspect/edit them
in Excel (or any CSV tool) and split rows into separate CSVs based on source path,
source volume, file type, or your preferred import method.

Example split:

- `add_directly_in_place.csv`
- `collect_these_to_import_with_lr_dialog.csv`

You can use both workflows in the same project:

- **Workflow 1: Gather + Lightroom Import dialog**  
  Build an import directory of hardlinks (or optional cross-volume copies), then use
  Lightroom's normal Import dialog and your usual import presets/settings.
- **Workflow 2: Import directly in place**  
  Lightroom catalogs each `new_file` exactly where it already lives on disk, with no
  file movement/copy/rename.

### Workflow 1 — Gather recommended files into one directory

Use this helper to gather each `new_file` into an import directory before using
Lightroom's normal Import dialog:

```bash
python3 gather_import_files.py import_other_formats.csv \
    --copy-across-volumes false \
    --tempdir /path/to/importdir
```

- Default source column is `new_file`; override with `--column-name`.
- Same filesystem/volume: creates a hard link in `--tempdir`.
- Different volume: copies only when `--copy-across-volumes true`; otherwise skips.
- Original source files are never modified, moved, renamed, or deleted.
- Filename collisions are handled deterministically (`name__1.ext`, `name__2.ext`, ...);
  existing files are never silently overwritten.

When cross-volume copies are disabled and rows are skipped, the script writes a CSV
containing those remaining rows (all original columns preserved), by default:

`<input_stem>_remaining_other_volume.csv`

### Workflow 2 — Import files directly in place via Lightroom plug-in

In Lightroom Classic, run:

**Library → Plug-in Extras → Import photos from recovery CSV**

What it does:

- Prompts you to choose a CSV.
- Reads the `new_file` column.
- Normalizes and validates each path.
- Skips blank paths.
- Skips missing/unreadable paths.
- Skips files already present in the catalog.
- Imports remaining files in place using `catalog:addPhoto()` (no move/copy/rename).
- Does not use `triggerImportUI()` (Lightroom SDK cannot preselect arbitrary files across many folders).
- Adds successfully imported photos to collection **`new-files-imported`** (reuses it if it already exists).
- Selects imported photos when practical.
- Writes detailed failures (path + error) to `~/Desktop/import_recovery_csv_failures.log`.
- Reports counts for imported / already present / missing-unreadable / failed.

#### Interrupt and resume

If the script is interrupted with Ctrl-C it flushes all output files and prints a
ready-to-paste resume command using `--skip-rows`, `--rows-to-process`, and
`--append-outputs`, so you can pick up where it left off without reprocessing rows
already written to the output files.

> **Note — pure Lua alternative (work in progress):** There is an experimental
> `FindLinkMatches.lrplugin` function called **Find Matches to Missing Photos**
> (`main.lua`) that attempts to perform a similar search entirely inside Lightroom,
> without needing a CSV or `exiftool`.  It queries the active catalog directly and
> writes `ln` shell commands to the Desktop.  This approach is simpler and could be
> useful for other users, but **it does not work reliably yet**.  The Python script
> (`relink_missing_photos.py`) is the currently recommended path.

### Step 2e — Compare metadata for a specific file

```bash
python3 compare_metadata.py data/Missing_Photos.csv <expected_filename> <candidate_path>
```

Loads metadata from the CSV for `<expected_filename>` and compares it against
`<candidate_path>` using exiftool.  Useful for manually verifying a specific
candidate before linking.

### Step 2f — Apply the hardlinks

Review `relink_good_matches.sh`, then run it:

```bash
bash relink_good_matches.sh
```

This creates hardlinks at Lightroom's expected pathnames without consuming
additional disk space and without modifying the Lightroom catalog.

---

## Prerequisites summary

| Tool | Used by | Install |
|------|---------|---------|
| Python 3.9+ | All Python scripts | `brew install python` or system Python |
| `pandas` | `relink_missing_photos.py` | `pip install pandas` |
| `python-dateutil` | `relink_missing_photos.py` | `pip install python-dateutil` |
| `exiftool` | `relink_missing_photos.py`, `compare_metadata.py` | `brew install exiftool` |
| Lightroom plugin in this repo | Missing-photo CSV export and catalog candidate matching | Included (`FindLinkMatches.lrplugin`) |

`recover_from_timemachine.py` uses only the Python standard library.

---

## Repository layout

```
FindLinkMatches.lrplugin/   Lightroom plugin — catalog-based tools for missing/redundant photos
  Info.lua                  Plugin metadata
  main.lua                  WIP: find catalog matches + link command suggestions (not yet working)
  check_same_or_better.lua  Step 0: add photos that have a same-or-better copy to a collection
  write_csv.lua             Step 1/2 input export: Writes all selected photos to Missing_Photos.csv with metadata
  write_missing_csv.lua     Same as write_csv.lua but attempts to skip non-missing photos, not tested yet.
  import_recovery_csv.lua   Workflow 2: import `new_file` rows directly in place into catalog + collection
  LIGHTROOM_LUA_DESIGN_NOTES.md  Notes on Lightroom Lua SDK design choices, pcall usage, and missing-photo reliability findings

recover_from_timemachine.py Phase 1: inspect/scan/restore from Time Machine
relink_missing_photos.py    Phase 2: index filesystem + match by EXIF metadata
compare_metadata.py         Manual metadata comparison helper
gather_import_files.py      Workflow 1: gather `new_file` rows into an import directory via hardlink/copy

data/
  Missing_Photos.csv        Input: exported from Lightroom via plugin CSV command
  timemachine_recovery_candidates.csv   Output of recover_from_timemachine scan
  restore_log.csv           Log of restore operations

setup.env                   pip install commands for Python dependencies
DESIGN.md                   Full design rationale and planned future phases
MISSING_LUA_VS_PYTHON.md    Describes the design advantages and disadvantages of the not-yet-functional lua version of link_missing_photos.py (main.lua)
README.md                   This file
requirements.txt            Python dependencies
```
