# Lightroom Missing Photo Recovery

Tools for recovering Lightroom Classic photos whose originals are reported as missing,
while preserving catalog records, edits, collections, and cloud relationships.

The guiding principle is: **restore or hardlink a valid file to the exact pathname
Lightroom already expects** — rather than importing a replacement as a new photo.

See [DESIGN.md](DESIGN.md) for the full rationale, background, and planned future phases.

---

## Recovery workflow

```
1. Export missing-photo paths from Lightroom (see below)
2. Recover originals from Time Machine           ← recover_from_timemachine.py
3. Re-run Lightroom "Find All Missing Photos"
4. Find identical copies elsewhere and hardlink  ← FindLinkMatches plugin + relink_missing_photos.py
5. Handle remaining gaps manually
```

---

## Step 1 — Export missing-photo paths from Lightroom

Use Lightroom Classic's built-in **"Find All Missing Photos"** (Library menu) to
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
**Status:** Implemented; not yet tested against the live backup volume.  
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

Step 2a and 2b are **complementary tools**, not the same implementation in two
languages:

- **2a (Lua plugin)** queries Lightroom's active catalog directly.
- **2b (Python script)** scans files under `/Volumes/Ladyhawke` and evaluates
  EXIF on-disk via `exiftool`.

Use 2a when you want fast catalog-internal candidate discovery from Lightroom.
Use 2b when you want a broader filesystem pass and more explicit ranking/output
files for review.

### Step 2a — Find candidates with the Lightroom plugin

**Plugin:** `FindLinkMatches.lrplugin`  
**Status:** Implemented; not yet fully end-to-end tested.

Install the plugin via Lightroom's Plugin Manager.  With the
`Missing Photographs` collection selected, run:

**Library → Plug-in Extras → Find Matches to Missing Photos**

The plugin searches the active catalog for non-missing photos whose filename stem,
capture timestamp (within 5 minutes), camera model, and dimensions all match the
missing photo.  It writes three files to the Desktop:

| File | Contents |
|------|----------|
| `link_missing.sh` | `ln` commands for unambiguous single matches |
| `ambiguous_match.csv` | Missing paths with multiple candidate matches |
| `possible_matches.txt` | Weaker candidates where metadata partially matches |

Review these files before running anything.

### Step 2b — Refine candidates with Python (optional)

**Script:** `relink_missing_photos.py`  
**Status:** Implemented; not yet fully end-to-end tested.  
**Dependencies:** Python 3.9+, `pandas`, `python-dateutil`, `exiftool` (CLI tool).

```bash
# Install Python dependencies:
source setup.env   # or: pip install pandas python-dateutil

python3 relink_missing_photos.py data/Missing_Photos.csv
```

Indexes all files under `/Volumes/Ladyhawke` by filename stem, then matches each
missing photo against candidates using `exiftool`-extracted EXIF data (timestamp,
camera make, width, height).  Handles resolution mismatches separately.

Unlike the plugin, this step is not limited to what Lightroom currently returns
from catalog search and emits separate outputs for exact relinks, resolution
mismatches, and still-missing records.

Options:

```
--test-n N               Process a random sample of N rows (for testing)
--exclude-sources PATH   Exclude directory subtrees from candidate indexing
--exclude-targets PATH   Skip missing-photo entries whose path contains this string
```

Outputs (written to the current working directory):

| File | Contents |
|------|----------|
| `relink_good_matches.sh` | `ln` commands for confirmed matches |
| `resolution_mismatch.sh` | Best-guess links where resolution differs |
| `Still_Missing_Photos.csv` | Records with no match found |

### Comparison

Step 2a and 2b are **complementary tools**, very similar but not 
quite the same implementation:

- **2a (Lua plugin)** queries Lightroom's active catalog directly.
- **2b (Python script)** scans files under `/Volumes/Ladyhawke` and evaluates
  EXIF on-disk via `exiftool`.

For a detailed explanation of how these two approaches differ, see
[MISSING_LUA_VS_PYTHON.md](MISSING_LUA_VS_PYTHON.md).

Use 2a when you want fast catalog-internal candidate discovery from Lightroom.
Use 2b when you want a broader filesystem pass and more explicit ranking/output
files for review.

### Step 2c — Compare metadata for a specific file

```bash
python3 compare_metadata.py data/Missing_Photos.csv <expected_filename> <candidate_path>
```

Loads metadata from the CSV for `<expected_filename>` and compares it against
`<candidate_path>` using exiftool.  Useful for manually verifying a specific
candidate before linking.

### Step 2d — Apply the hardlinks

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
FindLinkMatches.lrplugin/   Lightroom plugin — find catalog matches for missing photos
  Info.lua                  Plugin metadata
  main.lua                  Step 2a: find catalog matches + link command suggestions
  write_csv.lua             Step 1/2 input export: Missing_Photos.csv with metadata

recover_from_timemachine.py Phase 1: inspect/scan/restore from Time Machine
relink_missing_photos.py    Phase 2: index filesystem + match by EXIF metadata
compare_metadata.py         Manual metadata comparison helper

data/
  Missing_Photos.csv        Input: exported from Lightroom via plugin CSV command
  timemachine_recovery_candidates.csv   Output of recover_from_timemachine scan
  restore_log.csv           Log of restore operations

Still_Missing_Photos.csv    Photos with no match after Phase 2
ambiguous_matches.csv       Photos with multiple conflicting candidates
relink_good_matches.sh      Generated hardlink commands (review before running)
setup.env                   pip install commands for Python dependencies
DESIGN.md                   Full design rationale and planned future phases
```
