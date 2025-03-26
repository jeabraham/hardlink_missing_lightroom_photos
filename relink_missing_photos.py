import os
import csv
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
from PIL import Image
from PIL.ExifTags import TAGS
import pandas as pd
import random

# Constants
SEARCH_ROOT = "/Volumes/Ladyhawke"
TIME_DELTA = timedelta(minutes=5)

def get_exif_data(image_path):
    try:
        image = Image.open(image_path)
        exif_data = image._getexif() or {}
        exif = {TAGS.get(tag): val for tag, val in exif_data.items() if tag in TAGS}

        width, height = image.size
        return {
            'Camera Make': exif.get('Make', ''),
            'DateTime': exif.get('DateTimeOriginal', ''),
            'Width': width,
            'Height': height
        }
    except Exception as e:
        print(f"Error reading EXIF from {image_path}: {e}", file=sys.stderr)
        return None

def parse_datetime(dt_str):
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(dt_str)
        except Exception:
            return None

def index_files_by_stem(search_root):
    print(f"Indexing files under {search_root}...", file=sys.stderr)
    index = {}
    for root, _, files in os.walk(search_root):
        for file in files:
            stem = Path(file).stem
            full_path = Path(root) / file
            index.setdefault(stem.lower(), []).append(full_path)
    print(f"Indexed {sum(len(v) for v in index.values())} files.\n", file=sys.stderr)
    return index

def main(csv_filename, test_n=None):
    try:
        missing_photos_df = pd.read_csv(csv_filename)
    except Exception as e:
        print(f"Error reading CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    if test_n is not None:
        print(f"Running test mode with {test_n} random entries...", file=sys.stderr)
        missing_photos_df = missing_photos_df.sample(n=test_n, random_state=42)

    file_index = index_files_by_stem(SEARCH_ROOT)

    relink_commands = []
    ambiguous_matches = []
    still_missing = []

    total = len(missing_photos_df)
    print(f"Processing {total} rows...\n", file=sys.stderr)

    for i, (_, row) in enumerate(missing_photos_df.iterrows(), 1):
        original_path = row['Photo']
        filename = Path(original_path).name
        stem = Path(filename).stem.lower()

        candidates = file_index.get(stem, [])

        if not candidates:
            still_missing.append(row)
            continue

        if pd.isna(row.get("Date/Time Original (Capture)")) or pd.isna(row.get("Camera Make")) or pd.isna(row.get("Width")) or pd.isna(row.get("Height")):
            still_missing.append(row)
            continue

        target_time = parse_datetime(row['Date/Time Original (Capture)'])
        if not target_time:
            still_missing.append(row)
            continue

        good_matches = []
        for candidate in candidates:
            metadata = get_exif_data(candidate)
            if not metadata:
                continue

            candidate_time = parse_datetime(metadata['DateTime'])
            if not candidate_time:
                continue

            if (metadata['Camera Make'].strip().lower() == str(row['Camera Make']).strip().lower() and
                metadata['Width'] == int(row['Width']) and
                metadata['Height'] == int(row['Height']) and
                abs(candidate_time - target_time) <= TIME_DELTA):
                good_matches.append(candidate)

        if len(good_matches) == 1:
            relink_commands.append(f'ln "{good_matches[0]}" "{original_path}"')
        elif len(good_matches) > 1:
            ambiguous_matches.append({"Missing": original_path, "Candidates": [str(m) for m in good_matches]})
        else:
            still_missing.append(row)

        if i % 100 == 0 or i == total:
            print(f"Processed {i}/{total} rows...", file=sys.stderr)

    with open("relink_good_matches.sh", "w") as f:
        f.write("#!/bin/bash\n")
        for cmd in relink_commands:
            f.write(cmd + "\n")

    with open("ambiguous_matches.csv", "w", newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["Missing", "Candidates"])
        writer.writeheader()
        for item in ambiguous_matches:
            writer.writerow({"Missing": item["Missing"], "Candidates": "; ".join(item["Candidates"])})

    still_missing_df = pd.DataFrame(still_missing)
    still_missing_df.to_csv("Still_Missing_Photos.csv", index=False)

    print("\nSummary:", file=sys.stderr)
    print(f"  Relink commands generated: {len(relink_commands)}", file=sys.stderr)
    print(f"  Ambiguous matches: {len(ambiguous_matches)}", file=sys.stderr)
    print(f"  Still missing: {len(still_missing)}", file=sys.stderr)
    print("\nDone. Outputs: relink_good_matches.sh, ambiguous_matches.csv, Still_Missing_Photos.csv")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find and relink missing photos by matching metadata.")
    parser.add_argument("csv_filename", help="Path to the CSV file containing missing photos metadata.")
    parser.add_argument("--test-n", type=int, help="Run script on a random sample of N rows for testing.")
    args = parser.parse_args()
    main(args.csv_filename, args.test_n)
