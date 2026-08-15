# Design: Lightroom Missing Photo Recovery

This document describes the rationale, background, and planned future phases for
the `hardlink_missing_lightroom_photos` recovery workflow.

For the practical usage guide and current implementation status, see
[README.md](README.md).

---

## Background

There are several different reasons Lightroom currently reports files as missing.

Some originals appear to have genuinely disappeared from the current photo archive
but still exist in older Time Machine backups.

Other photos are probably not actually lost. Lightroom moved or reorganized files
at some point, and Lightroom Cloud subsequently downloaded another copy into a newer
location. Lightroom may therefore have one catalog record pointing to the old,
now-missing pathname while an identical file exists elsewhere on the same filesystem.

There are also historical duplicates caused by changes in photo-storage practices.
In earlier years, camera RAW files were stored separately from processed/exported
files. Later, the `RawPhotos` tree became the main repository for both RAW and JPEG
originals. Consequently, there can be valid copies or derivatives scattered through
older directory trees.

Finally, there are some very large files, particularly scans, for which identical
copies may legitimately exist in multiple locations and consume substantial disk space.

## Source of Truth: Lightroom's Missing Photographs

Lightroom Classic itself should determine which catalog records are missing.

Use:

**Library → Find All Missing Photos**

This creates Lightroom's `Missing Photographs` collection.

The recovery tools should work from an exported list of the expected full paths of
those photos. The full expected path is essential because filenames such as
`IMG_2302.JPG` or `DSC_0201.NEF` are frequently reused by cameras and are not
globally unique.

Example:

```text
/Volumes/Ladyhawke/RawPhotos/2013/2013-09-08/_JD07021.NEF
```

Files on volumes that are simply offline should not be treated as genuinely lost.
The software should classify missing records by expected volume so that
`/Volumes/Ladyhawke/...` can be processed independently of photos belonging on
other disconnected drives.

## Recovery Priority

Recovery should proceed from the highest-quality and most authoritative source to
progressively weaker substitutes.

### Phase 1: Recover originals from Time Machine

This is the highest priority.

A missing Lightroom original has already been found in a Time Machine snapshot
from 2022-11-10:

```text
/Volumes/Ladyhawke/RawPhotos/2013/2013-09-08/_JD07021.NEF
```

This demonstrates that older Time Machine backups may contain many of the originals
that are currently absent from Ladyhawke.

Suggested classifications:

```text
FOUND_IN_TIME_MACHINE
NOT_FOUND_IN_TIME_MACHINE
CURRENTLY_PRESENT
NON_LADYHAWKE_PATH
MULTIPLE_TIME_MACHINE_VERSIONS
```

For a Time Machine recovery, the preferred operation is a normal copy from the
backup into the expected Lightroom pathname.  Once the file exists at the path
already recorded in the Lightroom catalog, Lightroom should recognize the original
again without reimporting or relinking the catalog record.

### Phase 2: Find identical copies elsewhere and hardlink them

After recovering everything possible from Time Machine, rerun Lightroom's
`Find All Missing Photos`.

The remaining missing records become candidates for the existing purpose of this
repository.

For each missing expected pathname, search the current storage for another copy of
the same original.

Likely search locations include:

```text
/Volumes/Ladyhawke/RawPhotos
/Volumes/Ladyhawke/Photos
/Volumes/Ladyhawke/RawPhotoImports
/Volumes/Ladyhawke/JpgFromTiff
```

and any other known historical photo-storage trees.

Filename alone must never be considered sufficient evidence of identity.

Matching should use progressively stronger evidence, for example:

1. Exact pathname-history relationship where known.
2. Exact file size.
3. Cryptographic hash when the candidate and expected original can both be compared
   to a known source.
4. EXIF capture timestamp.
5. Camera make/model.
6. Image dimensions.
7. Other EXIF identifiers where available.
8. Existing project metadata-comparison logic.
9. Manual review for ambiguous cases.

Where a confirmed identical file exists elsewhere on the **same filesystem**, create
a hardlink at the pathname Lightroom expects.

Example conceptually:

```text
existing good copy:
    /Volumes/Ladyhawke/.../new-location/photo.NEF

Lightroom expects:
    /Volumes/Ladyhawke/.../old-location/photo.NEF
```

If they are confirmed to be the same original, create the missing pathname as
another hardlink to the existing inode.

This has several advantages:

* Lightroom sees the file at its historical pathname.
* No duplicate disk space is consumed.
* The existing newer path continues to work.
* Lightroom catalog manipulation is unnecessary.
* Lightroom Cloud relationships remain attached to their existing catalog records.

The software must verify that source and destination are on the same filesystem
before attempting a hardlink.

Ambiguous matches should never be linked automatically.

Future TODO for cross-format candidates:

* Add a Lightroom Lua workflow that consumes `import_other_formats.csv`.
* At minimum, support importing rank-0 `new_file` candidates and copying develop
  settings from `missing_file` where Lightroom APIs permit it.
* If format-specific automation is unreliable, fall back to putting all
  cross-format candidates into a Lightroom collection for manual review/sorting.

## Why This Situation Exists

A significant source of the missing records appears to be interaction between
Lightroom Classic file movement and Lightroom Cloud synchronization.

In some cases Lightroom has:

1. had a catalog record for a file in an older pathname;
2. moved or otherwise reorganized the original;
3. later downloaded a cloud copy into the newer configured Lightroom sync location;
4. retained another catalog record that still points to the old pathname.

The result is that Lightroom reports the historical record as missing even though an
identical file may already exist elsewhere on Ladyhawke.

This is exactly the class of problem where hardlinking is preferable to importing
another copy or trying to modify Lightroom's internal catalog references.

## Phase 3: Historical Photos Tree Audit

Separately from direct missing-photo recovery, the old:

```text
/Volumes/Ladyhawke/Photos
```

tree should eventually be audited.

Historically, this directory was the principal viewable-photo archive. RAW originals
lived separately because normal image-viewing software of the period could not
conveniently display RAW files. Processed JPEGs were exported into `Photos`, while
JPEG camera originals could also live there directly.

Starting roughly around 2009, the workflow changed and `RawPhotos` increasingly
became the repository for all originals, including JPEGs.

Therefore, even post-2008 files under `Photos` cannot simply be assumed to be
exports or duplicates.

The future audit should identify files in `Photos` that have no equivalent original
elsewhere.

Possible classifications:

```text
EXACT_DUPLICATE
LIKELY_RAW_EXPORT
LIKELY_SAME_IMAGE_REENCODED
UNIQUE_PHOTOS_TREE_FILE
AMBIGUOUS
```

`UNIQUE_PHOTOS_TREE_FILE` records are potentially important surviving originals and
should be preserved or incorporated into the modern archive.

This audit should be database/report driven and should not delete or move files
automatically.

## Phase 4: Lightroom Cloud as a Fallback

Some missing Lightroom photos are in `All Synced Photographs`.

This can provide a last-resort recovery source, but the cloud copy may only be a
Smart Preview rather than the original.

For example, Lightroom may display metadata describing an original NEF at full
native dimensions, while Lightroom Web only permits downloading a smaller derivative
such as approximately 2048 pixels on the long edge.

Therefore cloud recovery should rank below recovery of an actual original from Time
Machine or another filesystem location.

Fallback order should generally be:

```text
Time Machine original
    ↓
Identical original elsewhere
    ↓
Other archive containing original
    ↓
Full-resolution historical JPEG/export
    ↓
Lightroom Cloud Smart Preview
    ↓
No surviving image
```

Cloud-derived recovery files should be preserved separately unless there is a
deliberate decision about how to represent them in Lightroom.

## Phase 5: Optional Duplicate-Space Cleanup with rdfind

After missing-photo recovery is complete and verified, disk-space cleanup can be
considered as a separate operation.

Some very large scans or other files may exist in multiple directories as
byte-for-byte duplicates.

`rdfind` can identify identical files and replace duplicates with hardlinks.

This should be treated as an optional final phase, not part of missing-photo recovery.

The workflow should be:

1. Run `rdfind` in report/dry-run mode.
2. Review duplicate groups.
3. Pay particular attention to very large scans where hardlinking offers meaningful
   space savings.
4. Ensure all candidate files are on the same filesystem.
5. Only then allow `rdfind` to replace verified duplicates with hardlinks.

Do not use perceptual similarity for this operation. Only byte-identical files should
be automatically hardlinked for deduplication.

## Safety Rules

The recovery software should be conservative.

* Default to reporting rather than modifying.
* Never overwrite an existing destination file without explicit verification.
* Never treat filename equality as proof of identity.
* Never automatically resolve ambiguous matches.
* Never import recovered files into Lightroom as new photos if the existing Lightroom
  catalog record can simply be repaired by restoring the expected pathname.
* Preserve Lightroom's existing catalog identity whenever possible.
* Record every filesystem modification in a log.
* Support dry-run mode for every operation that writes files.
* Where possible, hash files before destructive or identity-sensitive operations.
* Never modify Time Machine backups.
* Keep Time Machine recovery, hardlink recovery, and duplicate cleanup as distinct
  operations.

## Suggested Development Structure

The repository is structured around several explicit stages:

```text
1. export_missing
   Obtain Lightroom's expected missing paths (via Any Filter plugin export).

2. scan_timemachine   [recover_from_timemachine.py scan]
   Search mounted Time Machine backups for missing originals.

3. restore_timemachine   [recover_from_timemachine.py restore]
   Restore explicitly approved originals to their expected paths.

4. find_existing_matches   [FindLinkMatches.lrplugin + relink_missing_photos.py]
   Search current storage for identical copies of remaining missing files.

5. hardlink_matches   [relink_good_matches.sh]
   Create approved hardlinks at Lightroom's expected paths.

6. verify
   Confirm expected files now exist and rerun Lightroom's missing-photo check.

7. audit_photos_tree   [future]
   Identify unique legacy files in /Volumes/Ladyhawke/Photos.

8. deduplicate   [future, rdfind]
   Optionally replace byte-identical large files with hardlinks.
```
