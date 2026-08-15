import os
import csv
import shlex
import argparse
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd
from dateutil import parser as dateparser

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Constants
TIME_DELTA = timedelta(minutes=5)
TZ_MISMATCH_RESIDUAL = timedelta(minutes=1)  # allowed residual after removing tz offset
TZ_MISMATCH_MAX = timedelta(hours=26)         # max tz offset to consider
RAW_EXTENSIONS = {".dng", ".orf", ".arw", ".cr2", ".nef", ".rw2", ".raf", ".pef"}
IGNORED_CANDIDATE_EXTENSIONS = {".xmp"}

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

def parse_datetime(value):
    try:
        if value is None:
            return None
        # Apple/Cocoa timestamp: seconds since 2001-01-01 UTC
        if isinstance(value, (int, float)):
            return (APPLE_EPOCH + timedelta(seconds=float(value))).replace(tzinfo=None)
        dt_str = str(value).strip()
        if not dt_str:
            return None
        # Also accept Apple timestamps that arrived as strings
        try:
            numeric_value = float(dt_str)
            return (APPLE_EPOCH + timedelta(seconds=numeric_value)).replace(tzinfo=None)
        except ValueError:
            pass
        # EXIF-style datetime
        if ":" in dt_str[:10]:
            dt_str = dt_str[:19]
            return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        # Everything else — strip any tz info dateparser may attach
        result = dateparser.parse(dt_str)
        return result.replace(tzinfo=None) if result is not None else None
    except Exception as e:
        print(f"⚠️ Failed to parse datetime: {value} ({e})", file=sys.stderr)
        return None

def index_files_by_stem(search_root, exclude_sources):
    print(f"Indexing files under {search_root}...", file=sys.stderr)
    index = {}
    for root, _, files in os.walk(search_root):
        if any(excl in root for excl in exclude_sources):
            continue
        for file in files:
            if Path(file).suffix.lower() in IGNORED_CANDIDATE_EXTENSIONS:
                continue
            stem = Path(file).stem
            full_path = Path(root) / file
            index.setdefault(stem.lower(), []).append(full_path)
    print(f"Indexed {sum(len(v) for v in index.values())} files.\n", file=sys.stderr)
    return index

def find_candidates_mdfind(stem, search_root=None, exclude_sources=None):
    """Use macOS Spotlight (mdfind) to locate files matching stem, then filter."""
    cmd = ["mdfind", "-name", stem]
    if search_root:
        cmd += ["-onlyin", str(search_root)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ mdfind error for {stem}: {e.stderr}", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("❌ mdfind not found — are you running on macOS?", file=sys.stderr)
        return []
    candidates = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        # mdfind -name matches on any part of the filename; keep only exact stem matches
        if p.stem.lower() != stem.lower():
            continue
        if p.suffix.lower() in IGNORED_CANDIDATE_EXTENSIONS:
            continue
        if exclude_sources and any(excl in str(p) for excl in exclude_sources):
            continue
        candidates.append(p)
    return candidates

def get_volume(path):
    """Return the top-level mount point (volume) of a path."""
    p = Path(path).resolve()
    parts = p.parts
    # On macOS: /Volumes/VolName/...
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path(parts[0]) / parts[1] / parts[2]
    # Fallback: filesystem root
    return Path(parts[0])

def make_link_or_copy_command(candidate_path, original_path, copy_across_volumes):
    """Return a shell command string to link or copy a candidate to the target location."""
    if copy_across_volumes:
        src_vol = get_volume(candidate_path)
        dst_vol = get_volume(original_path)
        if src_vol != dst_vol:
            return f'cp "{candidate_path}" "{original_path}"'
    return f'ln "{candidate_path}" "{original_path}"'

def is_raw_file(path):
    return Path(path).suffix.lower() in RAW_EXTENSIONS

def sort_key(candidate):
    # Prefer: exact tz, raw format, best camera match, then higher resolution (all else equal)
    return (candidate['tz_adjusted'], -candidate['raw'], -candidate['camera_score'], -candidate['resolution'])


def format_candidate_meta(candidate):
    return f'{candidate["path"]} ({candidate["meta"]["Width"]}x{candidate["meta"]["Height"]}, {candidate["meta"]["Camera Make"]})'

def timezone_offset_match(time_diff):
    """Return True if time_diff is within TZ_MISMATCH_RESIDUAL of an even half-hour offset
    (0, 30, 60, 90 … minutes) up to TZ_MISMATCH_MAX.  Covers all real-world timezone
    offsets including Newfoundland (UTC-3:30) and rare ±30-min zones."""
    if time_diff > TZ_MISMATCH_MAX:
        return False
    total_seconds = time_diff.total_seconds()
    half_hour_seconds = 1800  # 30 minutes
    nearest_half_hours = round(total_seconds / half_hour_seconds)
    residual = abs(total_seconds - nearest_half_hours * half_hour_seconds)
    return timedelta(seconds=residual) <= TZ_MISMATCH_RESIDUAL

def open_output(path, append):
    """Open a shell-script output file for writing or appending; write shebang only when creating."""
    mode = "a" if append else "w"
    existed = append and Path(path).exists()
    f = open(path, mode)
    if not existed:
        f.write("#!/bin/bash\n")
    return f

def _print_interrupt_resume(csv_filename, last_completed_row, skip_rows,
                             rows_to_process, output_dir, argv):
    """Print the last completed row and a ready-to-paste resume command to stderr."""
    print(f"\n⚠️  Interrupted after completing row {last_completed_row} "
          f"(0-based CSV data row, not counting header).", file=sys.stderr)
    # Rebuild resume command from original argv, replacing windowing flags
    skip_flags = {"--skip-rows", "--rows-to-process"}
    parts = [argv[0]]
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in skip_flags:
            skip_next = True
            continue
        if any(arg.startswith(f + "=") for f in skip_flags):
            continue
        if arg == "--append-outputs":
            continue
        parts.append(arg)
    parts += ["--skip-rows", str(last_completed_row), "--append-outputs"]
    if rows_to_process is not None:
        remaining = rows_to_process - (last_completed_row - skip_rows)
        if remaining > 0:
            parts += ["--rows-to-process", str(remaining)]
    print("Resume with:", file=sys.stderr)
    print("  " + " ".join(shlex.quote(p) for p in parts), file=sys.stderr)

def main(csv_filename, search_root=None, test_n=None, exclude_sources=None,
         exclude_targets=None, use_mdfind=False, copy_across_volumes=False,
         output_dir=None, skip_rows=0, rows_to_process=None,
         append_outputs=False, debug=False, allow_timezone_mismatches=False):

    out_dir = Path(output_dir) if output_dir else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    relink_path = out_dir / "relink_good_matches.sh"
    mismatch_path = out_dir / "resolution_mismatch.sh"
    still_missing_path = out_dir / "Still_Missing_Photos.csv"
    import_other_formats_path = out_dir / "import_other_formats.csv"
    import_same_format_higher_res_path = out_dir / "import_same_format_higher_resolution.csv"

    try:
        missing_photos_df = pd.read_csv(csv_filename)
    except Exception as e:
        print(f"Error reading CSV file: {e}", file=sys.stderr)
        sys.exit(1)

    # Apply row windowing
    if skip_rows:
        missing_photos_df = missing_photos_df.iloc[skip_rows:]
    if rows_to_process is not None:
        missing_photos_df = missing_photos_df.iloc[:rows_to_process]

    if test_n is not None:
        print(f"Running test mode with {test_n} random entries...", file=sys.stderr)
        missing_photos_df = missing_photos_df.sample(n=min(test_n, len(missing_photos_df)))

    # Build full-tree index only when not using mdfind
    if not use_mdfind:
        if search_root is None:
            print("Error: --search-root is required unless --mdfind is specified.", file=sys.stderr)
            sys.exit(1)
        file_index = index_files_by_stem(search_root, exclude_sources or [])
    else:
        file_index = None  # candidates fetched per-stem via mdfind

    relink_commands = []
    still_missing = []
    resolution_mismatches = []
    import_other_formats = []
    import_same_format_higher_res = []

    # Separate counters for meaningful summary (each photo counted once in primary category)
    relink_best_count = 0
    relink_alt_count = 0
    resolution_match_count = 0
    resolution_alt_count = 0
    import_other_formats_primary_count = 0

    total = len(missing_photos_df)
    print(f"Processing {total} rows...\n", file=sys.stderr)

    last_completed_row = skip_rows  # absolute CSV data row of the last finished row

    try:
        for i, (_, row) in enumerate(missing_photos_df.iterrows(), 1):
            original_path = row['Photo']
            if exclude_targets and any(excl in original_path for excl in exclude_targets):
                continue

            filename = Path(original_path).name
            stem = Path(filename).stem.lower()
            target_ext = Path(filename).suffix.lower()

            if use_mdfind:
                candidates = find_candidates_mdfind(stem, search_root, exclude_sources)
            else:
                candidates = file_index.get(stem, [])

            if debug:
                print(f"\n[DEBUG] Row {i}: {original_path}", file=sys.stderr)
                print(f"  stem={stem}, candidates found={len(candidates)}", file=sys.stderr)

            if not candidates:
                if debug:
                    print(f"  → no candidates found", file=sys.stderr)
                still_missing.append(row)
                last_completed_row = skip_rows + i
                continue

            if pd.isna(row.get("Date/Time Original (Capture)")) or pd.isna(row.get("Width")) or pd.isna(row.get("Height")):
                if debug:
                    print(f"  → missing metadata in CSV row (date/width/height)", file=sys.stderr)
                still_missing.append(row)
                last_completed_row = skip_rows + i
                continue

            target_time = parse_datetime(row['Date/Time Original (Capture)'])
            if not target_time:
                if debug:
                    print(f"  → could not parse target datetime: {row.get('Date/Time Original (Capture)')}", file=sys.stderr)
                still_missing.append(row)
                last_completed_row = skip_rows + i
                continue

            csv_camera = str(row.get('Camera Make') or '').strip().lower()
            target_w = int(row['Width'])
            target_h = int(row['Height'])

            def score(candidate, _target_time=target_time, _csv_camera=csv_camera):
                meta = get_exif_data_exiftool(candidate)
                if not meta:
                    if debug:
                        print(f"    [DEBUG] {candidate}: exiftool returned no data", file=sys.stderr)
                    return None
                cand_time = parse_datetime(meta['DateTime'])
                if not cand_time:
                    if debug:
                        print(f"    [DEBUG] {candidate}: could not parse candidate datetime: {meta['DateTime']}", file=sys.stderr)
                    return None
                target_time_naive = _target_time.replace(tzinfo=None)
                cand_time_naive = cand_time.replace(tzinfo=None)
                time_diff = abs(cand_time_naive - target_time_naive)
                tz_adjusted = False
                if time_diff > TIME_DELTA:
                    if allow_timezone_mismatches and timezone_offset_match(time_diff):
                        tz_adjusted = True
                        if debug:
                            total_mins = int(time_diff.total_seconds() / 60)
                            print(f"    [DEBUG] {candidate}: time diff {time_diff} accepted as timezone offset (~{total_mins} min)", file=sys.stderr)
                    else:
                        if debug:
                            print(f"    [DEBUG] {candidate}: time diff {time_diff} exceeds limit ({_target_time} vs {cand_time})", file=sys.stderr)
                        return None
                file_camera = meta['Camera Make'].strip().lower()
                camera_ok = not _csv_camera or not file_camera or _csv_camera in file_camera or file_camera in _csv_camera
                if not camera_ok:
                    if debug:
                        print(f"    [DEBUG] {candidate}: camera mismatch (csv={_csv_camera!r}, file={file_camera!r})", file=sys.stderr)
                    return None
                if debug:
                    tz_note = " (timezone-adjusted)" if tz_adjusted else ""
                    print(f"    [DEBUG] {candidate}: PASS{tz_note} — time_diff={time_diff}, camera={file_camera!r}, size={meta['Width']}x{meta['Height']}", file=sys.stderr)
                return {
                    'path': candidate,
                    'meta': meta,
                    'ext': Path(candidate).suffix.lower(),
                    'raw': is_raw_file(candidate),
                    'tz_adjusted': tz_adjusted,
                    'camera_score': 2 if _csv_camera and file_camera and _csv_camera == file_camera
                                    else 1 if _csv_camera in file_camera or file_camera in _csv_camera
                                    else 0,
                    'resolution': meta['Width'] * meta['Height']
                }

            scored = list(filter(None, (score(c) for c in candidates)))

            same_type_sorted = sorted(
                [s for s in scored if s['ext'] == target_ext],
                key=sort_key
            )
            other_type_sorted = sorted(
                [s for s in scored if s['ext'] != target_ext],
                key=sort_key
            )

            exact_matches = [s for s in same_type_sorted if s['meta']['Width'] == target_w and s['meta']['Height'] == target_h]
            emitted_resolution_mismatch = False
            decision_made = False

            if debug:
                print(
                    f"  scored={len(scored)}, same_type={len(same_type_sorted)}, "
                    f"other_type={len(other_type_sorted)}, exact_matches={len(exact_matches)}",
                    file=sys.stderr
                )

            if len(exact_matches) == 1:
                cmd = make_link_or_copy_command(exact_matches[0]['path'], original_path, copy_across_volumes)
                relink_commands.append(cmd)
                relink_best_count += 1
                decision_made = True
                # Check for same-extension candidates with higher resolution than the exact match
                best_res = exact_matches[0]['resolution']
                for rank, candidate in enumerate(
                    s for s in same_type_sorted if s['resolution'] > best_res
                ):
                    import_same_format_higher_res.append({
                        "missing_file": original_path,
                        "matched_file": str(exact_matches[0]['path']),
                        "new_file": str(candidate["path"]),
                        "lr_width": target_w,
                        "lr_height": target_h,
                        "matched_width": exact_matches[0]['meta']['Width'],
                        "matched_height": exact_matches[0]['meta']['Height'],
                        "new_width": candidate["meta"]["Width"],
                        "new_height": candidate["meta"]["Height"],
                        "rank": rank,
                    })
            elif len(exact_matches) > 1:
                sorted_matches = sorted(exact_matches, key=sort_key)
                best = sorted_matches[0]
                cmd = make_link_or_copy_command(best['path'], original_path, copy_across_volumes)
                relink_commands.append(
                    f'# Selected best match from {len(sorted_matches)} candidates: {format_candidate_meta(best)}'
                )
                relink_commands.append(cmd)
                relink_best_count += 1
                for alt in sorted_matches[1:]:
                    relink_commands.append(f'# Alt: {alt["path"]} ({alt["meta"]["Width"]}x{alt["meta"]["Height"]}, {alt["meta"]["Camera Make"]})')
                    relink_alt_count += 1
                decision_made = True
                # Check for same-extension candidates with higher resolution than the best exact match
                best_res = best['resolution']
                for rank, candidate in enumerate(
                    s for s in same_type_sorted if s['resolution'] > best_res
                ):
                    import_same_format_higher_res.append({
                        "missing_file": original_path,
                        "matched_file": str(best['path']),
                        "new_file": str(candidate["path"]),
                        "lr_width": target_w,
                        "lr_height": target_h,
                        "matched_width": best['meta']['Width'],
                        "matched_height": best['meta']['Height'],
                        "new_width": candidate["meta"]["Width"],
                        "new_height": candidate["meta"]["Height"],
                        "rank": rank,
                    })
            elif same_type_sorted:
                best = same_type_sorted[0]
                cmd = make_link_or_copy_command(best['path'], original_path, copy_across_volumes)
                resolution_mismatches.append(
                    f'# Resolution mismatch (LR:{target_w}x{target_h}): {original_path} -> {format_candidate_meta(best)}'
                )
                resolution_mismatches.append(cmd)
                emitted_resolution_mismatch = True
                resolution_match_count += 1
                decision_made = True
                for alt in same_type_sorted[1:]:
                    resolution_mismatches.append(f'# Alt: {alt["path"]} ({alt["meta"]["Width"]}x{alt["meta"]["Height"]}, {alt["meta"]["Camera Make"]})')
                    resolution_alt_count += 1
                higher_res_other_formats = [
                    s for s in other_type_sorted
                    if s['resolution'] > best['resolution']
                ]
                for rank, candidate in enumerate(higher_res_other_formats):
                    import_other_formats.append({
                        "missing_file": original_path,
                        "new_file": str(candidate["path"]),
                        "missing_width": target_w,
                        "missing_height": target_h,
                        "new_width": candidate["meta"]["Width"],
                        "new_height": candidate["meta"]["Height"],
                        "rank": rank,
                    })
            elif other_type_sorted:
                for rank, candidate in enumerate(other_type_sorted):
                    import_other_formats.append({
                        "missing_file": original_path,
                        "new_file": str(candidate["path"]),
                        "missing_width": target_w,
                        "missing_height": target_h,
                        "new_width": candidate["meta"]["Width"],
                        "new_height": candidate["meta"]["Height"],
                        "rank": rank,
                    })
                import_other_formats_primary_count += 1
                decision_made = True

            # Defensive guard: if same-type candidates exist and no exact match exists,
            # resolution mismatch output must not be skipped.
            if same_type_sorted and not exact_matches and not emitted_resolution_mismatch:
                best = same_type_sorted[0]
                cmd = make_link_or_copy_command(best['path'], original_path, copy_across_volumes)
                resolution_mismatches.append(
                    f'# Resolution mismatch (LR:{target_w}x{target_h}): {original_path} -> {format_candidate_meta(best)}'
                )
                resolution_mismatches.append(cmd)
                resolution_match_count += 1
                for alt in same_type_sorted[1:]:
                    resolution_mismatches.append(f'# Alt: {alt["path"]} ({alt["meta"]["Width"]}x{alt["meta"]["Height"]}, {alt["meta"]["Camera Make"]})')
                    resolution_alt_count += 1
                decision_made = True
                if debug:
                    print("  [DEBUG] Fallback guard emitted resolution mismatch entry.", file=sys.stderr)
            if not decision_made:
                if debug:
                    print(f"  → no scored candidates passed filters", file=sys.stderr)
                still_missing.append(row)

            last_completed_row = skip_rows + i
            if i % 100 == 0 or i == total:
                print(f"Processed {i}/{total} rows...", file=sys.stderr)

    except KeyboardInterrupt:
        _print_interrupt_resume(csv_filename, last_completed_row, skip_rows,
                                rows_to_process, output_dir, sys.argv)
        sys.exit(130)

    # Write relink_good_matches.sh
    with open_output(relink_path, append_outputs) as f:
        for cmd in relink_commands:
            f.write(cmd + "\n")

    # Write resolution_mismatch.sh
    with open_output(mismatch_path, append_outputs) as f:
        for line in resolution_mismatches:
            f.write(line + "\n")

    # Write Still_Missing_Photos.csv
    still_missing_df = pd.DataFrame(still_missing)
    if append_outputs and still_missing_path.exists():
        still_missing_df.to_csv(still_missing_path, mode="a", index=False, header=False)
    else:
        still_missing_df.to_csv(still_missing_path, index=False)

    # Write import_other_formats.csv
    import_other_formats_df = pd.DataFrame(
        import_other_formats,
        columns=["missing_file", "new_file", "missing_width", "missing_height", "new_width", "new_height", "rank"],
    )
    if append_outputs and import_other_formats_path.exists():
        import_other_formats_df.to_csv(import_other_formats_path, mode="a", index=False, header=False)
    else:
        import_other_formats_df.to_csv(import_other_formats_path, index=False)

    # Write import_same_format_higher_resolution.csv
    import_same_format_higher_res_df = pd.DataFrame(
        import_same_format_higher_res,
        columns=["missing_file", "matched_file", "new_file",
                 "lr_width", "lr_height",
                 "matched_width", "matched_height",
                 "new_width", "new_height", "rank"],
    )
    if append_outputs and import_same_format_higher_res_path.exists():
        import_same_format_higher_res_df.to_csv(import_same_format_higher_res_path, mode="a", index=False, header=False)
    else:
        import_same_format_higher_res_df.to_csv(import_same_format_higher_res_path, index=False)

    print("\nSummary:", file=sys.stderr)
    print(f"  Relink commands (best):            {relink_best_count}", file=sys.stderr)
    print(f"  Relink commands (alternate):       {relink_alt_count}", file=sys.stderr)
    print(f"  Resolution mismatches (match):     {resolution_match_count}", file=sys.stderr)
    print(f"  Resolution mismatches (alternate): {resolution_alt_count}", file=sys.stderr)
    print(f"  Import other formats:              {import_other_formats_primary_count} photos ({len(import_other_formats)} candidates)", file=sys.stderr)
    print(f"  Import higher res same format:     {len(import_same_format_higher_res)}", file=sys.stderr)
    print(f"  Still missing:                     {len(still_missing)}", file=sys.stderr)
    total_primary = relink_best_count + resolution_match_count + import_other_formats_primary_count + len(still_missing)
    print(f"  (Total primary outcomes: {total_primary} / {total})", file=sys.stderr)
    print(f"\nDone. Outputs written to: {out_dir}/")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find and relink missing photos by matching metadata.")
    parser.add_argument("csv_filename", help="Path to the CSV file containing missing photos metadata.")
    parser.add_argument(
        "--search-root",
        default=None,
        help="Root directory to search for candidate photo files. Required unless --mdfind is specified.",
    )
    parser.add_argument(
        "--mdfind",
        action="store_true",
        help="Use macOS Spotlight (mdfind) to find candidates instead of walking the directory tree. "
             "Makes --search-root optional (but it can still be used to constrain the search).",
    )
    parser.add_argument(
        "--copy-across-volumes",
        action="store_true",
        help="When a candidate is on a different volume than the target, emit a 'cp' command instead of 'ln'.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where output files will be written. Defaults to current directory.",
    )
    parser.add_argument(
        "--skip-rows",
        type=int,
        default=0,
        help="Skip this many rows from the top of the CSV before processing.",
    )
    parser.add_argument(
        "--rows-to-process",
        type=int,
        default=None,
        help="Process at most this many rows (applied after --skip-rows).",
    )
    parser.add_argument(
        "--append-outputs",
        action="store_true",
        help="Append to existing output files instead of overwriting them.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print full candidate match details and rejection reasons. Auto-enabled when --test-n < 20.",
    )
    parser.add_argument(
        "--allow-timezone-mismatches",
        action="store_true",
        help="Accept candidates whose capture time differs by an even half-hour offset (±30 min granularity) "
             "up to ±26 hours, with a residual within 1 minute. Handles cameras without timezone support.",
    )
    parser.add_argument("--test-n", type=int, help="Run script on a random sample of N rows for testing.")
    parser.add_argument("--exclude-sources", nargs='*', help="Paths to exclude as candidate sources.")
    parser.add_argument("--exclude-targets", nargs='*', help="Paths to exclude from processing as missing targets.")
    args = parser.parse_args()

    # Validate: search-root required unless --mdfind
    if not args.mdfind and args.search_root is None:
        parser.error("--search-root is required unless --mdfind is specified.")

    # Auto-enable debug for small test runs
    debug = args.debug or (args.test_n is not None and args.test_n < 20)

    main(
        csv_filename=args.csv_filename,
        search_root=args.search_root,
        test_n=args.test_n,
        exclude_sources=args.exclude_sources,
        exclude_targets=args.exclude_targets,
        use_mdfind=args.mdfind,
        copy_across_volumes=args.copy_across_volumes,
        output_dir=args.output_dir,
        skip_rows=args.skip_rows,
        rows_to_process=args.rows_to_process,
        append_outputs=args.append_outputs,
        debug=debug,
        allow_timezone_mismatches=args.allow_timezone_mismatches,
    )
