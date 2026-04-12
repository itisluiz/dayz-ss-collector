#!/usr/bin/env python3
"""
patch_meta_positions.py — Fix position fields in ss-meta-*.json files.

The original collect run saved the player's (exiled) position instead of the
camera/location position.  Since every meta file has a `locationIndex` field
and locations.json contains the correct position for each index, this script
patches all affected files in-place.

Usage:
    python scripts/patch_meta_positions.py [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
SETTINGS_PATH = SCRIPTS_DIR / "settings.json"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        sys.exit(f"ERROR: settings.json not found at {SETTINGS_PATH}")
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Patch position fields in ss-meta-*.json files.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    settings = load_settings()
    base = Path(settings["logs_dir"]) / "SSCollector"
    locations_path = base / "locations.json"
    output_path = base / "output"

    if not locations_path.exists():
        sys.exit(f"ERROR: locations.json not found at {locations_path}")
    if not output_path.exists():
        sys.exit(f"ERROR: output directory not found at {output_path}")

    with open(locations_path) as f:
        loc_data = json.load(f)
    locations = loc_data.get("locations", [])
    print(f"Loaded {len(locations)} location(s) from {locations_path}")

    meta_files = sorted(output_path.glob("ss-*.json"),
                        key=lambda p: int(p.stem.split("-")[1]) if p.stem != "collect_state" else -1)
    meta_files = [p for p in meta_files if p.stem != "collect_state"]
    print(f"Found {len(meta_files)} meta file(s) to patch")

    patched = skipped = errors = 0

    for path in meta_files:
        try:
            with open(path) as f:
                meta = json.load(f)
        except Exception as e:
            print(f"  [ERROR] {path.name}: {e}")
            errors += 1
            continue

        idx = meta.get("locationIndex")
        if idx is None:
            print(f"  [SKIP]  {path.name}: no locationIndex field")
            skipped += 1
            continue

        if idx < 0 or idx >= len(locations):
            print(f"  [SKIP]  {path.name}: locationIndex={idx} out of range (0–{len(locations)-1})")
            skipped += 1
            continue

        loc = locations[idx]
        loc_pos = loc.get("position")
        if not loc_pos:
            print(f"  [SKIP]  {path.name}: location {idx} has no position")
            skipped += 1
            continue

        old_pos = meta.get("position", {})
        new_pos = {"x": loc_pos["x"], "y": loc_pos["y"], "z": loc_pos["z"]}

        if old_pos == new_pos:
            skipped += 1
            continue

        meta["position"] = new_pos

        if args.dry_run:
            print(f"  [DRY]   {path.name}: idx={idx}  {old_pos} → {new_pos}")
        else:
            with open(path, "w") as f:
                json.dump(meta, f, indent=4)

        patched += 1

    print()
    if args.dry_run:
        print(f"Dry run complete — would patch {patched}, skip {skipped}, error {errors}")
    else:
        print(f"Done — patched {patched}, skipped {skipped}, error {errors}")


if __name__ == "__main__":
    main()
