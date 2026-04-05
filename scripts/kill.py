#!/usr/bin/env python3
# Kill DayZ processes.
#
# Usage:
#   python kill.py              # kill everything (default)
#   python kill.py --server     # DayZServer_x64.exe only
#   python kill.py --client     # DayZ_BE.exe + DayZ_x64.exe only
#   python kill.py --server --client  # both (same as default)

import argparse
import subprocess
import sys

SERVER_PROCS = ["DayZServer_x64.exe"]
CLIENT_PROCS = ["DayZ_BE.exe", "DayZ_x64.exe"]


def kill(proc: str) -> bool:
    result = subprocess.run(
        ["taskkill", "/F", "/IM", proc],
        capture_output=True, text=True
    )
    success = result.returncode == 0
    if success:
        print(f"  Killed {proc}")
    else:
        # taskkill exits non-zero if the process isn't running — not an error.
        msg = result.stderr.strip() or result.stdout.strip()
        if "not found" in msg.lower() or "no tasks" in msg.lower():
            print(f"  {proc} not running")
        else:
            print(f"  {proc}: {msg}", file=sys.stderr)
    return success


def main():
    parser = argparse.ArgumentParser(description="Kill DayZ processes.")
    parser.add_argument("--server", action="store_true", help="Kill server process")
    parser.add_argument("--client", action="store_true", help="Kill client processes")
    args = parser.parse_args()

    kill_all = not args.server and not args.client

    procs = []
    if args.server or kill_all:
        procs += SERVER_PROCS
    if args.client or kill_all:
        procs += CLIENT_PROCS

    print(f"Killing: {', '.join(procs)}")
    for p in procs:
        kill(p)


if __name__ == "__main__":
    main()
