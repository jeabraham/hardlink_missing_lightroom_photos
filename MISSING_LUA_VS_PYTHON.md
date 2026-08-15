# Lua Plugin vs Python Relinking Script

This project includes two tools that appear similar at first glance:

- `FindLinkMatches.lrplugin/main.lua`
- `relink_missing_photos.py`

They overlap in purpose, but they are not identical. Both try to identify existing files that may correspond to Lightroom photos marked as missing, then produce reviewable hardlink commands. The major differences are where they search, what metadata they trust, and how conservative they are when multiple candidates exist.

## High-level comparison

| Aspect | `main.lua` Lightroom plugin | `relink_missing_photos.py` |
|---|---|---|
| Runs inside Lightroom? | Yes | No |
| Input missing-photo list | Currently selected Lightroom photos | CSV file |
| Candidate source | Lightroom catalog only | Filesystem under `--search-root` |
| Candidate discovery | Catalog filename search using `contains` | `os.walk()` index by exact filename stem |
| Metadata source for missing photo | Lightroom catalog metadata | CSV metadata exported from Lightroom |
| Metadata source for candidate | Lightroom catalog metadata | On-disk EXIF via `exiftool` |
| Timestamp tolerance | 5 minutes | 5 minutes |
| Camera comparison | Exact camera model string match | Fuzzy camera make containment match |
| Dimension comparison | Exact width and height for strong match | Exact width and height for good match; resolution mismatch handled separately |
| Multiple exact matches | Written as ambiguous | Automatically ranks and selects best |
| Weaker matches | Written to `possible_matches.txt` | Written to `resolution_mismatch.sh` if timestamp/camera match but dimensions differ |
| Still-missing output | No explicit output | Writes `Still_Missing_Photos.csv` |
| Output location | Desktop | Current working directory |
| Can find files not in Lightroom catalog? | No | Yes |
| Can find catalog entries outside `--search-root`? | Yes, if non-missing in catalog | No |

## What they have in common

Both tools implement the same general recovery idea:

1. Start with photos Lightroom says are missing.
2. Use the missing photo's filename stem as the first filter.
3. Compare metadata:
   - capture time
   - camera information
   - width
   - height
4. Generate `ln` shell commands that would create a hardlink at Lightroom's expected missing path.

Conceptually, both are looking for:

```text
Existing file somewhere else
        ↓
same or similar filename stem
        ↓
capture time within 5 minutes
        ↓
matching camera/dimensions
        ↓
candidate for hardlinking to Lightroom's expected path
```

Both tools intentionally do not directly relink Lightroom and do not directly modify the catalog. They emit files for review.

## Candidate discovery is the biggest difference

### `main.lua`

The Lua plugin searches inside the active Lightroom catalog.

It can only find candidate photos that Lightroom already knows about. If a valid replacement file exists on disk but was never imported into Lightroom, the Lua plugin will not find it.

It searches by filename `contains`, so a missing photo named:

```text
IMG_1234.CR2
```

may match catalog filenames such as:

```text
IMG_1234.CR2
IMG_1234.JPG
backup_IMG_1234.tif
IMG_12345.CR2
```

The later metadata checks reduce false positives, but the initial search is broad.

### `relink_missing_photos.py`

The Python script scans the filesystem under a given `--search-root`.

It indexes files by exact lowercase filename stem, then looks up candidates by the missing photo's stem.

So for a missing file named:

```text
IMG_1234.CR2
```

it looks for files whose stem is exactly:

```text
img_1234
```

This is narrower than Lua's `contains` search, but broader in another way because it can find files that are not in the Lightroom catalog.

### Practical difference

`main.lua` answers:

> Do I already have another non-missing Lightroom catalog entry that looks like this missing photo?

`relink_missing_photos.py` answers:

> Does my filesystem contain a file under this search root that looks like this missing photo?

Those are related but meaningfully different questions.

## Metadata source differs

### `main.lua`

For both the missing photo and candidate photos, Lua uses Lightroom catalog metadata.

This is fast and catalog-native, but it depends on Lightroom's stored interpretation of the files.

### `relink_missing_photos.py`

For the missing target, Python uses the exported CSV row.

For candidate files, Python shells out to `exiftool` and reads metadata from the file on disk.

That makes Python potentially more authoritative about candidate files, especially if those files are not in Lightroom, but it is slower and depends on `exiftool`.

## Camera matching is different

### `main.lua`

Lua requires exact camera model equality.

That is strict. If Lightroom reports slightly different strings for otherwise matching files, the candidate may be downgraded or missed.

### `relink_missing_photos.py`

Python compares camera make more loosely. It lowercases and strips strings, then accepts a match if either camera string contains the other, or if one side is missing.

This is more forgiving, but less strict.

One important nuance: the Lua plugin uses camera model, while the Python script uses camera make. Those are not equivalent.

For example:

```text
Camera model: Canon EOS 5D Mark III
Camera make:  Canon
```

So Python's camera check is probably weaker than Lua's.

## Timestamp handling is similar

Both tools use a five-minute timestamp tolerance.

Lua relies on Lightroom date parsing.

Python parses Lightroom-exported dates and EXIF-style dates, and it also normalizes timezone-aware versus timezone-naive values before comparing.

The intent is the same, but Python performs more explicit normalization.

## Dimension handling differs

### `main.lua`

Lua divides timestamp-matching candidates into:

- strong matches: camera, width, and height all match
- possible matches: timestamp matches, but camera or dimensions differ

If no exact match exists, possible matches are written as informational text only.

### `relink_missing_photos.py`

Python first filters candidates by timestamp and camera. Then it separates:

- exact dimension matches into `relink_good_matches.sh`
- dimension mismatches into `resolution_mismatch.sh`

That is more actionable than Lua's `possible_matches.txt`, but it is also potentially riskier if run without review.

## Ambiguity handling is very different

This is one of the most important behavioral differences.

### `main.lua` is conservative

If exactly one strong match exists, Lua writes a hardlink command.

If multiple strong matches exist, Lua writes the case to `ambiguous_match.csv`.

It does not pick a winner.

That is safer and more conservative.

### `relink_missing_photos.py` ranks and chooses

If multiple exact matches exist, Python sorts them and chooses a best candidate.

Its ranking prefers:

1. RAW files
2. stronger camera matches
3. higher resolution

It writes comments for alternate candidates, but still emits a link command for the selected best match.

This can be convenient, but it is less conservative than the Lua plugin. If two candidates are genuinely equivalent duplicates, this is fine. If they are different files with similar metadata, auto-selection could be wrong.

## Output comparison

### `main.lua` outputs

Written to the Desktop:

| File | Meaning |
|---|---|
| `link_missing.sh` | One command per missing photo with exactly one strong match |
| `ambiguous_match.csv` | Missing photos with multiple strong matches |
| `possible_matches.txt` | Timestamp matches that failed the full metadata match |

### `relink_missing_photos.py` outputs

Written to the current working directory:

| File | Meaning |
|---|---|
| `relink_good_matches.sh` | Exact dimension matches, including auto-selected best matches |
| `resolution_mismatch.sh` | Timestamp/camera matches where dimensions differ |
| `Still_Missing_Photos.csv` | CSV rows with no usable candidate or insufficient metadata |

Python's output is more workflow-oriented because it explicitly tracks still-missing rows.

Lua's output is more review-oriented because it separates ambiguous exact matches rather than resolving them.

## Shell command quoting

Both tools produce shell commands that should be reviewed before running.

The Lua plugin uses single quotes:

```bash
ln '/candidate/path' '/target/path'
```

The Python script uses double quotes:

```bash
ln "/candidate/path" "/target/path"
```

Neither form is fully robust for arbitrary filenames. Single quotes break on paths containing single quotes. Double quotes can still be affected by shell expansion characters such as `$`, backticks, and embedded double quotes.

A future improvement would be to use robust shell quoting, such as Python's `shlex.quote()` in the Python script and equivalent escaping in the Lua plugin.

## Filtering and operational controls

Python has command-line controls that Lua does not:

```text
--search-root
--test-n
--exclude-sources
--exclude-targets
```

These are useful when testing, limiting scope, or avoiding known-bad source directories.

Lua's scope is controlled by:

- which photos are selected in Lightroom
- which non-missing catalog photos Lightroom finds via catalog search

## Performance characteristics

### `main.lua`

The Lua plugin avoids external processes and uses Lightroom's catalog metadata. That should be fast per candidate.

However, it performs a catalog search for each selected missing photo, so performance depends on Lightroom catalog size and search behavior.

### `relink_missing_photos.py`

The Python script walks the search tree once and builds an index by filename stem. Candidate lookup is then cheap.

However, it invokes `exiftool` for candidate files, which can be slow if there are many candidates.

Python may scale well when filename stems are selective, but poorly when many files share the same stem or when the search root is very large.

## Safety comparison

### Safer aspects of `main.lua`

- Does not scan arbitrary filesystem locations.
- Does not auto-resolve multiple strong matches.
- Only considers non-missing Lightroom catalog entries.
- Writes possible and ambiguous cases for manual review.

### Safer aspects of `relink_missing_photos.py`

- Has explicit `Still_Missing_Photos.csv`.
- Uses on-disk EXIF from actual files.
- Can exclude source and target paths.
- Can test on a random subset with `--test-n`.

### Riskier aspects of `main.lua`

- Trusts Lightroom catalog metadata for candidates.
- Uses broad filename `contains` search.
- Does not emit an explicit still-missing report.
- Shell quoting is fragile.

### Riskier aspects of `relink_missing_photos.py`

- Automatically chooses among multiple exact matches.
- Writes executable commands for resolution mismatches.
- Camera match is fuzzy and uses make rather than model.
- Shell quoting is fragile.
- Searches any files under `--search-root`, whether or not Lightroom knows about them.

## Are they redundant?

They are partially redundant, but not fully.

They share the same high-level matching rule:

```text
filename stem + timestamp within 5 minutes + camera/dimensions comparison
```

But they are complementary because:

- `main.lua` searches Lightroom catalog entries.
- `relink_missing_photos.py` searches filesystem files.
- `main.lua` uses catalog metadata.
- `relink_missing_photos.py` uses EXIF from disk.
- `main.lua` is conservative with ambiguity.
- `relink_missing_photos.py` ranks and emits best guesses.

If your filesystem search root contains all files Lightroom knows about, plus more, the Python script can cover much of the Lua plugin's practical purpose. But it still does not reproduce the exact same behavior because its candidate set and metadata interpretation differ.

## Recommended interpretation

Use `main.lua` when you want fast Lightroom-native candidate discovery for selected missing catalog photos.

Use `relink_missing_photos.py` when you want a broader filesystem-based recovery pass using exported Lightroom metadata and on-disk EXIF.

In one sentence:

> `main.lua` finds non-missing Lightroom catalog photos that resemble selected missing photos, while `relink_missing_photos.py` finds on-disk files under a search root that resemble missing-photo rows from a CSV, and it is more aggressive about ranking and generating relink commands.
```
