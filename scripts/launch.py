#!/usr/bin/env python3
# Launch the DayZ server or client.
#
# Usage:
#   python launch.py --server                    # launch server
#   python launch.py --client                    # launch client (DayZ_x64.exe)
#   python launch.py --client --be               # launch client (DayZ_BE.exe)
#   python launch.py --server --follow-log       # launch server + tail its log
#   python launch.py --server --follow-log --search "SSCollector"  # filtered tail
#   python launch.py --server --extra "-doLogs"  # pass extra args to executable

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import load_settings, mod_out_dir, find_logs, follow_log


def wait_for_new_log(since: datetime, kind: str | None, timeout: float = 15.0) -> Path | None:
    # Poll until a new script log appears that was created after `since`.
    deadline = time.time() + timeout
    while time.time() < deadline:
        for log in find_logs(kind):
            if datetime.fromtimestamp(log.stat().st_mtime) >= since:
                return log
        time.sleep(0.5)
    return None


def launch_server(s: dict, extra: list[str]) -> subprocess.Popen:
    exe = Path(s["dayz_server_dir"]) / "DayZServer_x64.exe"
    mod = mod_out_dir()
    args = [
        str(exe),
        f"-config={s['server_config']}",
        f"-mod={mod}",
        *s.get("server_extra_args", []),
        *extra,
    ]
    print(f"Launching server: {' '.join(str(a) for a in args)}")
    return subprocess.Popen(args, cwd=str(s["dayz_server_dir"]))


def launch_client(s: dict, extra: list[str], use_be: bool = False) -> subprocess.Popen:
    binary = "DayZ_BE.exe" if use_be else "DayZ_x64.exe"
    exe = Path(s["dayz_dir"]) / binary
    mod = mod_out_dir()
    args = [
        str(exe),
        f"-connect={s['server_host']}",
        f"-port={s['server_port']}",
        f"-mod={mod}",
        *s.get("client_extra_args", []),
        *extra,
    ]
    print(f"Launching client: {' '.join(str(a) for a in args)}")
    return subprocess.Popen(args, cwd=str(s["dayz_dir"]))


def main():
    parser = argparse.ArgumentParser(description="Launch DayZ server or client.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--server", action="store_true", help="Launch the server")
    group.add_argument("--client", action="store_true", help="Launch the client")
    parser.add_argument("--be", action="store_true", help="Use DayZ_BE.exe instead of DayZ_x64.exe (client only)")
    parser.add_argument("--follow-log", action="store_true", help="Tail the script log after launch")
    parser.add_argument("--search", metavar="PATTERN", help="Filter followed log output (regex)")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="Extra args passed to the executable")
    args = parser.parse_args()

    s = load_settings()
    extra = args.extra or []
    launched_at = datetime.now()

    if args.server:
        proc = launch_server(s, extra)
        kind = "server"
    else:
        proc = launch_client(s, extra, use_be=args.be)
        kind = "client"

    if args.follow_log:
        print("Waiting for log file...")
        log = wait_for_new_log(launched_at, kind)
        if log is None:
            # Fallback: just grab the newest log of any kind
            log = wait_for_new_log(launched_at, None)
        if log:
            pattern = re.compile(args.search, re.IGNORECASE) if args.search else None
            follow_log(log, pattern)
        else:
            print("WARNING: No log file appeared within timeout.", file=sys.stderr)
    else:
        print(f"PID {proc.pid} launched. Use 'python logs.py --{kind} --follow' to tail the log.")


if __name__ == "__main__":
    main()
