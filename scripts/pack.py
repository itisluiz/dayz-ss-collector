#!/usr/bin/env python3
# Pack the mod PBO and deploy it.
#
# Usage:
#   python pack.py              # pack and deploy
#   python pack.py --dry-run    # show what would run without doing it

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import load_settings, addon_builder, mod_out_dir


def main():
    parser = argparse.ArgumentParser(description="Pack and deploy the mod.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = parser.parse_args()

    s = load_settings()
    builder = addon_builder()
    out      = mod_out_dir()
    source   = s["mod_source_dir"] / "Scripts"
    dest     = out / "Addons"
    mod_cpp_src = s["mod_source_dir"] / "mod.cpp"
    mod_cpp_dst = out / "mod.cpp"

    # Validate
    for path, label in [(builder, "AddonBuilder"), (source, "Scripts source"), (mod_cpp_src, "mod.cpp")]:
        if not path.exists():
            print(f"ERROR: {label} not found: {path}", file=sys.stderr)
            sys.exit(1)

    dest.mkdir(parents=True, exist_ok=True)

    # Determine P: project root (parent of mod_source_dir on P:)
    # -project tells AddonBuilder how to compute the PBO prefix.
    source_drive = source.drive or "P:"
    project_root = source_drive + "\\"

    cmd = [
        str(builder),
        str(source),
        str(dest),
        "-packonly",
        "-clear",
        f"-project={project_root}",
    ]

    print(f"Source:  {source}")
    print(f"Deploy:  {dest}")
    print(f"Command: {' '.join(cmd)}")

    if args.dry_run:
        print("[dry-run] Skipping execution.")
        return

    print("\n--- AddonBuilder output ---")
    result = subprocess.run(cmd, text=True, capture_output=False)
    print("---------------------------")

    if result.returncode != 0:
        print(f"ERROR: AddonBuilder exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    # Copy mod.cpp
    shutil.copy2(mod_cpp_src, mod_cpp_dst)
    print(f"Copied mod.cpp -> {mod_cpp_dst}")
    print("Pack complete.")


if __name__ == "__main__":
    main()
