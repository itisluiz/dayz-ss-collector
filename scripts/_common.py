#!/usr/bin/env python3
# Shared utilities for all dev-scripts.

import json
import re
import sys
import time
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent / "settings.json"

_settings = None


def load_settings() -> dict:
    global _settings
    if _settings is not None:
        return _settings

    if not SETTINGS_PATH.exists():
        print(f"ERROR: settings.json not found at {SETTINGS_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(SETTINGS_PATH, "r") as f:
        raw = json.load(f)

    # Resolve all *_dir and *_path keys to Path objects, strip comment keys.
    s = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str) and (k.endswith("_dir") or k.endswith("_path") or k.endswith("_config")):
            s[k] = Path(v)
        else:
            s[k] = v

    _settings = s
    return s


def mod_out_dir() -> Path:
    s = load_settings()
    return s["mod_source_dir"] / "out" / s["mod_name"]


def addon_builder() -> Path:
    s = load_settings()
    return s["dayz_tools_dir"] / "Bin" / "AddonBuilder" / "AddonBuilder.exe"


def find_logs(kind: str | None = None) -> list[Path]:
    # Return script log files sorted newest-first.
    # kind: 'server', 'client', or None (all).
    # Detection is by content, not filename.

    s = load_settings()
    logs_dir = s["logs_dir"]

    candidates = sorted(
        Path(logs_dir).glob("script_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if kind is None:
        return candidates

    matched = []
    for path in candidates:
        t = _detect_log_type(path)
        if t == kind:
            matched.append(path)

    return matched


def _detect_log_type(path: Path) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            head = f.read(4096)
        if "Server mission initialized" in head:
            return "server"
        if "MissionGameplay initialized" in head or "Creating Mission:" in head:
            return "client"
    except OSError:
        pass
    return "unknown"


def latest_log(kind: str | None = None) -> Path | None:
    logs = find_logs(kind)
    return logs[0] if logs else None


def follow_log(path: Path, pattern: re.Pattern | None = None):
    print(f"[Following {path}] Ctrl+C to stop.\n")
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                stripped = line.rstrip()
                if pattern is None or pattern.search(stripped):
                    print(stripped)
            while True:
                line = f.readline()
                if line:
                    stripped = line.rstrip()
                    if pattern is None or pattern.search(stripped):
                        print(stripped, flush=True)
                else:
                    time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n[Stopped]")
