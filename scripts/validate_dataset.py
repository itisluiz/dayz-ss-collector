#!/usr/bin/env python3
"""
validate_dataset.py — Validate the captured screenshot dataset against locations.json.

For every location index checks:
  - Both ss-<N>.png and ss-<N>.json are present
  - JSON is parseable
  - locationIndex field matches the filename index
  - Position matches the location entry  (threshold: --pos-threshold, default 10 m)
  - Camera direction matches the location (threshold: --cam-threshold, default 10 °)

Only issues are printed per-file. A compact summary is always shown at the end.

Usage:
    python scripts/validate_dataset.py [--pos-threshold M] [--cam-threshold DEG]
"""

import argparse
import json
import math
import sys
from pathlib import Path

SCRIPTS_DIR   = Path(__file__).parent
SETTINGS_PATH = SCRIPTS_DIR / "settings.json"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        sys.exit(f"ERROR: settings.json not found at {SETTINGS_PATH}")
    with open(SETTINGS_PATH) as f:
        return json.load(f)


# ── geometry helpers (same convention as collect.py / SSCNavigator.c) ────────

def _pos_dist(meta: dict, loc: dict) -> float | None:
    mp = meta.get("position") or {}
    lp = loc.get("position") or {}
    if not mp or not lp:
        return None
    dx = mp.get("x", 0.0) - lp.get("x", 0.0)
    dy = mp.get("y", 0.0) - lp.get("y", 0.0)
    dz = mp.get("z", 0.0) - lp.get("z", 0.0)
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def _cam_angle_deg(meta: dict, loc: dict) -> float | None:
    """Angle in degrees between the recorded cameraDirection and the expected direction."""
    cd = meta.get("cameraDirection") or {}
    if not cd:
        return None

    # Reconstruct expected unit vector from yaw/pitch (DayZ convention, see SSCNavigator.c)
    yaw_r   = math.radians(loc.get("cameraYaw",   0.0))
    pitch_r = math.radians(loc.get("cameraPitch", 0.0))
    ex = math.sin(yaw_r) * math.cos(pitch_r)
    ey = math.sin(pitch_r)
    ez = math.cos(yaw_r) * math.cos(pitch_r)

    ax, ay, az = cd.get("x", 0.0), cd.get("y", 0.0), cd.get("z", 0.0)
    a_len = math.sqrt(ax*ax + ay*ay + az*az)
    if a_len < 1e-6:
        return None
    ax /= a_len; ay /= a_len; az /= a_len

    dot = max(-1.0, min(1.0, ax*ex + ay*ey + az*ez))
    return math.degrees(math.acos(dot))


# ── compact range printer ─────────────────────────────────────────────────────

def _fmt_ranges(indices: list[int]) -> str:
    if not indices:
        return ""
    segs = []
    s = e = indices[0]
    for i in indices[1:]:
        if i == e + 1:
            e = i
        else:
            segs.append(str(s) if s == e else f"{s}–{e}")
            s = e = i
    segs.append(str(s) if s == e else f"{s}–{e}")
    return ", ".join(segs)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate captured dataset against locations.json.")
    parser.add_argument("--pos-threshold", type=float, default=10.0,
                        help="Position mismatch threshold in metres (default 10).")
    parser.add_argument("--cam-threshold", type=float, default=10.0,
                        help="Camera direction mismatch threshold in degrees (default 10).")
    args = parser.parse_args()

    settings     = load_settings()
    base         = Path(settings["logs_dir"]) / "SSCollector"
    loc_path     = base / "locations.json"
    output_path  = base / "output"

    if not loc_path.exists():
        sys.exit(f"ERROR: locations.json not found at {loc_path}")
    if not output_path.exists():
        sys.exit(f"ERROR: output directory not found at {output_path}")

    with open(loc_path) as f:
        locations = json.load(f).get("locations", [])
    total = len(locations)
    print(f"Locations : {total}")
    print(f"Output    : {output_path}\n")

    counts = dict(ok=0, fully_missing=0, no_png=0, no_json=0,
                  json_error=0, index_mismatch=0, pos_mismatch=0, cam_mismatch=0)

    missing_indices = []   # both files absent
    issues          = []   # (idx, description)  — at least one file present but wrong

    for idx in range(total):
        png_path  = output_path / f"ss-{idx}.png"
        json_path = output_path / f"ss-{idx}.json"
        has_png   = png_path.exists()
        has_json  = json_path.exists()

        if not has_png and not has_json:
            counts["fully_missing"] += 1
            missing_indices.append(idx)
            continue

        if not has_png:
            counts["no_png"] += 1
            issues.append((idx, "missing PNG (JSON present)"))
            continue

        if not has_json:
            counts["no_json"] += 1
            issues.append((idx, "missing JSON (PNG present)"))
            continue

        # Both files exist — validate JSON content.
        try:
            with open(json_path) as f:
                data = json.load(f)
        except Exception as e:
            counts["json_error"] += 1
            issues.append((idx, f"JSON parse error: {e}"))
            continue

        loc        = locations[idx]
        file_issues = []

        # locationIndex field
        stored = data.get("locationIndex")
        if stored is None:
            file_issues.append("no locationIndex field")
            counts["index_mismatch"] += 1
        elif stored != idx:
            file_issues.append(f"locationIndex={stored} (expected {idx})")
            counts["index_mismatch"] += 1

        # Position
        d = _pos_dist(data, loc)
        if d is None:
            file_issues.append("missing position field")
        elif d > args.pos_threshold:
            file_issues.append(f"pos Δ {d:.2f}m")
            counts["pos_mismatch"] += 1

        # Camera direction
        a = _cam_angle_deg(data, loc)
        if a is None:
            file_issues.append("missing cameraDirection field")
        elif a > args.cam_threshold:
            file_issues.append(f"cam Δ {a:.1f}°")
            counts["cam_mismatch"] += 1

        if file_issues:
            issues.append((idx, ", ".join(file_issues)))
        else:
            counts["ok"] += 1

    # ── Report ────────────────────────────────────────────────────────────────
    if missing_indices:
        print(f"MISSING ({counts['fully_missing']} indices):")
        # Print up to 20 range segments per line for readability
        segs = _fmt_ranges(missing_indices).split(", ")
        line = []
        for seg in segs:
            line.append(seg)
            if len(line) == 20:
                print("  " + ", ".join(line))
                line = []
        if line:
            print("  " + ", ".join(line))
        print()

    if issues:
        print(f"ISSUES ({len(issues)}):")
        for idx, desc in issues:
            print(f"  [{idx}] {desc}")
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    total_bad = (counts["fully_missing"] + counts["no_png"] + counts["no_json"] +
                 counts["json_error"] + counts["index_mismatch"] +
                 counts["pos_mismatch"] + counts["cam_mismatch"])

    print("── Summary ──────────────────────────────────────────────────────────────")
    print(f"  OK              : {counts['ok']} / {total}")
    if total_bad:
        print(f"  Fully missing   : {counts['fully_missing']}")
        if counts["no_png"]:         print(f"  Missing PNG     : {counts['no_png']}")
        if counts["no_json"]:        print(f"  Missing JSON    : {counts['no_json']}")
        if counts["json_error"]:     print(f"  JSON errors     : {counts['json_error']}")
        if counts["index_mismatch"]: print(f"  Index mismatch  : {counts['index_mismatch']}")
        if counts["pos_mismatch"]:   print(f"  Pos mismatch    : {counts['pos_mismatch']}  (>{args.pos_threshold:.0f}m)")
        if counts["cam_mismatch"]:   print(f"  Cam mismatch    : {counts['cam_mismatch']}  (>{args.cam_threshold:.0f}°)")
    else:
        print("  No issues found.")


if __name__ == "__main__":
    main()
