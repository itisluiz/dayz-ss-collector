#!/usr/bin/env python3
# Read, search and follow DayZ script logs.
#
# Usage:
#   python logs.py                          # print latest log (auto-detect type)
#   python logs.py --server                 # latest server log
#   python logs.py --client                 # latest client log
#   python logs.py --path                   # print log file path only
#   python logs.py --search "SSCollector"   # regex search (case-insensitive)
#   python logs.py --search "error|warning" # OR search
#   python logs.py --follow                 # tail -f the latest log
#   python logs.py --server --follow        # tail server log
#   python logs.py --server --follow --search "SSCollector"  # filtered tail
#   python logs.py --list                   # list recent logs with detected types

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import find_logs, latest_log, load_settings, follow_log, _detect_log_type


def print_log(path: Path, pattern: re.Pattern | None = None):
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                stripped = line.rstrip()
                if pattern is None or pattern.search(stripped):
                    print(stripped)
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        sys.exit(1)


def list_logs():
    logs = find_logs()[:20]
    if not logs:
        print("No logs found.")
        return
    for p in logs:
        kind = _detect_log_type(p)
        mtime = time.strftime("%H:%M:%S", time.localtime(p.stat().st_mtime))
        print(f"  [{kind:7s}] {mtime}  {p.name}")


def main():
    parser = argparse.ArgumentParser(description="Read and search DayZ script logs.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--server", action="store_true", help="Use latest server log")
    group.add_argument("--client", action="store_true", help="Use latest client log")
    parser.add_argument("--path",   action="store_true", help="Print log file path and exit")
    parser.add_argument("--search", metavar="PATTERN",   help="Case-insensitive regex to filter lines")
    parser.add_argument("--follow", action="store_true", help="Tail the log (like tail -f)")
    parser.add_argument("--list",   action="store_true", help="List recent logs with detected types")
    args = parser.parse_args()

    if args.list:
        list_logs()
        return

    kind = "server" if args.server else "client" if args.client else None
    log = latest_log(kind)

    if log is None:
        what = f"{kind} " if kind else ""
        print(f"No {what}log found in {load_settings()['logs_dir']}", file=sys.stderr)
        sys.exit(1)

    if args.path:
        print(log)
        return

    pattern = re.compile(args.search, re.IGNORECASE) if args.search else None

    if args.follow:
        follow_log(log, pattern)
    else:
        print(f"# {log}\n")
        print_log(log, pattern)


if __name__ == "__main__":
    main()
