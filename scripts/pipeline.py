#!/usr/bin/env python3
# Orchestrate the full dev loop: build → kill → server → (wait) → client.
#
# Usage:
#   python pipeline.py -bsc                      # build + server + client (full workflow)
#   python pipeline.py -sc                       # skip build, run both
#   python pipeline.py -bs                       # build + server only
#   python pipeline.py -b                        # build only
#   python pipeline.py -sc --server-delay 10     # wait 10s after log appears before client
#
# Flags:
#   -b / --build           Pack the mod before launching.
#   -s / --server          Launch the server.
#   -c / --client          Launch the client. If --server is also given, waits for
#                          the server log to appear before continuing.
#   --no-kill              Skip killing existing DayZ processes before launching.
#   --server-delay SECS    Extra seconds to wait after the server log appears before
#                          launching the client (default: 0).

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import load_settings
from launch import launch_server, launch_client, wait_for_new_log


def run_build() -> bool:
    script = Path(__file__).parent / "pack.py"
    print("\n=== Building mod ===")
    result = subprocess.run([sys.executable, str(script)])
    if result.returncode != 0:
        print(f"ERROR: Build failed (exit {result.returncode})", file=sys.stderr)
        return False
    return True


def kill_existing(server: bool, client: bool):
    script = Path(__file__).parent / "kill.py"
    cmd = [sys.executable, str(script)]
    if server and not client:
        cmd.append("--server")
    elif client and not server:
        cmd.append("--client")
    # no flag = kill all, which is correct when both are True
    print("\n=== Killing existing processes ===")
    subprocess.run(cmd)
    time.sleep(1)  # let the OS release file handles before we re-launch


def main():
    parser = argparse.ArgumentParser(
        description="Full dev pipeline: build → kill → server → client.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--build",  "-b", action="store_true", help="Pack the mod first")
    parser.add_argument("--server", "-s", action="store_true", help="Launch the server")
    parser.add_argument("--client", "-c", action="store_true", help="Launch the client")
    parser.add_argument("--be", action="store_true", help="Use DayZ_BE.exe instead of DayZ_x64.exe for the client")
    parser.add_argument("--no-kill", action="store_true", help="Don't kill existing processes")
    parser.add_argument(
        "--server-delay", type=float, default=0,
        metavar="SECS",
        help="Extra seconds to wait after server log appears before launching client (default: 0)",
    )
    args = parser.parse_args()

    if not any([args.build, args.server, args.client]):
        parser.error("Specify at least one of: -b/--build, -s/--server, -c/--client")

    s = load_settings()

    # 1. Build
    if args.build:
        if not run_build():
            sys.exit(1)

    if not args.server and not args.client:
        print("\nBuild complete. Nothing to launch.")
        return

    # 2. Kill existing instances
    if not args.no_kill:
        kill_existing(server=args.server, client=args.client)

    launched_at = datetime.now()
    server_log = None

    # 3. Launch server
    if args.server:
        print("\n=== Launching server ===")
        launch_server(s, [])
        print("Waiting for server log file to appear...")
        server_log = wait_for_new_log(launched_at, "server", timeout=20.0)
        if server_log is None:
            # Log appeared but type not yet detectable — grab newest regardless
            server_log = wait_for_new_log(launched_at, None, timeout=5.0)
        if server_log:
            print(f"[pipeline] Server log: {server_log.name}")
        else:
            print("WARNING: No server log appeared within timeout.", file=sys.stderr)

    # 4. Optional delay, then launch client
    if args.client:
        if args.server and args.server_delay > 0:
            print(f"Waiting {args.server_delay:.0f}s before launching client...")
            time.sleep(args.server_delay)
        print("\n=== Launching client ===")
        launch_client(s, [], use_be=args.be)


if __name__ == "__main__":
    main()
