#!/usr/bin/env python3
"""
recover_from_timemachine.py — Phase 1 Time Machine recovery for Lightroom missing photos.

Commands:
  inspect   <tm_volume>            — Discover and report Time Machine layout.
  scan      <csv> <tm_volume>      — Find missing Ladyhawke originals in Time Machine.
  restore   --report <csv>         — Copy found originals back (dry-run by default).

Examples:
  python3 recover_from_timemachine.py inspect /Volumes/iMacBackup3
  python3 recover_from_timemachine.py scan data/Missing_Photos.csv /Volumes/iMacBackup3
  python3 recover_from_timemachine.py restore --report data/timemachine_recovery_candidates.csv --dry-run
  python3 recover_from_timemachine.py restore --report data/timemachine_recovery_candidates.csv --execute
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from itertools import groupby
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LADYHAWKE_VOLUME = "/Volumes/Ladyhawke"
DEFAULT_REPORT = "data/timemachine_recovery_candidates.csv"

REPORT_COLUMNS = [
    "status",
    "expected_path",
    "relative_path",
    "backup_path",
    "backup_date",
    "file_size",
    "mtime",
    "notes",
]

STATUS_FOUND = "FOUND_IN_TIME_MACHINE"
STATUS_MULTIPLE = "MULTIPLE_TIME_MACHINE_VERSIONS"
STATUS_NOT_FOUND = "NOT_FOUND_IN_TIME_MACHINE"
STATUS_PRESENT = "CURRENTLY_PRESENT"
STATUS_OTHER = "NON_LADYHAWKE_PATH"


# ---------------------------------------------------------------------------
# Time Machine discovery
# ---------------------------------------------------------------------------

# Regex to extract the datetime portion from an APFS TM snapshot name.
# e.g. "com.apple.TimeMachine.2022-11-10-152833.backup" → "2022-11-10-152833"
_APFS_SNAP_DATE_RE = re.compile(
    r"com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.backup"
)


def _list_apfs_snapshots(tm_volume: Path) -> list[dict]:
    """
    Run ``diskutil apfs listsnapshots <volume>`` and parse the output.

    Returns a list of dicts with keys:
      uuid  — snapshot UUID
      name  — full snapshot name (e.g. "com.apple.TimeMachine.2022-11-10-152833.backup")
      date  — extracted date string (e.g. "2022-11-10-152833"), or the full name if
              the pattern doesn't match
    Ordered as reported by diskutil (oldest-first in practice).
    """
    try:
        proc = subprocess.run(
            ["diskutil", "apfs", "listsnapshots", str(tm_volume)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []  # diskutil not available (non-macOS)

    snapshots = []
    current_uuid = None
    current_name = None

    for line in proc.stdout.splitlines():
        line = line.strip()
        # UUID line: "+-- 3838ACCC-AC78-4A75-8126-27BC2768186F"
        uuid_m = re.match(r"\+--\s+([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})", line)
        if uuid_m:
            current_uuid = uuid_m.group(1)
            current_name = None
            continue
        # Name line: "|   Name:        com.apple.TimeMachine.2022-11-10-152833.backup"
        name_m = re.search(r"Name:\s+(.+)", line)
        if name_m and current_uuid:
            current_name = name_m.group(1).strip()
            date_m = _APFS_SNAP_DATE_RE.search(current_name)
            date = date_m.group(1) if date_m else current_name
            snapshots.append({"uuid": current_uuid, "name": current_name, "date": date})
            current_uuid = None
            current_name = None

    return snapshots


@contextmanager
def _mount_apfs_snapshot(snapshot_name: str, tm_volume: Path):
    """
    Context manager that mounts an APFS snapshot read-only under a temporary
    directory, yields the mount path, then unmounts when done.

    Uses ``mount_apfs -s <snapshot_name> <volume> <mountpoint>``.
    Requires root (or the caller to have appropriate privileges).
    """
    with tempfile.TemporaryDirectory(prefix="tm_snap_") as mountpoint:
        try:
            subprocess.run(
                ["mount_apfs", "-s", snapshot_name, str(tm_volume), mountpoint],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise OSError(
                f"mount_apfs failed for snapshot {snapshot_name!r}: "
                f"{exc.stderr.decode(errors='replace').strip()}"
            ) from exc
        try:
            yield Path(mountpoint)
        finally:
            subprocess.run(
                ["umount", mountpoint],
                capture_output=True,
            )


def discover_tm_layout(tm_volume: Path) -> dict:
    """
    Inspect a mounted Time Machine volume and return a dict describing its
    layout.  Handles three cases:

      * ``apfs``   — APFS volume with Time Machine snapshots (macOS 10.14+).
                     Snapshots are enumerated via ``diskutil apfs listsnapshots``.
                     They must be individually mounted to browse their content.
      * ``modern`` — HFS+ Backups.backupdb/<host>/<snapshot-dir>/ layout.
      * ``unknown``— Neither structure found.

    Returns a dict with:
      layout_type:      "apfs" | "modern" | "unknown"
      apfs_snapshots:   [{"uuid": …, "name": …, "date": …}]  (apfs only)
      hosts:            [str, …]            (modern only)
      snapshots:        {host: [Path]}      (modern only)
      ladyhawke_roots:  [Path]              (modern only; one per host)
      raw_notes:        [str, …]
    """
    result = {
        "layout_type": "unknown",
        "apfs_snapshots": [],
        "hosts": [],
        "snapshots": {},
        "ladyhawke_roots": [],
        "raw_notes": [],
    }

    # --- Try APFS snapshot layout first ---
    apfs_snaps = _list_apfs_snapshots(tm_volume)
    tm_snaps = [s for s in apfs_snaps if "TimeMachine" in s["name"]]
    if tm_snaps:
        result["layout_type"] = "apfs"
        result["apfs_snapshots"] = tm_snaps
        result["raw_notes"].append(
            f"APFS volume: {len(tm_snaps)} Time Machine snapshots found "
            f"(out of {len(apfs_snaps)} total APFS snapshots)."
        )
        return result

    # --- Fall back to classic Backups.backupdb layout ---
    backupdb = tm_volume / "Backups.backupdb"
    if backupdb.is_dir():
        result["layout_type"] = "modern"
        result["raw_notes"].append(f"Found Backups.backupdb at {backupdb}")

        try:
            host_dirs = sorted(
                p for p in backupdb.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        except PermissionError as exc:
            result["raw_notes"].append(f"Permission error reading {backupdb}: {exc}")
            host_dirs = []

        for host_dir in host_dirs:
            host = host_dir.name
            result["hosts"].append(host)
            try:
                snap_dirs = sorted(
                    p
                    for p in host_dir.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                )
            except PermissionError as exc:
                result["raw_notes"].append(f"Permission error reading {host_dir}: {exc}")
                snap_dirs = []
            result["snapshots"][host] = snap_dirs

            # Look for Ladyhawke-related volume dirs inside each snapshot.
            for snap_dir in snap_dirs:
                candidate = snap_dir / "Ladyhawke"
                if candidate.is_dir():
                    result["ladyhawke_roots"].append(candidate)
                    break  # one representative is enough for listing
    else:
        result["raw_notes"].append(
            f"No Backups.backupdb found at {backupdb}. "
            "Volume may use an unsupported layout or may not be a Time Machine volume."
        )

    # Deduplicate Ladyhawke roots while preserving first-seen order
    seen = set()
    deduped = []
    for p in result["ladyhawke_roots"]:
        key = str(p)
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    result["ladyhawke_roots"] = deduped

    return result


def all_ladyhawke_snapshots(layout: dict) -> list:
    """
    Return a list of ``(snapshot_ref, date_str)`` tuples, newest-first, for
    every snapshot that (for modern layout) contains a Ladyhawke volume
    directory, or (for APFS layout) every Time Machine snapshot on the volume.

    For ``modern`` layout the snapshot_ref is a ``Path`` (the Ladyhawke root
    inside the snapshot directory).

    For ``apfs`` layout the snapshot_ref is the snapshot **name** string
    (e.g. ``"com.apple.TimeMachine.2022-11-10-152833.backup"``).  Files cannot
    be browsed directly — callers must mount the snapshot to access content.
    """
    if layout.get("layout_type") == "apfs":
        # APFS snapshots from diskutil are typically oldest-first; reverse for newest-first.
        snaps = layout.get("apfs_snapshots", [])
        results = [(s["name"], s["date"]) for s in snaps]
        results.sort(key=lambda t: t[1], reverse=True)
        return results

    # modern / unknown — original path-based approach
    results = []
    for host, snap_dirs in layout.get("snapshots", {}).items():
        for snap_dir in snap_dirs:
            lh = snap_dir / "Ladyhawke"
            if lh.is_dir():
                results.append((lh, snap_dir.name))
    results.sort(key=lambda t: t[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# CSV parsing (reuses the same format as relink_missing_photos.py)
# ---------------------------------------------------------------------------

def load_missing_photos(csv_path: Path) -> list:
    """
    Load the Lightroom missing-photo CSV.  The only required column is 'Photo'
    which holds the full expected path.  Extra columns are preserved as-is.
    Returns a list of dicts.
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "Photo" not in (reader.fieldnames or []):
            print(
                f"ERROR: CSV {csv_path} has no 'Photo' column. "
                f"Found columns: {reader.fieldnames}",
                file=sys.stderr,
            )
            sys.exit(1)
        for row in reader:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def relative_ladyhawke_path(expected_path: str) -> str | None:
    """
    Given a full expected path such as /Volumes/Ladyhawke/RawPhotos/foo/bar.NEF,
    return the relative portion (RawPhotos/foo/bar.NEF).
    Returns None if the path is not under /Volumes/Ladyhawke.
    """
    try:
        return str(Path(expected_path).relative_to(LADYHAWKE_VOLUME))
    except ValueError:
        return None


def find_in_snapshots(rel_path: str, snapshots: list) -> list:
    """
    Search ordered list of (snapshot_lh_root, date_str) tuples for a relative
    path.  Returns a list of (backup_path, date_str) for every snapshot where
    the file exists, newest-first.
    """
    found = []
    for lh_root, date_str in snapshots:
        if _snapshot_contains_path(lh_root, rel_path):
            found.append((lh_root / rel_path, date_str))
    return found


def _snapshot_contains_path(lh_root: Path, rel_path: str) -> bool:
    candidate = lh_root / rel_path
    return candidate.is_file()


def _snapshot_month_key(date_str: str) -> str | None:
    """
    Extract a YYYY-MM month key from a Time Machine snapshot directory name.
    Returns None if no valid month-like prefix is present.
    """
    m = re.match(r"^(\d{4})-(\d{2})", date_str)
    if not m:
        return None
    month = int(m.group(2))
    if month < 1 or month > 12:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def build_month_anchor_indices(snapshots: list) -> list[int]:
    """
    Build month anchors for snapshots ordered newest-first.

    Returns snapshot indices for the first chronological snapshot in each month
    (typically the oldest snapshot in that month), with months ordered
    newest-to-oldest.
    """
    month_order = []
    month_to_index = {}

    for idx, (_, date_str) in enumerate(snapshots):
        key = _snapshot_month_key(date_str) or f"unknown:{date_str}"
        if key not in month_to_index:
            month_order.append(key)
        # Overwrite so we keep the oldest-in-month index for this month.
        month_to_index[key] = idx

    return [month_to_index[key] for key in month_order]


def find_in_snapshots_anchored(
    rel_path: str,
    snapshots: list,
    month_anchor_indices: list[int],
    month_interval: int = 1,
) -> list:
    """
    Two-phase snapshot search:
      1) Probe month anchors at the configured month interval.
      2) Once found, scan forward in time (toward newer snapshots) to pick the
         newest available match and keep all matches in that forward window.
    """
    if month_interval < 1:
        raise ValueError("month_interval must be >= 1")
    if not snapshots:
        return []

    anchor_probe_indices = month_anchor_indices[::month_interval]
    found_anchor_idx = None

    for idx in anchor_probe_indices:
        lh_root, _ = snapshots[idx]
        if _snapshot_contains_path(lh_root, rel_path):
            found_anchor_idx = idx
            break

    if found_anchor_idx is None:
        return []

    found = []
    for idx in range(0, found_anchor_idx + 1):
        lh_root, date_str = snapshots[idx]
        if _snapshot_contains_path(lh_root, rel_path):
            found.append((lh_root / rel_path, date_str))
    return found


def file_stat(path: Path) -> dict:
    """Return size and mtime for a file, or empty strings on error."""
    try:
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
        return {"file_size": st.st_size, "mtime": mtime}
    except OSError:
        return {"file_size": "", "mtime": ""}


# ---------------------------------------------------------------------------
# APFS snapshot backup-path encoding
# ---------------------------------------------------------------------------

# Backup paths for APFS snapshots use a URI-like scheme so the restore command
# can re-mount the correct snapshot.
# Format:  apfs-snap://<tm_volume>::<snapshot_name>::<rel_path>
# Example: apfs-snap:///Volumes/iMacBackup3::com.apple.TimeMachine.2022-11-10-152833.backup::RawPhotos/2013/foo.NEF
APFS_SNAP_PREFIX = "apfs-snap://"


def _apfs_backup_path(tm_volume: Path, snap_name: str, rel_path: str) -> str:
    return f"{APFS_SNAP_PREFIX}{tm_volume}::{snap_name}::{rel_path}"


def _parse_apfs_backup_path(backup_path: str):
    """
    Parse an APFS backup path URI.
    Returns (tm_volume, snap_name, rel_path) or raises ValueError.
    """
    if not backup_path.startswith(APFS_SNAP_PREFIX):
        raise ValueError(f"Not an APFS backup path: {backup_path!r}")
    rest = backup_path[len(APFS_SNAP_PREFIX):]
    parts = rest.split("::", 2)
    if len(parts) != 3:
        raise ValueError(f"Malformed APFS backup path: {backup_path!r}")
    return Path(parts[0]), parts[1], parts[2]


def is_apfs_backup_path(backup_path: str) -> bool:
    return backup_path.startswith(APFS_SNAP_PREFIX)


# ---------------------------------------------------------------------------
# APFS-specific snapshot scanning (mount each snapshot once, check all files)
# ---------------------------------------------------------------------------

def scan_apfs_snapshots(
    rel_paths: list[str],
    snapshots: list,
    tm_volume: Path,
    *,
    progress_callback=None,
) -> dict:
    """
    For APFS Time Machine volumes, mount each snapshot once, check every
    ``rel_path`` in the mounted Ladyhawke directory, then unmount.

    ``snapshots`` is a list of ``(snap_name, date_str)`` tuples, newest-first
    (as returned by ``all_ladyhawke_snapshots`` for the APFS layout).

    Returns a dict mapping ``rel_path`` → list of
    ``(apfs_backup_path_str, date_str, file_size, mtime)`` tuples, newest-first.
    """
    results = {r: [] for r in rel_paths}

    for i, (snap_name, date_str) in enumerate(snapshots, 1):
        if progress_callback:
            progress_callback(i, len(snapshots), snap_name)

        try:
            with _mount_apfs_snapshot(snap_name, tm_volume) as mountpoint:
                lh_root = mountpoint / "Ladyhawke"
                for rel in rel_paths:
                    candidate = lh_root / rel
                    if candidate.is_file():
                        stat = file_stat(candidate)
                        results[rel].append((
                            _apfs_backup_path(tm_volume, snap_name, rel),
                            date_str,
                            stat["file_size"],
                            stat["mtime"],
                        ))
        except OSError as exc:
            print(
                f"  WARNING: skipping snapshot {snap_name}: {exc}",
                file=sys.stderr,
            )

    return results


# ---------------------------------------------------------------------------
# inspect command
# ---------------------------------------------------------------------------

def cmd_inspect(args):
    tm_volume = Path(args.tm_volume)
    if not tm_volume.exists():
        print(f"ERROR: Time Machine volume not found: {tm_volume}", file=sys.stderr)
        sys.exit(1)

    print(f"\n=== Inspecting Time Machine volume: {tm_volume} ===\n")

    layout = discover_tm_layout(tm_volume)

    print(f"Layout type : {layout['layout_type']}")

    for note in layout["raw_notes"]:
        print(f"Note        : {note}")

    if layout["layout_type"] == "apfs":
        snaps = layout.get("apfs_snapshots", [])
        print(f"\nAPFS Time Machine snapshots: {len(snaps)} total")
        if snaps:
            # Show a few oldest and newest; snapshots are newest-first after sorting
            sorted_snaps = sorted(snaps, key=lambda s: s["date"], reverse=True)
            display = sorted_snaps[:3]
            tail = sorted_snaps[-3:] if len(sorted_snaps) > 6 else []
            for s in display:
                print(f"  {s['date']}  {s['name']}")
            if len(sorted_snaps) > 6:
                print(f"  ... ({len(sorted_snaps) - 6} more) ...")
            for s in tail:
                if s not in display:
                    print(f"  {s['date']}  {s['name']}")
            print(f"\n  Newest: {sorted_snaps[0]['date']}")
            print(f"  Oldest: {sorted_snaps[-1]['date']}")
        print(
            "\nNote: APFS snapshots must be individually mounted to access their content."
            "\nThe 'scan' command will mount each snapshot automatically (requires root)."
        )
        return

    # modern / unknown layout
    print(f"Hosts found : {layout['hosts'] or ['(none)']}")

    for host, snap_dirs in layout.get("snapshots", {}).items():
        print(f"\nHost: {host}")
        print(f"  Snapshots ({len(snap_dirs)} total):")
        if snap_dirs:
            # Show oldest and newest to keep output compact
            for snap in snap_dirs[:3]:
                lh = snap / "Ladyhawke"
                has_lh = "✓ Ladyhawke" if lh.is_dir() else "  (no Ladyhawke)"
                print(f"    {snap.name}  {has_lh}")
            if len(snap_dirs) > 6:
                print(f"    ... ({len(snap_dirs) - 6} more) ...")
            for snap in snap_dirs[-3:]:
                if snap not in snap_dirs[:3]:
                    lh = snap / "Ladyhawke"
                    has_lh = "✓ Ladyhawke" if lh.is_dir() else "  (no Ladyhawke)"
                    print(f"    {snap.name}  {has_lh}")

    snapshots = all_ladyhawke_snapshots(layout)
    print(f"\nSnapshots containing Ladyhawke: {len(snapshots)}")
    if snapshots:
        print(f"  Newest: {snapshots[0][1]}  ({snapshots[0][0]})")
        print(f"  Oldest: {snapshots[-1][1]}  ({snapshots[-1][0]})")

    if not snapshots:
        print(
            "\nWARNING: No snapshots containing a 'Ladyhawke' directory were found.\n"
            "Check that the Time Machine volume is fully mounted and that the volume\n"
            "being backed up was named 'Ladyhawke'.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# scan command
# ---------------------------------------------------------------------------

def cmd_scan(args):
    csv_path = Path(args.csv)
    tm_volume = Path(args.tm_volume)

    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)
    if not tm_volume.exists():
        print(f"ERROR: Time Machine volume not found: {tm_volume}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading missing photos from {csv_path}...", file=sys.stderr)
    missing = load_missing_photos(csv_path)
    print(f"  {len(missing)} records loaded.", file=sys.stderr)

    print(f"Discovering Time Machine layout on {tm_volume}...", file=sys.stderr)
    layout = discover_tm_layout(tm_volume)
    is_apfs = layout["layout_type"] == "apfs"
    snapshots = all_ladyhawke_snapshots(layout)
    print(f"  Layout type: {layout['layout_type']}", file=sys.stderr)
    print(f"  {len(snapshots)} Time Machine snapshots found.", file=sys.stderr)
    if not snapshots:
        print(
            "WARNING: No Time Machine snapshots found. "
            "Every record will be classified NOT_FOUND_IN_TIME_MACHINE.",
            file=sys.stderr,
        )

    search_mode = args.search_mode
    month_interval = args.month_interval

    if is_apfs:
        # For APFS, each snapshot must be individually mounted.  We scan all
        # snapshots (mounting each one once) and check every rel_path per mount.
        # The anchored/full distinction only affects classic layouts; for APFS
        # we always perform a full scan but apply the monthly-anchor filter if
        # anchored mode is requested (to reduce the number of mounts).
        if search_mode == "anchored":
            if month_interval < 1:
                print("ERROR: --month-interval must be >= 1", file=sys.stderr)
                sys.exit(1)
            anchor_indices = build_month_anchor_indices(snapshots)
            probe_indices = set(anchor_indices[::month_interval])
            snapshots_to_scan = [
                snapshots[i] for i in sorted(probe_indices)
            ]
            # Re-sort newest-first after filtering
            snapshots_to_scan.sort(key=lambda t: t[1], reverse=True)
            print(
                f"  APFS anchored mode: probing {len(snapshots_to_scan)} monthly "
                f"anchor snapshots (interval={month_interval}).",
                file=sys.stderr,
            )
        else:
            snapshots_to_scan = snapshots
            print(
                f"  APFS full mode: will mount all {len(snapshots_to_scan)} snapshots.",
                file=sys.stderr,
            )
    else:
        snapshots_to_scan = snapshots
        month_anchor_indices = []
        if search_mode == "anchored":
            if month_interval < 1:
                print("ERROR: --month-interval must be >= 1", file=sys.stderr)
                sys.exit(1)
            month_anchor_indices = build_month_anchor_indices(snapshots)
            print(
                "  Anchored mode: "
                f"{len(month_anchor_indices)} monthly anchors, interval={month_interval}.",
                file=sys.stderr,
            )
        else:
            print("  Full mode: scanning all snapshots for each file.", file=sys.stderr)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    counters = {
        "total": 0,
        "ladyhawke": 0,
        "present": 0,
        "found": 0,
        "multiple": 0,
        "not_found": 0,
        "other": 0,
    }

    # -----------------------------------------------------------------------
    # APFS path: classify all files first, then do a single multi-file scan
    # across all snapshots (each snapshot mounted once).
    # -----------------------------------------------------------------------
    if is_apfs:
        # First pass: classify each record and collect rel_paths needing TM search.
        classified = []  # list of (row, expected_path, rel, classification)
        rel_paths_needing_search = []

        for row in missing:
            expected_path = row.get("Photo", "").strip()
            counters["total"] += 1
            rel = relative_ladyhawke_path(expected_path)
            if rel is None:
                classified.append((row, expected_path, None, "other"))
                counters["other"] += 1
            elif Path(expected_path).exists():
                classified.append((row, expected_path, rel, "present"))
                counters["present"] += 1
                counters["ladyhawke"] += 1
            else:
                classified.append((row, expected_path, rel, "search"))
                counters["ladyhawke"] += 1
                rel_paths_needing_search.append(rel)

        # Deduplicate rel_paths for the scan (there may be duplicates in the CSV).
        unique_rels = list(dict.fromkeys(rel_paths_needing_search))

        def _progress(i, total, snap_name):
            if i % 10 == 0 or i == total:
                print(
                    f"  Mounting snapshot {i}/{total}: {snap_name}",
                    file=sys.stderr,
                )

        if unique_rels and snapshots_to_scan:
            print(
                f"  Scanning {len(unique_rels)} unique paths across "
                f"{len(snapshots_to_scan)} snapshots...",
                file=sys.stderr,
            )
            apfs_results = scan_apfs_snapshots(
                unique_rels,
                snapshots_to_scan,
                tm_volume,
                progress_callback=_progress,
            )
        else:
            apfs_results = {r: [] for r in unique_rels}

        # Second pass: write the CSV.
        with open(output_path, "w", newline="", encoding="utf-8") as out_fh:
            writer = csv.DictWriter(out_fh, fieldnames=REPORT_COLUMNS)
            writer.writeheader()

            for row, expected_path, rel, classification in classified:
                if classification == "other":
                    writer.writerow(
                        {
                            "status": STATUS_OTHER,
                            "expected_path": expected_path,
                            "relative_path": "",
                            "backup_path": "",
                            "backup_date": "",
                            "file_size": "",
                            "mtime": "",
                            "notes": "Expected path is not under /Volumes/Ladyhawke",
                        }
                    )
                elif classification == "present":
                    writer.writerow(
                        {
                            "status": STATUS_PRESENT,
                            "expected_path": expected_path,
                            "relative_path": rel,
                            "backup_path": "",
                            "backup_date": "",
                            "file_size": "",
                            "mtime": "",
                            "notes": "File already exists at expected path",
                        }
                    )
                else:
                    matches = apfs_results.get(rel, [])
                    if not matches:
                        counters["not_found"] += 1
                        writer.writerow(
                            {
                                "status": STATUS_NOT_FOUND,
                                "expected_path": expected_path,
                                "relative_path": rel,
                                "backup_path": "",
                                "backup_date": "",
                                "file_size": "",
                                "mtime": "",
                                "notes": "",
                            }
                        )
                    else:
                        best_bp, best_date, best_size, best_mtime = matches[0]
                        if len(matches) == 1:
                            counters["found"] += 1
                            status = STATUS_FOUND
                            notes = ""
                        else:
                            counters["multiple"] += 1
                            status = STATUS_MULTIPLE
                            other_dates = ", ".join(d for _, d, _, _ in matches[1:])
                            notes = f"Also found in: {other_dates}"
                        writer.writerow(
                            {
                                "status": status,
                                "expected_path": expected_path,
                                "relative_path": rel,
                                "backup_path": best_bp,
                                "backup_date": best_date,
                                "file_size": best_size,
                                "mtime": best_mtime,
                                "notes": notes,
                            }
                        )

        print(f"\nReport written to {output_path}\n")
        _print_summary(counters)
        return

    # -----------------------------------------------------------------------
    # Classic (modern/HFS+) path: per-file search across pre-mounted snapshots.
    # -----------------------------------------------------------------------
    with open(output_path, "w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(out_fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()

        for i, row in enumerate(missing, 1):
            expected_path = row.get("Photo", "").strip()
            counters["total"] += 1

            if i % 200 == 0 or i == len(missing):
                print(f"  Processed {i}/{len(missing)}...", file=sys.stderr)

            rel = relative_ladyhawke_path(expected_path)

            if rel is None:
                counters["other"] += 1
                writer.writerow(
                    {
                        "status": STATUS_OTHER,
                        "expected_path": expected_path,
                        "relative_path": "",
                        "backup_path": "",
                        "backup_date": "",
                        "file_size": "",
                        "mtime": "",
                        "notes": "Expected path is not under /Volumes/Ladyhawke",
                    }
                )
                continue

            counters["ladyhawke"] += 1

            # Check whether the file is already present on Ladyhawke.
            if Path(expected_path).exists():
                counters["present"] += 1
                writer.writerow(
                    {
                        "status": STATUS_PRESENT,
                        "expected_path": expected_path,
                        "relative_path": rel,
                        "backup_path": "",
                        "backup_date": "",
                        "file_size": "",
                        "mtime": "",
                        "notes": "File already exists at expected path",
                    }
                )
                continue

            # Search Time Machine snapshots.
            if search_mode == "anchored":
                matches = find_in_snapshots_anchored(
                    rel,
                    snapshots_to_scan,
                    month_anchor_indices=month_anchor_indices,
                    month_interval=month_interval,
                )
            else:
                matches = find_in_snapshots(rel, snapshots_to_scan)

            if not matches:
                counters["not_found"] += 1
                writer.writerow(
                    {
                        "status": STATUS_NOT_FOUND,
                        "expected_path": expected_path,
                        "relative_path": rel,
                        "backup_path": "",
                        "backup_date": "",
                        "file_size": "",
                        "mtime": "",
                        "notes": "",
                    }
                )
                continue

            # Use the newest match as the primary backup source.
            best_path, best_date = matches[0]
            stat = file_stat(best_path)

            if len(matches) == 1:
                counters["found"] += 1
                status = STATUS_FOUND
                notes = ""
            else:
                counters["multiple"] += 1
                status = STATUS_MULTIPLE
                other_dates = ", ".join(d for _, d in matches[1:])
                notes = f"Also found in: {other_dates}"

            writer.writerow(
                {
                    "status": status,
                    "expected_path": expected_path,
                    "relative_path": rel,
                    "backup_path": str(best_path),
                    "backup_date": best_date,
                    "file_size": stat["file_size"],
                    "mtime": stat["mtime"],
                    "notes": notes,
                }
            )

    print(f"\nReport written to {output_path}\n")
    _print_summary(counters)


def _print_summary(counters):
    recoverable = counters["found"] + counters["multiple"]
    print(f"Missing Lightroom records examined : {counters['total']:>7,}")
    print(f"Ladyhawke records                  : {counters['ladyhawke']:>7,}")
    print(f"  Already present on disk          : {counters['present']:>7,}")
    print(f"  Found in Time Machine            : {counters['found']:>7,}")
    print(f"  Multiple TM versions             : {counters['multiple']:>7,}")
    print(f"  Not found in Time Machine        : {counters['not_found']:>7,}")
    print(f"Other/non-Ladyhawke volumes        : {counters['other']:>7,}")
    print(f"Total recoverable from TM          : {recoverable:>7,}")


# ---------------------------------------------------------------------------
# restore command
# ---------------------------------------------------------------------------

def cmd_restore(args):
    report_path = Path(args.report)
    dry_run = not args.execute

    if not report_path.exists():
        print(f"ERROR: Report not found: {report_path}", file=sys.stderr)
        sys.exit(1)

    mode_label = "DRY-RUN" if dry_run else "EXECUTE"
    print(f"\n=== Restore mode: {mode_label} ===\n")
    if dry_run:
        print(
            "No files will be copied.  Pass --execute to perform actual copies.\n"
        )

    log_path = Path(args.log) if args.log else report_path.parent / "restore_log.csv"
    log_columns = [
        "timestamp",
        "operation",
        "status",
        "source",
        "destination",
        "file_size",
        "sha256",
        "notes",
    ]

    log_rows = []

    def log(operation, status, source, destination, file_size="", sha256="", notes=""):
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        log_rows.append(
            {
                "timestamp": ts,
                "operation": operation,
                "status": status,
                "source": source,
                "destination": destination,
                "file_size": file_size,
                "sha256": sha256,
                "notes": notes,
            }
        )
        print(f"[{ts}] {operation} {status}: {source} → {destination}  {notes}")

    with open(report_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    eligible = [
        r for r in rows if r.get("status") in (STATUS_FOUND, STATUS_MULTIPLE)
    ]
    skipped = len(rows) - len(eligible)

    print(f"Total report rows        : {len(rows)}")
    print(f"Eligible for restore     : {len(eligible)}")
    print(f"Skipped (non-recoverable): {skipped}\n")

    copied = 0
    errors = 0

    # Group APFS rows by snapshot name to minimise the number of mounts.
    apfs_rows = [r for r in eligible if is_apfs_backup_path(r.get("backup_path", ""))]
    classic_rows = [r for r in eligible if not is_apfs_backup_path(r.get("backup_path", ""))]

    def _restore_file(src: Path, dst: Path, backup: str, expected: str, date: str):
        nonlocal copied, errors
        if not src.exists():
            log("COPY", "ERROR", backup, expected, notes="Source no longer exists")
            errors += 1
            return
        if not src.is_file():
            log("COPY", "ERROR", backup, expected, notes="Source is not a regular file")
            errors += 1
            return
        if dst.exists():
            log(
                "COPY",
                "SKIP",
                backup,
                expected,
                notes="Destination already exists; will not overwrite",
            )
            return

        sha = ""
        if args.verify_hash:
            sha = _sha256(src)

        if dry_run:
            st = file_stat(src)
            log(
                "COPY",
                "DRY-RUN",
                backup,
                expected,
                file_size=st["file_size"],
                sha256=sha,
                notes=f"snapshot={date}",
            )
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Copy file preserving metadata; use normal copy (not hardlink) from TM
                shutil.copy2(str(src), str(dst))
                if not dst.is_file():
                    raise OSError("Destination file not created")
                st = file_stat(dst)
                if not sha and args.verify_hash:
                    sha = _sha256(dst)
                log(
                    "COPY",
                    "OK",
                    backup,
                    expected,
                    file_size=st["file_size"],
                    sha256=sha,
                    notes=f"snapshot={date}",
                )
                copied += 1
            except Exception as exc:
                log("COPY", "ERROR", backup, expected, notes=str(exc))
                errors += 1

    # --- Classic (HFS+) rows: source paths are plain filesystem paths ---
    for row in classic_rows:
        expected = row["expected_path"]
        backup = row["backup_path"]
        date = row.get("backup_date", "")
        _restore_file(Path(backup), Path(expected), backup, expected, date)

    # --- APFS rows: group by (tm_volume, snap_name) to mount each snapshot once ---
    def _apfs_sort_key(row):
        bp = row.get("backup_path", "")
        try:
            tm_vol, snap_name, _ = _parse_apfs_backup_path(bp)
            return (str(tm_vol), snap_name)
        except ValueError:
            return ("", "")

    apfs_rows_sorted = sorted(apfs_rows, key=_apfs_sort_key)
    for (tm_vol_str, snap_name), group_rows in groupby(apfs_rows_sorted, key=_apfs_sort_key):
        group_list = list(group_rows)
        if not snap_name:
            for row in group_list:
                log("COPY", "ERROR", row["backup_path"], row["expected_path"],
                    notes="Malformed APFS backup path")
                errors += 1
            continue

        tm_vol = Path(tm_vol_str)
        print(
            f"  Mounting APFS snapshot {snap_name} "
            f"({len(group_list)} file(s))...",
            file=sys.stderr,
        )
        try:
            with _mount_apfs_snapshot(snap_name, tm_vol) as mountpoint:
                for row in group_list:
                    backup = row["backup_path"]
                    expected = row["expected_path"]
                    date = row.get("backup_date", "")
                    try:
                        _, _, rel = _parse_apfs_backup_path(backup)
                    except ValueError:
                        log("COPY", "ERROR", backup, expected,
                            notes="Malformed APFS backup path")
                        errors += 1
                        continue
                    src = mountpoint / "Ladyhawke" / rel
                    _restore_file(src, Path(expected), backup, expected, date)
        except OSError as exc:
            for row in group_list:
                log("COPY", "ERROR", row["backup_path"], row["expected_path"],
                    notes=f"Could not mount snapshot: {exc}")
                errors += 1

    # Write log
    with open(log_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=log_columns)
        writer.writeheader()
        writer.writerows(log_rows)

    print(f"\nLog written to {log_path}")
    if dry_run:
        print(f"DRY-RUN complete. Would copy: {len(eligible)} files.  Errors: {errors}")
    else:
        print(f"Restore complete. Copied: {copied}  Errors: {errors}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# hash command — optional per-file SHA-256 verification
# ---------------------------------------------------------------------------

def cmd_hash(args):
    """Print the SHA-256 hash of one or more files (for manual verification)."""
    for p in args.files:
        path = Path(p)
        if not path.is_file():
            print(f"NOT A FILE: {p}")
            continue
        digest = _sha256(path)
        print(f"{digest}  {p}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Recover Lightroom Classic missing originals from a Time Machine backup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = sub.add_parser(
        "inspect", help="Discover and report Time Machine layout."
    )
    p_inspect.add_argument(
        "tm_volume",
        help="Path to mounted Time Machine volume (e.g. /Volumes/iMacBackup3).",
    )
    p_inspect.set_defaults(func=cmd_inspect)

    # scan
    p_scan = sub.add_parser(
        "scan", help="Search Time Machine for missing Lightroom originals."
    )
    p_scan.add_argument(
        "csv", help="Path to Lightroom missing-photos CSV (must have a 'Photo' column)."
    )
    p_scan.add_argument(
        "tm_volume",
        help="Path to mounted Time Machine volume (e.g. /Volumes/iMacBackup3).",
    )
    p_scan.add_argument(
        "--output",
        default=DEFAULT_REPORT,
        help=f"Output CSV report path (default: {DEFAULT_REPORT}).",
    )
    p_scan.add_argument(
        "--search-mode",
        choices=("full", "anchored"),
        default="full",
        help=(
            "Snapshot search strategy. "
            "'full' scans all snapshots (exhaustive). "
            "'anchored' probes monthly anchors first, then scans forward to newest match."
        ),
    )
    p_scan.add_argument(
        "--month-interval",
        type=int,
        default=1,
        help=(
            "Month step used by --search-mode anchored (default: 1). "
            "Example: 1 probes every month, 2 probes every other month."
        ),
    )
    p_scan.set_defaults(func=cmd_scan)

    # restore
    p_restore = sub.add_parser(
        "restore",
        help=(
            "Copy Time Machine originals to their Lightroom-expected paths. "
            "Default is dry-run; pass --execute to copy files."
        ),
    )
    p_restore.add_argument(
        "--report",
        default=DEFAULT_REPORT,
        help=f"Recovery candidates CSV from 'scan' (default: {DEFAULT_REPORT}).",
    )
    p_restore.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview only; do not copy any files (default).",
    )
    p_restore.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually copy files. Without this flag the command is always a dry-run.",
    )
    p_restore.add_argument(
        "--log",
        default=None,
        help="Path for the operation log CSV (default: next to --report).",
    )
    p_restore.add_argument(
        "--verify-hash",
        action="store_true",
        default=False,
        help="Calculate SHA-256 of each copied file (slower but provides integrity check).",
    )
    p_restore.set_defaults(func=cmd_restore)

    # hash
    p_hash = sub.add_parser(
        "hash", help="Print SHA-256 hashes for one or more files."
    )
    p_hash.add_argument("files", nargs="+", help="Files to hash.")
    p_hash.set_defaults(func=cmd_hash)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
