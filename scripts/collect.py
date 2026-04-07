#!/usr/bin/env python3
"""
collect.py — Automated screenshot collection for dayz-ss-collector.

Key sending is done via AutoHotkey (reliable with games).
Window focus is never managed by the script — press --hotkey (default HOME)
while DayZ is focused to start or pause the loop.

For each location index the loop:
  1. Sends /ss-goto <index> in DayZ chat via AHK.
  2. Waits --delay seconds for lighting to settle.
  3. Sends /ss-meta in DayZ chat via AHK.
  4. Polls for the new ss-meta-*.json with the matching locationIndex.
  5. Screenshots the DayZ window client area.
  6. Saves ss-<index>.png and renames the json to ss-<index>.json.
  7. Saves state so the run can be resumed after any interruption.

Usage:
    python scripts/collect.py [--start-index N] [--delay FLOAT]
                               [--hotkey KEY] [--monitor N]

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


def find_window_by_title(substr: str) -> int:
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if substr.lower() in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return found[0] if found else 0


# ── ahk ───────────────────────────────────────────────────────────────────────
def send_chat_command(text: str, ahk_exe: str):
    """
    Open DayZ chat with Enter, type the command, close with Enter.
    Uses SendEvent mode — bypasses DayZ's SendInput block on game keybinds.
    DayZ window must already be in focus.
    """
    # AHK v2 syntax. SendMode "Event" simulates hardware-level keystrokes,
    # which DayZ's input system responds to for its own keybinds (e.g. Enter
    # to open chat). Once chat is open, SendText types the raw string.
    script = f"""\
SendMode "Event"
SetKeyDelay 30, 30
Send "{{Enter}}"
Sleep 200
SendText "{text}"
Sleep 50
Send "{{Enter}}"
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
def wait_for_meta(out_dir: Path, expected_index: int,
                  pre_existing: set, timeout: float = 8.0):
    """
    Poll for a new ss-meta-*.json whose locationIndex == expected_index.
    pre_existing must be snapshotted BEFORE the /ss-meta command is sent,
    otherwise the file may already be written by the time we start polling.
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
                if data.get("locationIndex") == expected_index:
                    return f, data
                seen.add(f)
            except (json.JSONDecodeError, OSError):
                pass
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
        "num_add":      keyboard.KeyCode(vk=107),  # numpad +
        "num_sub":      keyboard.KeyCode(vk=109),  # numpad -
        "num_mul":      keyboard.KeyCode(vk=106),  # numpad *
        "num_div":      keyboard.KeyCode(vk=111),  # numpad /
        "num_enter":    keyboard.KeyCode(vk=13),   # numpad enter
        "num_0":        keyboard.KeyCode(vk=96),
        "num_1":        keyboard.KeyCode(vk=97),
        "num_2":        keyboard.KeyCode(vk=98),
        "num_3":        keyboard.KeyCode(vk=99),
        "num_4":        keyboard.KeyCode(vk=100),
        "num_5":        keyboard.KeyCode(vk=101),
        "num_6":        keyboard.KeyCode(vk=102),
        "num_7":        keyboard.KeyCode(vk=103),
        "num_8":        keyboard.KeyCode(vk=104),
        "num_9":        keyboard.KeyCode(vk=105),
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
                        help="Force capture a specific monitor by index (1-based). "
                             "Use this if window detection picks the wrong monitor.")
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

    print(f"Locations: {total}  |  delay: {args.delay}s  |  hotkey: {args.hotkey.upper()}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── find DayZ window (for screenshots) ───────────────────────────────────
    hwnd = find_window_by_process("DayZ_x64.exe")
    if not hwnd:
        sys.exit("ERROR: No visible window owned by DayZ_x64.exe found.")
    print(f"Window: '{win32gui.GetWindowText(hwnd)}' (hwnd={hwnd})")

    # ── determine start index ────────────────────────────────────────────────
    state = load_state(state_path)
    start = args.start_index if args.start_index is not None else state["next_index"]

    if start >= total:
        print("All locations already captured.")
        return

    print(f"Starting at index {start} / {total - 1}")

    # ── graceful shutdown ────────────────────────────────────────────────────
    shutdown = {"requested": False}
    paused   = {"active": False}

    def _on_signal(sig, frame):
        print("\n[Interrupted] Saving state and exiting...")
        shutdown["requested"] = True

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ── wait for first press, then start the pause/resume listener ───────────
    # Important: listener is started AFTER wait_for_hotkey returns so the
    # same keypress doesn't immediately toggle paused=True.
    wait_for_hotkey(hotkey, f"Focus DayZ and press {args.hotkey.upper()} to start...")

    def on_hotkey(key):
        if key == hotkey:
            paused["active"] = not paused["active"]
            state_str = "Paused" if paused["active"] else "Resumed"
            print(f"\n[{state_str}]  Press {args.hotkey.upper()} to {'resume' if paused['active'] else 'pause'}.")

    hotkey_listener = keyboard.Listener(on_press=on_hotkey)
    hotkey_listener.start()

    # ── startup: exile player + enable freecam ───────────────────────────────
    print("Exiling player...")
    send_chat_command("/ss-exile", ahk_exe)
    time.sleep(0.3)
    print("Enabling freecam...")
    send_chat_command("/ss-freecam", ahk_exe)
    time.sleep(0.3)

    # ── resume jump ──────────────────────────────────────────────────────────
    if start > 0 or args.start_index is not None:
        print(f"Jumping to index {start}...")
        send_chat_command(f"/ss-goto {start}", ahk_exe)
        time.sleep(0.5)

    # ── capture loop ─────────────────────────────────────────────────────────
    captured = 0
    skipped  = 0

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
            continue

        send_chat_command(f"/ss-goto {index}", ahk_exe)
        time.sleep(args.delay)

        if shutdown["requested"]:
            break

        # Snapshot BEFORE sending /ss-meta — the server writes the file during
        # the AHK delay, so snapshotting after would make it look "existing".
        pre_existing = set(out_dir.glob("ss-meta-*.json"))
        send_chat_command("/ss-meta", ahk_exe)

        meta_file, _ = wait_for_meta(out_dir, index, pre_existing)
        if meta_file is None:
            print(f"[WARN] index={index}: metadata JSON did not appear — skipping.")
            continue

        screenshot_window(hwnd, png_path, args.monitor)
        shutil.move(str(meta_file), str(json_path))

        captured += 1
        save_state(index + 1, state_path)
        print(f"[{index}/{total - 1}] captured  (session: {captured}, skipped: {skipped})")

    hotkey_listener.stop()

    if not shutdown["requested"]:
        print(f"\nDone. Captured {captured}, skipped {skipped} existing.")
    else:
        print(f"Stopped. Captured {captured} this session.")


if __name__ == "__main__":
    main()
