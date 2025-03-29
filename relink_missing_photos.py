import os
import csv
import argparse
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from PIL import ExifTags
import pandas as pd
import random
from dateutil import parser as dateparser

# Constants
TIME_DELTA = timedelta(minutes=5)
RAW_EXTENSIONS = {".dng", ".orf", ".arw", ".cr2", ".nef", ".rw2", ".raf", ".pef"}

def get_exif_data_exiftool(image_path):
    try:
        result = subprocess.run(
            ["exiftool", "-Make", "-ImageWidth", "-ImageHeight", "-DateTimeOriginal", image_path],
            capture_output=True, text=True, check=True
        )
        data = {}
        for line in result.stdout.strip().splitlines():
            if ':' not in line:
                continue
            key, val = line.split(':', 1)
            data[key.strip()] = val.strip()
        return {
            "Camera Make": data.get("Make", ""),
            "Width": int(data.get("Image Width", "0").replace(" pixels", "")),
            "Height": int(data.get("Image Height", "0").replace(" pixels", "")),
            "DateTime": data.get("Date/Time Original", "")
        }
    except subprocess.CalledProcessError as e:
        print(f"❌ Error running exiftool on {image_path}: {e.stderr}", file=sys.stderr)
        return None

def parse_datetime(dt_str):
    try:
        dt_str = dt_str.strip()
        if ":" in dt_str[:10]:
            dt_str = dt_str[:19]
            return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        return dateparser.parse(dt_str)
    except Exception as e:
        print(f"⚠️ Failed to parse datetime: {dt_str} ({e})", file=sys.stderr)
        return None

def index_files_by_stem(search_root, exclude_sources):
    print(f"Indexing files under {search_root}...", file=sys.stderr)
    index = {}
    for root, _, files in os.walk(search_root):
        if any(excl in root for excl in exclude_sources):
            continue
        for file in files:
            stem = Path(file).stem
            full_path = Path(root) / file
            index.setdefault(stem.lower(), []).append(full_path)
    print(f"Indexed {sum(len(v) for v in index.values())} files.\n", file=sys.stderr)
    return index

def is_raw_file(path):
    return Path(path).suffix.lower() in RAW_EXTENSIONS

def main(csv_filename, test_n=None, exclude_sources=None, exclude_targets=None):
    try:
        missing_photos_df = pd.read_csv(csv_filename)
    except Exception as e:
        print(f"Error reading CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    if test_n is not None:
        print(f"Running test mode with {test_n} random entries...", file=sys.stderr)
        missing_photos_df = missing_photos_df.sample(n=test_n, random_state=42)

    file_index = index_files_by_stem("/Volumes/Ladyhawke", exclude_sources or [])

    relink_commands = []
    ambiguous_matches = []
    still_missing = []
    resolution_mismatches = []

    total = len(missing_photos_df)
    print(f"Processing {total} rows...\n", file=sys.stderr)

    for i, (_, row) in enumerate(missing_photos_df.iterrows(), 1):
        original_path = row['Photo']
        if exclude_targets and any(excl in original_path for excl in exclude_targets):
            continue

        filename = Path(original_path).name
        stem = Path(filename).stem.lower()

        candidates = file_index.get(stem, [])

        if not candidates:
            still_missing.append(row)
            continue

        if pd.isna(row.get("Date/Time Original (Capture)")) or pd.isna(row.get("Width")) or pd.isna(row.get("Height")):
            still_missing.append(row)
            continue

        target_time = parse_datetime(row['Date/Time Original (Capture)'])
        if not target_time:
            still_missing.append(row)
            continue

        csv_camera = str(row.get('Camera Make') or '').strip().lower()

        def score(candidate):
            meta = get_exif_data_exiftool(candidate)
            if not meta:
                return None
            cand_time = parse_datetime(meta['DateTime'])
            if not cand_time:
                return None
            if (target_time.tzinfo is None or cand_time.tzinfo is None):
                target_time_naive = target_time.replace(tzinfo=None)
                cand_time_naive = cand_time.replace(tzinfo=None)
            else:
                target_time_naive = target_time
                cand_time_naive = cand_time
            if abs(cand_time_naive - target_time_naive) > TIME_DELTA:
                return None
            file_camera = meta['Camera Make'].strip().lower()
            camera_ok = not csv_camera or not file_camera or csv_camera in file_camera or file_camera in csv_camera
            if not camera_ok:
                return None
            return {
                'path': candidate,
                'meta': meta,
                'raw': is_raw_file(candidate),
                'camera_score': 2 if csv_camera and file_camera and csv_camera == file_camera else 1 if csv_camera in file_camera or file_camera in csv_camera else 0,
                'resolution': meta['Width'] * meta['Height']
            }

        scored = list(filter(None, (score(c) for c in candidates)))

        exact_matches = [s for s in scored if s['meta']['Width'] == int(row['Width']) and s['meta']['Height'] == int(row['Height'])]

        if len(exact_matches) == 1:
            relink_commands.append(f'ln "{exact_matches[0]["path"]}" "{original_path}"')
        elif len(exact_matches) > 1:
            sorted_matches = sorted(exact_matches, key=lambda x: (-x['raw'], -x['camera_score'], -x['resolution']))
            best = sorted_matches[0]
            relink_commands.append(f'# Selected best match from {len(sorted_matches)} candidates')
            relink_commands.append(f'ln "{best["path"]}" "{original_path}"')
            for alt in sorted_matches[1:]:
                relink_commands.append(f'# Alt: {alt["path"]} ({alt["meta"]["Width"]}x{alt["meta"]["Height"]}, {alt["meta"]["Camera Make"]})')
        elif scored:
            resolution_sorted = sorted(scored, key=lambda x: (-x['raw'], -x['camera_score'], -x['resolution']))
            best = resolution_sorted[0]
            resolution_mismatches.append(f'# Resolution mismatch: {original_path}')
            resolution_mismatches.append(f'ln "{best["path"]}" "{original_path}"')
            for alt in resolution_sorted[1:]:
                resolution_mismatches.append(f'# Alt: {alt["path"]} ({alt["meta"]["Width"]}x{alt["meta"]["Height"]}, {alt["meta"]["Camera Make"]})')
        else:
            still_missing.append(row)

        if i % 100 == 0 or i == total:
            print(f"Processed {i}/{total} rows...", file=sys.stderr)

    with open("relink_good_matches.sh", "w") as f:
        f.write("#!/bin/bash\n")
        for cmd in relink_commands:
            f.write(cmd + "\n")

    with open("resolution_mismatch.sh", "w") as f:
        f.write("#!/bin/bash\n")
        for line in resolution_mismatches:
            f.write(f"{line}\n")

    still_missing_df = pd.DataFrame(still_missing)
    still_missing_df.to_csv("Still_Missing_Photos.csv", index=False)

    print("\nSummary:", file=sys.stderr)
    print(f"  Relink commands generated: {len(relink_commands)}", file=sys.stderr)
    print(f"  Resolution mismatches: {len(resolution_mismatches)}", file=sys.stderr)
    print(f"  Still missing: {len(still_missing)}", file=sys.stderr)
    print("\nDone. Outputs: relink_good_matches.sh, resolution_mismatch.sh, Still_Missing_Photos.csv")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find and relink missing photos by matching metadata.")
    parser.add_argument("csv_filename", help="Path to the CSV file containing missing photos metadata.")
    parser.add_argument("--test-n", type=int, help="Run script on a random sample of N rows for testing.")
    parser.add_argument("--exclude-sources", nargs='*', help="Paths to exclude as candidate sources.")
    parser.add_argument("--exclude-targets", nargs='*', help="Paths to exclude from processing as missing targets.")
    args = parser.parse_args()
    main(args.csv_filename, args.test_n, args.exclude_sources, args.exclude_targets)