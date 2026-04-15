#!/usr/bin/env python3
"""
collect.py — Automated screenshot collection for dayz-ss-collector.

Key sending is done via AutoHotkey (reliable with games).
Window focus is never managed by the script — press --hotkey (default HOME)
while DayZ is focused to start or pause the loop.

For each location index the loop:
  1. Sends /ss-goto <index> in DayZ chat via AHK (absolute — no drift).
  2. Waits --delay seconds for lighting to settle.
  3. Presses --meta-key (F13 by default) to trigger metadata capture.
  4. Polls for the new ss-meta-*.json.
  5. Screenshots the DayZ window.
  6. Saves ss-<index>.png and ss-<index>.json.
  7. Saves state so the run can be resumed after any interruption.

Usage:
    python scripts/collect.py [--start-index N] [--delay FLOAT]
                               [--hotkey KEY] [--monitor N] [--disk PATH]

settings.json key:
    ahk_path  — path to AutoHotkey.exe (default: "AutoHotkey.exe" if on PATH)
"""

import argparse
import ctypes
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import math
from datetime import datetime, timedelta
from pathlib import Path

# Enable per-monitor DPI awareness so win32gui returns physical pixel
# coordinates that agree with what mss captures.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    pass

# ── optional deps check ───────────────────────────────────────────────────────
try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:
    sys.exit("ERROR: pywin32 not installed. Run: python -m pip install pywin32")

try:
    import mss
    import mss.tools
except ImportError:
    sys.exit("ERROR: mss not installed. Run: python -m pip install mss")

try:
    from pynput import keyboard
    from pynput.keyboard import Key
except ImportError:
    sys.exit("ERROR: pynput not installed. Run: python -m pip install pynput")

try:
    from colorama import Fore, Style, init as _colorama_init
    _colorama_init()
    _G  = Fore.GREEN
    _Y  = Fore.YELLOW
    _R  = Fore.RED
    _C  = Fore.CYAN
    _M  = Fore.MAGENTA
    _DIM = Style.DIM
    _RST = Style.RESET_ALL
except ImportError:
    sys.exit("ERROR: colorama not installed. Run: python -m pip install colorama")

try:
    import psutil as _psutil
except ImportError:
    sys.exit("ERROR: psutil not installed. Run: python -m pip install psutil")


def _ts() -> str:
    return f"{_DIM}[{datetime.now().strftime('%H:%M:%S')}]{_RST}"


def _delta_str(meta_data: dict, loc: dict) -> str:
    """
    Compare what the meta JSON reports against what locations.json expected.
    Returns a coloured one-liner: 'pos Δ 0.03m  cam Δ 1.2°'
    """
    parts = []

    # ── position (3-D Euclidean distance) ────────────────────────────────────
    mp = meta_data.get("position") or {}
    lp = loc.get("position") or {}
    if mp and lp:
        dx = mp.get("x", 0) - lp.get("x", 0)
        dy = mp.get("y", 0) - lp.get("y", 0)
        dz = mp.get("z", 0) - lp.get("z", 0)
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        col = _G if dist < 1.0 else (_Y if dist < 10.0 else _R)
        parts.append(f"pos Δ {col}{dist:.2f}m{_RST}")

    # ── camera direction (angle between expected and actual unit vectors) ─────
    cd = meta_data.get("cameraDirection") or {}
    if cd:
        # Reconstruct expected direction using DayZ convention (SSCNavigator.c:84):
        #   x = sin(yaw) * cos(pitch),  y = sin(pitch),  z = cos(yaw) * cos(pitch)
        yaw_r   = math.radians(loc.get("cameraYaw",   0.0))
        pitch_r = math.radians(loc.get("cameraPitch", 0.0))
        ex = math.sin(yaw_r) * math.cos(pitch_r)
        ey = math.sin(pitch_r)
        ez = math.cos(yaw_r) * math.cos(pitch_r)

        ax, ay, az = cd.get("x", 0.0), cd.get("y", 0.0), cd.get("z", 0.0)
        a_len = math.sqrt(ax * ax + ay * ay + az * az)
        if a_len > 1e-6:
            ax /= a_len; ay /= a_len; az /= a_len

        dot   = max(-1.0, min(1.0, ax * ex + ay * ey + az * ez))
        angle = math.degrees(math.acos(dot))
        col   = _G if angle < 2.0 else (_Y if angle < 10.0 else _R)
        parts.append(f"cam Δ {col}{angle:.1f}°{_RST}")

    return ("  " + "  ".join(parts)) if parts else ""


class _ETA:
    """Cumulative ETA: total elapsed / steps done × remaining."""

    def __init__(self):
        self._start: float | None = None
        self._done = 0

    def tick(self):
        now = time.monotonic()
        if self._start is None:
            self._start = now
        else:
            self._done += 1

    def format(self, remaining: int) -> str:
        if self._done < 1 or remaining <= 0:
            return ""
        elapsed = time.monotonic() - self._start
        avg = elapsed / self._done
        eta = timedelta(seconds=int(avg * remaining))
        return f"  {_DIM}│  ETA {eta}{_RST}"


# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPTS_DIR   = Path(__file__).parent
SETTINGS_PATH = SCRIPTS_DIR / "settings.json"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        sys.exit(f"ERROR: settings.json not found at {SETTINGS_PATH}")
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def output_dir(settings: dict) -> Path:
    return Path(settings["logs_dir"]) / "SSCollector" / "output"


def locations_path(settings: dict) -> Path:
    return Path(settings["logs_dir"]) / "SSCollector" / "locations.json"


# ── state ─────────────────────────────────────────────────────────────────────
def load_state(state_path: Path) -> dict:
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"next_index": 0}


def save_state(next_index: int, state_path: Path):
    with open(state_path, "w") as f:
        json.dump({"next_index": next_index}, f)


# ── system stats ──────────────────────────────────────────────────────────────
def _stats(disk_path: str | None) -> str:
    parts = []
    if disk_path:
        try:
            du = shutil.disk_usage(disk_path)
            free_gb = du.free / 1_073_741_824
            parts.append(f"{disk_path} {free_gb:.0f}G free")
        except Exception:
            pass
    try:
        vm = _psutil.virtual_memory()
        used_gb  = vm.used  / 1_073_741_824
        total_gb = vm.total / 1_073_741_824
        parts.append(f"RAM {used_gb:.1f}/{total_gb:.0f}G")
    except Exception:
        pass
    return f"  {_DIM}│ " + "  │  ".join(parts) + _RST if parts else ""


# ── window (for screenshot only — never focused programmatically) ─────────────
def find_window_by_process(exe_name: str) -> int:
    """Find the first visible window whose owning process matches exe_name."""
    found = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            hproc = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False, pid)
            exe = win32process.GetModuleFileNameEx(hproc, 0)
            win32api.CloseHandle(hproc)
            if os.path.basename(exe).lower() == exe_name.lower():
                found.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return found[0] if found else 0


# ── ahk ───────────────────────────────────────────────────────────────────────
def send_chat_command(text: str, ahk_exe: str):
    """
    Type and submit a DayZ chat command reliably regardless of current chat state.
    NumpadEnter first ensures chat is closed, Enter opens it, NumpadEnter submits
    (can't open chat — so submit is safe even if timing is slightly off).
    Uses SendEvent mode — bypasses DayZ's SendInput block on game keybinds.
    DayZ window must already be in focus.
    """
    script = f"""\
SendMode "Event"
SetKeyDelay 30, 30
Send "{{NumpadEnter}}"
Sleep 100
Send "{{Enter}}"
Sleep 200
SendText "{text}"
Sleep 50
Send "{{NumpadEnter}}"
Sleep 100
"""
    fd, path = tempfile.mkstemp(suffix=".ahk")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        import subprocess
        subprocess.run([ahk_exe, path], timeout=15)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def send_keystroke(key: str, ahk_exe: str):
    """
    Press a single key or mouse button via AHK (v2 syntax).
    Works for keyboard keys (F13) and mouse extra buttons (XButton1, XButton2).
    DayZ window must already be in focus.
    """
    script = f"""\
SendMode "Event"
SetKeyDelay 30, 30
Send "{{{key}}}"
Sleep 50
"""
    fd, path = tempfile.mkstemp(suffix=".ahk")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        import subprocess
        subprocess.run([ahk_exe, path], timeout=10)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── screenshot ────────────────────────────────────────────────────────────────
def screenshot_window(hwnd: int, dest: Path, monitor_index: int | None = None):
    with mss.mss() as sct:
        if monitor_index is not None:
            monitors = sct.monitors  # [0] = virtual screen, [1+] = real monitors
            if monitor_index < 1 or monitor_index >= len(monitors):
                sys.exit(f"ERROR: --monitor {monitor_index} out of range "
                         f"(available: 1–{len(monitors) - 1})")
            monitor = monitors[monitor_index]
        else:
            rect      = win32gui.GetClientRect(hwnd)
            left, top = win32gui.ClientToScreen(hwnd, (0, 0))
            width     = rect[2] - rect[0]
            height    = rect[3] - rect[1]
            monitor   = {"top": top, "left": left, "width": width, "height": height}

        img = sct.grab(monitor)
        mss.tools.to_png(img.rgb, img.size, output=str(dest))


# ── meta polling ──────────────────────────────────────────────────────────────
def wait_for_meta(out_dir: Path, pre_existing: set, timeout: float = 8.0):
    """
    Wait for any new ss-meta-*.json to appear.
    pre_existing must be snapshotted BEFORE the capture key is pressed.
    Returns (file, data) — caller uses data["locationIndex"] for output naming.
    """
    deadline = time.time() + timeout
    seen     = set(pre_existing)

    while time.time() < deadline:
        for f in out_dir.glob("ss-meta-*.json"):
            if f in seen:
                continue
            try:
                with open(f) as fh:
                    data = json.load(fh)
                return f, data
            except (json.JSONDecodeError, OSError):
                pass  # file still being written — retry
        time.sleep(0.1)

    return None, None


# ── hotkey gate ───────────────────────────────────────────────────────────────
def _parse_hotkey(key_str: str):
    key_str = key_str.lower()
    key_map = {f"f{n}": getattr(Key, f"f{n}") for n in range(1, 21)}
    key_map.update({
        "insert": Key.insert, "home": Key.home, "end": Key.end,
        "page_up": Key.page_up, "page_down": Key.page_down,
        "delete": Key.delete,
        # Numpad keys (Windows VK codes)
        "num_add":   keyboard.KeyCode(vk=107),
        "num_sub":   keyboard.KeyCode(vk=109),
        "num_mul":   keyboard.KeyCode(vk=106),
        "num_div":   keyboard.KeyCode(vk=111),
        "num_enter": keyboard.KeyCode(vk=13),
        "num_0":     keyboard.KeyCode(vk=96),
        "num_1":     keyboard.KeyCode(vk=97),
        "num_2":     keyboard.KeyCode(vk=98),
        "num_3":     keyboard.KeyCode(vk=99),
        "num_4":     keyboard.KeyCode(vk=100),
        "num_5":     keyboard.KeyCode(vk=101),
        "num_6":     keyboard.KeyCode(vk=102),
        "num_7":     keyboard.KeyCode(vk=103),
        "num_8":     keyboard.KeyCode(vk=104),
        "num_9":     keyboard.KeyCode(vk=105),
    })
    return key_map.get(key_str, keyboard.KeyCode.from_char(key_str))


def wait_for_hotkey(hotkey, prompt: str):
    print(prompt, flush=True)
    event = threading.Event()

    def on_press(key):
        if key == hotkey:
            event.set()
            return False

    with keyboard.Listener(on_press=on_press):
        event.wait()


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Automated DayZ screenshot collector.")
    parser.add_argument("--start-index", type=int, default=None)
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds to wait after /ss-goto for lighting to settle (default 1.5).")
    parser.add_argument("--hotkey", type=str, default="home",
                        help="Global hotkey to start/pause the loop (default: home).")
    parser.add_argument("--monitor", type=int, default=None,
                        help="Force capture a specific monitor by index (1-based).")
    parser.add_argument("--meta-key", type=str, default="F13",
                        help="AHK key name bound to 'Capture Meta' in DayZ (default: F13).")
    parser.add_argument("--transition-delay", type=float, default=60.0,
                        help="Extra wait when time-of-day or weather changes between locations (default 60s).")
    parser.add_argument("--disk", type=str, default=None,
                        help="Disk path to monitor free space for (e.g. D:\\).")
    args   = parser.parse_args()
    hotkey = _parse_hotkey(args.hotkey)

    settings   = load_settings()
    out_dir    = output_dir(settings)
    loc_path   = locations_path(settings)
    ahk_exe    = settings.get("ahk_path", "AutoHotkey.exe")
    state_path = out_dir / "collect_state.json"

    # ── verify AHK ───────────────────────────────────────────────────────────
    resolved = shutil.which(ahk_exe) or (ahk_exe if Path(ahk_exe).is_file() else None)
    if not resolved:
        sys.exit(f"ERROR: AutoHotkey not found at '{ahk_exe}'.\n"
                 "Install AutoHotkey or set 'ahk_path' in settings.json.")
    ahk_exe = resolved

    # ── load location count ──────────────────────────────────────────────────
    if not loc_path.exists():
        sys.exit(f"ERROR: locations.json not found at {loc_path}")

    with open(loc_path) as f:
        locs = json.load(f)
    total = len(locs.get("locations", []))
    if total == 0:
        sys.exit("ERROR: locations.json contains no locations.")

    print(f"{_C}Locations:{_RST} {total}  "
          f"{_C}delay:{_RST} {args.delay}s  "
          f"{_C}hotkey:{_RST} {args.hotkey.upper()}  "
          f"{_C}meta-key:{_RST} {args.meta_key}"
          + (f"  {_C}disk:{_RST} {args.disk}" if args.disk else ""))

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── find DayZ window (for screenshots) ───────────────────────────────────
    hwnd = find_window_by_process("DayZ_x64.exe")
    if not hwnd:
        sys.exit("ERROR: No visible window owned by DayZ_x64.exe found.")
    print(f"{_DIM}Window: '{win32gui.GetWindowText(hwnd)}' (hwnd={hwnd}){_RST}")

    # ── determine start index ────────────────────────────────────────────────
    state = load_state(state_path)
    start = args.start_index if args.start_index is not None else state["next_index"]

    if start >= total:
        print(f"{_G}All locations already captured.{_RST}")
        return

    print(f"{_DIM}Starting at index {start} / {total - 1}{_RST}")

    # ── graceful shutdown ────────────────────────────────────────────────────
    shutdown = {"requested": False}
    paused   = {"active": False}

    def _on_signal(sig, frame):
        print(f"\n{_Y}[Interrupted]{_RST} Saving state and exiting...")
        shutdown["requested"] = True

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ── wait for first press, then start the pause/resume listener ───────────
    # Listener starts AFTER wait_for_hotkey so the same keypress doesn't
    # immediately toggle paused=True.
    wait_for_hotkey(hotkey, f"{_C}Focus DayZ and press {args.hotkey.upper()} to start...{_RST}")

    def on_hotkey(key):
        if key == hotkey:
            paused["active"] = not paused["active"]
            if paused["active"]:
                print(f"\n{_ts()} {_M}[Paused]{_RST}   Press {args.hotkey.upper()} to resume."
                      + _stats(args.disk), flush=True)
            else:
                print(f"\n{_ts()} {_G}[Resumed]{_RST}  Press {args.hotkey.upper()} to pause."
                      + _stats(args.disk), flush=True)

    hotkey_listener = keyboard.Listener(on_press=on_hotkey)
    hotkey_listener.start()

    # ── capture loop ─────────────────────────────────────────────────────────
    # Each iteration sends /ss-goto {index} absolutely — no relative navigate
    # keybind, so m_SSCLocIndex can never drift regardless of skips or pauses.
    loc_list = locs.get("locations", [])
    captured       = 0
    skipped        = 0
    eta            = _ETA()
    last_preset    = None   # preset of the last location we actually navigated to

    def _preset_key(loc):
        w = loc.get("weather") or {}
        return (loc.get("timeOfDay"), w.get("overcast"), w.get("fog"), w.get("rain"))

    for index in range(start, total):
        if shutdown["requested"]:
            break

        while paused["active"] and not shutdown["requested"]:
            time.sleep(0.25)

        if shutdown["requested"]:
            break

        png_path  = out_dir / f"ss-{index}.png"
        json_path = out_dir / f"ss-{index}.json"

        if png_path.exists() and json_path.exists():
            skipped += 1
            save_state(index + 1, state_path)
            continue  # next iteration sends its own absolute goto

        # Navigate absolutely to this index, then wait for lighting to settle.
        send_chat_command(f"/ss-goto {index}", ahk_exe)

        delay = args.delay
        if index < len(loc_list):
            cur_preset = _preset_key(loc_list[index])
            if last_preset is not None and cur_preset != last_preset:
                delay = args.transition_delay
                print(f"{_ts()}  {_Y}[transition]{_RST} preset change at {index} "
                      f"— waiting {delay:.0f}s...")
            last_preset = cur_preset
        time.sleep(delay)

        if shutdown["requested"]:
            break

        # Snapshot BEFORE pressing meta key — the server writes the file almost
        # immediately; snapshotting after risks treating it as pre-existing.
        pre_existing = set(out_dir.glob("ss-meta-*.json"))
        send_keystroke(args.meta_key, ahk_exe)

        meta_file, meta_data = wait_for_meta(out_dir, pre_existing)
        if meta_file is None:
            print(f"{_ts()} {_Y}[WARN]{_RST} index={index}: metadata JSON did not appear — skipping.")
            continue

        actual_index = meta_data.get("locationIndex", index)
        if actual_index != index:
            print(f"{_ts()} {_C}[NOTE]{_RST} index={index}: server returned locationIndex={actual_index}.")

        actual_png  = out_dir / f"ss-{actual_index}.png"
        actual_json = out_dir / f"ss-{actual_index}.json"

        deltas = _delta_str(meta_data, loc_list[actual_index]) if actual_index < len(loc_list) else ""

        if actual_png.exists() or actual_json.exists():
            print(f"{_ts()} {_Y}[SKIP]{_RST} locationIndex={actual_index} already exists — discarding duplicate.")
            meta_file.unlink(missing_ok=True)
        else:
            screenshot_window(hwnd, actual_png, args.monitor)
            shutil.move(str(meta_file), str(actual_json))
            captured += 1

        eta.tick()
        save_state(index + 1, state_path)
        print(f"{_ts()} {_G}[{index}/{total - 1}]{_RST} captured  "
              f"{_DIM}(session: {captured}, skipped: {skipped}){_RST}"
              + deltas
              + eta.format(total - index - 1)
              + _stats(args.disk))

    hotkey_listener.stop()

    if not shutdown["requested"]:
        print(f"\n{_G}Done.{_RST} Captured {captured}, skipped {skipped} existing."
              + _stats(args.disk))
    else:
        print(f"{_Y}Stopped.{_RST} Captured {captured} this session."
              + _stats(args.disk))


if __name__ == "__main__":
    main()
