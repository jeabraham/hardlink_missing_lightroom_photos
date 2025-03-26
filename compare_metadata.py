import sys
import subprocess
import pandas as pd
from datetime import datetime, timedelta
from dateutil import parser as dateparser
from datetime import timezone

from pathlib import Path

TIME_DELTA = timedelta(minutes=5)

def load_metadata_from_csv(csv_path, filename):
    df = pd.read_csv(csv_path)
    row = df[df['Photo'].str.contains(filename, case=False, regex=False)]
    if row.empty:
        print(f"❌ {filename} not found in CSV.")
        return None
    row = row.iloc[0]
    return {
        "Camera Make": str(row.get("Camera Make", "")).strip(),
        "Width": int(row.get("Width", 0)),
        "Height": int(row.get("Height", 0)),
        "DateTime": str(row.get("Date/Time Original (Capture)", "")).strip()
    }

def get_metadata_with_exiftool(file_path):
    try:
        result = subprocess.run(["exiftool", "-Make", "-ImageWidth", "-ImageHeight", "-DateTimeOriginal", file_path],
                                capture_output=True, text=True, check=True)
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
        print(f"❌ Error running exiftool: {e.stderr}")
        return None

def parse_datetime(dt_str):
    try:
        dt_str = dt_str.strip()
        if ":" in dt_str[:10]:  # Likely EXIF format
            # Strip sub-seconds and timezone offset if present
            dt_str = dt_str[:19]
            return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        return dateparser.parse(dt_str)
    except Exception as e:
        print(f"⚠️ Failed to parse datetime: {dt_str} ({e})", file=sys.stderr)
        return None


def compare_metadata(csv_meta, file_meta):
    print("🔍 Comparing metadata:")
    all_match = True

    for field in ["Camera Make", "Width", "Height"]:
        v1, v2 = csv_meta[field], file_meta[field]
        print(f"{field}: CSV='{v1}' vs File='{v2}'")
        if str(v1).lower() != str(v2).lower():
            all_match = False

    dt1 = parse_datetime(csv_meta["DateTime"])
    dt2 = parse_datetime(file_meta["DateTime"])

    # If one is naive, strip timezone from both
    if (dt1 and dt2) and (dt1.tzinfo is None or dt2.tzinfo is None):
        dt1 = dt1.replace(tzinfo=None)
        dt2 = dt2.replace(tzinfo=None)    
    print(f"DateTimeOriginal: CSV='{csv_meta['DateTime']}' vs File='{file_meta['DateTime']}'")

    if dt1 and dt2:
        delta = abs(dt1 - dt2)
        print(f"→ Time difference: {delta}")
        if delta > TIME_DELTA:
            print("❌ Time difference exceeds 5 minutes.")
            all_match = False
    else:
        print("❌ Could not parse one or both timestamps.")
        all_match = False

    if all_match:
        print("\n✅ MATCHES according to relinking rules.")
    else:
        print("\n❌ DOES NOT MATCH.")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python compare_with_csv_metadata.py <Missing_Photos.csv> <expected_filename> <found_file_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    expected_filename = Path(sys.argv[2]).name
    candidate_file = sys.argv[3]

    csv_metadata = load_metadata_from_csv(csv_path, expected_filename)
    if not csv_metadata:
        sys.exit(1)

    file_metadata = get_metadata_with_exiftool(candidate_file)
    if not file_metadata:
        sys.exit(1)

    compare_metadata(csv_metadata, file_metadata)
