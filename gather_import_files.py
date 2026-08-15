#!/usr/bin/env python3
"""Gather Lightroom import candidates into one directory via hard links or copies."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
from pathlib import Path
from typing import Dict, List, Tuple


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}. Use true/false.")


def split_name(filename: str) -> Tuple[str, str]:
    path = Path(filename)
    return path.stem, path.suffix


def choose_destination_path(source: Path, destination_dir: Path) -> Tuple[Path, bool]:
    """Return destination path and whether a suffix was required due to a name collision."""
    stem, suffix = split_name(source.name)
    preferred = destination_dir / source.name
    if not preferred.exists():
        return preferred, False

    index = 1
    while True:
        candidate = destination_dir / f"{stem}__{index}{suffix}"
        if not candidate.exists():
            return candidate, True
        index += 1


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def gather_files(
    csv_path: Path,
    tempdir: Path,
    column_name: str,
    copy_across_volumes: bool,
    remaining_csv_path: Path,
) -> None:
    stats = {
        "rows_examined": 0,
        "valid_source_paths": 0,
        "hard_linked": 0,
        "copied": 0,
        "already_present": 0,
        "missing_unreadable": 0,
        "skipped_other_volume": 0,
        "failed_other": 0,
    }

    tempdir = tempdir.resolve()
    tempdir.mkdir(parents=True, exist_ok=True)
    destination_dev = os.stat(tempdir).st_dev

    skipped_rows: List[Dict[str, str]] = []

    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or column_name not in reader.fieldnames:
            raise ValueError(f"Column {column_name!r} not found in CSV header.")

        for row in reader:
            stats["rows_examined"] += 1
            source_value = (row.get(column_name) or "").strip()
            if not source_value:
                stats["missing_unreadable"] += 1
                continue

            source = Path(source_value).expanduser()
            try:
                source_stat = source.stat()
            except OSError:
                stats["missing_unreadable"] += 1
                continue

            if not source.is_file() or not os.access(source, os.R_OK):
                stats["missing_unreadable"] += 1
                continue

            stats["valid_source_paths"] += 1
            same_volume = source_stat.st_dev == destination_dev

            if not same_volume and not copy_across_volumes:
                stats["skipped_other_volume"] += 1
                skipped_rows.append(row)
                continue

            try:
                preferred_destination = tempdir / source.name
                if preferred_destination.exists() and os.path.samefile(source, preferred_destination):
                    stats["already_present"] += 1
                    continue
                destination, _ = choose_destination_path(source, tempdir)

                ensure_parent(destination)

                if same_volume:
                    os.link(source, destination)
                    stats["hard_linked"] += 1
                else:
                    shutil.copy2(source, destination)
                    stats["copied"] += 1
            except FileExistsError:
                stats["already_present"] += 1
            except OSError:
                stats["failed_other"] += 1

        if skipped_rows and not copy_across_volumes:
            with remaining_csv_path.open("w", newline="", encoding="utf-8") as outf:
                writer = csv.DictWriter(outf, fieldnames=reader.fieldnames)
                writer.writeheader()
                writer.writerows(skipped_rows)

    print("Gather summary")
    print(f"  CSV rows examined:                    {stats['rows_examined']}")
    print(f"  Valid source paths:                  {stats['valid_source_paths']}")
    print(f"  Hard-linked:                         {stats['hard_linked']}")
    print(f"  Copied:                              {stats['copied']}")
    print(f"  Already present in destination:      {stats['already_present']}")
    print(f"  Missing/unreadable:                  {stats['missing_unreadable']}")
    print(f"  Skipped (other volume):              {stats['skipped_other_volume']}")
    print(f"  Failed (other reasons):              {stats['failed_other']}")

    if skipped_rows and not copy_across_volumes:
        print(f"  Remaining rows CSV:                  {remaining_csv_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gather files from a CSV column into one import directory via hard links or copies."
    )
    parser.add_argument("csv_file", help="Path to input CSV (e.g. import_other_formats.csv)")
    parser.add_argument(
        "--copy-across-volumes",
        type=parse_bool,
        default=False,
        help="When true, copy files from other volumes into --tempdir (default: false).",
    )
    parser.add_argument(
        "--tempdir",
        required=True,
        help="Directory to gather files into for Lightroom import.",
    )
    parser.add_argument(
        "--column-name",
        default="new_file",
        help="CSV column containing source file paths (default: new_file).",
    )
    parser.add_argument(
        "--remaining-csv",
        default=None,
        help=(
            "Output path for ungathered rows when --copy-across-volumes=false. "
            "Defaults to <input_stem>_remaining_other_volume.csv beside the input CSV."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    csv_path = Path(args.csv_file).expanduser().resolve()
    if not csv_path.exists():
        parser.error(f"CSV file not found: {csv_path}")

    remaining_csv_path = (
        Path(args.remaining_csv).expanduser().resolve()
        if args.remaining_csv
        else csv_path.with_name(f"{csv_path.stem}_remaining_other_volume.csv")
    )

    gather_files(
        csv_path=csv_path,
        tempdir=Path(args.tempdir).expanduser(),
        column_name=args.column_name,
        copy_across_volumes=args.copy_across_volumes,
        remaining_csv_path=remaining_csv_path,
    )


if __name__ == "__main__":
    main()
