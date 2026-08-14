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
import sys
from datetime import datetime
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

def discover_tm_layout(tm_volume: Path) -> dict:
    """
    Inspect a mounted Time Machine volume and return a dict describing its
    layout.  Handles both the modern macOS layout (Backups.backupdb/<host>/)
    and older HFS+ layout variants.

    Returns a dict with:
      layout_type: "modern" | "unknown"
      hosts: [str, ...]            — subdirectory names under Backups.backupdb
      snapshots: {host: [Path]}    — sorted list of snapshot dirs per host
      ladyhawke_roots: [Path]      — candidate dirs that look like Ladyhawke
    """
    result = {
        "layout_type": "unknown",
        "hosts": [],
        "snapshots": {},
        "ladyhawke_roots": [],
        "raw_notes": [],
    }

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
            # Time Machine stores backed-up volumes as subdirectories of each
            # snapshot directory named after the volume (e.g. "Ladyhawke").
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
    Return a list of (snapshot_path, date_str) tuples, newest-first, for every
    snapshot that contains a Ladyhawke volume directory.
    """
    results = []
    for host, snap_dirs in layout.get("snapshots", {}).items():
        for snap_dir in snap_dirs:
            lh = snap_dir / "Ladyhawke"
            if lh.is_dir():
                results.append((lh, snap_dir.name))  # snap_dir.name is the date string
    # Sort newest first (date strings are lexicographically sortable).
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
    print(f"Hosts found : {layout['hosts'] or ['(none)']}")

    for note in layout["raw_notes"]:
        print(f"Note        : {note}")

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
    snapshots = all_ladyhawke_snapshots(layout)
    print(f"  {len(snapshots)} snapshots containing Ladyhawke found.", file=sys.stderr)
    if not snapshots:
        print(
            "WARNING: No Ladyhawke snapshots found. "
            "Every record will be classified NOT_FOUND_IN_TIME_MACHINE.",
            file=sys.stderr,
        )

    search_mode = args.search_mode
    month_interval = args.month_interval
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
                    snapshots,
                    month_anchor_indices=month_anchor_indices,
                    month_interval=month_interval,
                )
            else:
                matches = find_in_snapshots(rel, snapshots)

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

    for row in eligible:
        expected = row["expected_path"]
        backup = row["backup_path"]
        date = row.get("backup_date", "")

        src = Path(backup)
        dst = Path(expected)

        # Safety checks
        if not src.exists():
            log("COPY", "ERROR", backup, expected, notes="Source no longer exists")
            errors += 1
            continue
        if not src.is_file():
            log("COPY", "ERROR", backup, expected, notes="Source is not a regular file")
            errors += 1
            continue
        if dst.exists():
            log(
                "COPY",
                "SKIP",
                backup,
                expected,
                notes="Destination already exists; will not overwrite",
            )
            continue

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
