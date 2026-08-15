#!/usr/bin/env python3

import csv
import sys
from collections import Counter
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} MISSING_PHOTOS.csv", file=sys.stderr)
        sys.exit(2)

    csv_path = Path(sys.argv[1])
    counts = Counter()

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        if "Photo" not in (reader.fieldnames or []):
            print("ERROR: CSV has no 'Photo' column", file=sys.stderr)
            sys.exit(1)

        for row in reader:
            photo = (row.get("Photo") or "").strip()
            if not photo:
                continue

            parent = Path(photo).parent

            # Count every directory level, excluding "/"
            while parent != parent.parent:
                counts[str(parent)] += 1
                parent = parent.parent

    print(f"{'COUNT':>8}  DIRECTORY")
    print(f"{'-----':>8}  ---------")

    # Largest count first; alphabetically for ties
    for directory, count in sorted(
        counts.items(),
        key=lambda x: (-x[1], x[0])
    ):
        print(f"{count:8,d}  {directory}")


if __name__ == "__main__":
    main()
