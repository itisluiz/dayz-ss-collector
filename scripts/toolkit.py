#!/usr/bin/env python3
# DayZ dev toolkit — GUI wrapper for all dev-scripts.

import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk

ROOT_DIR = Path(__file__).parent
SCRIPTS_DIR = ROOT_DIR

sys.path.insert(0, str(SCRIPTS_DIR))
from _common import find_logs, latest_log, load_settings  # type: ignore  # noqa: E402


_PROGRESS_RE = re.compile(r'\[(\d+)/(\d+)\]')


class ETATracker:
    """Tracks progress lines like [N/M] and estimates time-to-completion."""

    _MAX_SAMPLES = 20

    def __init__(self):
        self._samples: list[tuple[int, float]] = []  # (step, monotonic_time)
        self._total: int | None = None

    def update(self, step: int, total: int):
        self._total = total
        now = time.monotonic()
        self._samples.append((step, now))
        if len(self._samples) > self._MAX_SAMPLES:
            self._samples.pop(0)

    def reset(self):
        self._samples.clear()
        self._total = None

    def eta_str(self) -> str:
        if len(self._samples) < 2 or self._total is None:
            return ""
        first_step, first_time = self._samples[0]
        last_step,  last_time  = self._samples[-1]
        steps_done = last_step - first_step
        if steps_done <= 0:
            return ""
        rate = (last_time - first_time) / steps_done  # seconds per step
        remaining = self._total - last_step - 1
        if remaining <= 0:
            return "ETA: done"
        eta_secs = int(rate * remaining)
        return f"ETA: {timedelta(seconds=eta_secs)}"


class FollowWorker:
    # Follows a DayZ log in a background thread with a watchdog for new log files.

    WATCHDOG_INTERVAL = 2.0  # seconds between checks for a newer log

    def __init__(self, kind: str, prefix: str, tag: str, out_queue: queue.Queue):
        self.kind = kind
        self.prefix = prefix
        self.tag = tag
        self._out_queue = out_queue
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _emit(self, text: str):
        self._out_queue.put((self.tag, f"{self.prefix} {text}"))

    def _run(self):
        current = latest_log(self.kind)
        if not current:
            self._out_queue.put(("err", f"No {self.kind} log found.\n"))
            return

        self._emit(f"Now following: {current.name} (from end)\n")

        f = open(current, "r", errors="replace")
        f.seek(0, 2)  # jump to end — skip existing content
        last_watchdog = time.monotonic()
        try:
            while not self._stop.is_set():
                line = f.readline()
                if line:
                    self._emit(line)
                else:
                    if time.monotonic() - last_watchdog >= self.WATCHDOG_INTERVAL:
                        last_watchdog = time.monotonic()
                        logs = find_logs(self.kind)
                        if logs and logs[0] != current:
                            current = logs[0]
                            f.close()
                            f = open(current, "r", errors="replace")
                            self._emit(f"→ New log detected, now following: {current.name}\n")
                    time.sleep(0.1)
        finally:
            f.close()


class FilteredFollowWorker(FollowWorker):
    # FollowWorker that applies a live regex filter from a StringVar.

    def __init__(self, kind: str, prefix: str, tag: str, out_queue: queue.Queue,
                 filter_var: tk.StringVar):
        super().__init__(kind, prefix, tag, out_queue)
        self._filter_var = filter_var

    def _run(self):
        import re
        current = latest_log(self.kind)
        if not current:
            self._out_queue.put(("err", f"No {self.kind} log found.\n"))
            return

        self._emit(f"Now following: {current.name} (from end)\n")

        f = open(current, "r", errors="replace")
        f.seek(0, 2)  # jump to end — skip existing content
        last_watchdog = time.monotonic()
        try:
            while not self._stop.is_set():
                line = f.readline()
                if line:
                    raw = self._filter_var.get().strip()
                    if raw:
                        try:
                            if not re.search(raw, line, re.IGNORECASE):
                                continue
                        except re.error:
                            pass  # invalid regex — show everything
                    self._emit(line)
                else:
                    if time.monotonic() - last_watchdog >= self.WATCHDOG_INTERVAL:
                        last_watchdog = time.monotonic()
                        logs = find_logs(self.kind)
                        if logs and logs[0] != current:
                            current = logs[0]
                            f.close()
                            f = open(current, "r", errors="replace")
                            self._emit(f"→ New log detected, now following: {current.name}\n")
                    time.sleep(0.1)
        finally:
            f.close()


class DevToolkit(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DayZ Dev Toolkit")
        self.geometry("960x580")
        self.minsize(700, 420)
        self._out_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._follow_workers: dict[str, FollowWorker] = {}
        self._eta = ETATracker()
        self._at_line_start = True
        self._build_ui()
        self._poll_output()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ── Top toolbar ──────────────────────────────────────────────────────
        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="ew")

        # Pipeline
        pip = ttk.LabelFrame(top, text="Pipeline", padding=6)
        pip.pack(side="left", padx=(0, 10), fill="y")

        self.var_build  = tk.BooleanVar(value=True)
        self.var_server = tk.BooleanVar(value=True)
        self.var_client = tk.BooleanVar(value=True)

        cb_row = ttk.Frame(pip)
        cb_row.pack(fill="x")
        for col, (text, var) in enumerate([
            ("Build",  self.var_build),
            ("Server", self.var_server),
            ("Client", self.var_client),
        ]):
            ttk.Checkbutton(cb_row, text=text, variable=var).grid(row=0, column=col, padx=(0, 6))

        ttk.Button(pip, text="▶  Run Pipeline", command=self._run_pipeline).pack(fill="x", pady=(6, 0))

        # Launch
        launch = ttk.LabelFrame(top, text="Launch", padding=6)
        launch.pack(side="left", padx=(0, 10), fill="y")
        ttk.Button(launch, text="Server", width=10, command=self._launch_server).pack(fill="x", pady=(0, 4))
        ttk.Button(launch, text="Client", width=10, command=self._launch_client).pack(fill="x")
        self.var_be = tk.BooleanVar(value=False)
        ttk.Checkbutton(launch, text="BattlEye", variable=self.var_be).pack(anchor="w", pady=(4, 0))

        # Kill
        kill = ttk.LabelFrame(top, text="Kill", padding=6)
        kill.pack(side="left", padx=(0, 10), fill="y")
        ttk.Button(kill, text="Kill All",    width=10, command=self._kill_all).pack(fill="x", pady=(0, 4))
        ttk.Button(kill, text="Kill Server", width=10, command=self._kill_server).pack(fill="x", pady=(0, 4))
        ttk.Button(kill, text="Kill Client", width=10, command=self._kill_client).pack(fill="x")

        # Folders
        folders = ttk.LabelFrame(top, text="Folders", padding=6)
        folders.pack(side="left", padx=(0, 10), fill="y")
        ttk.Button(folders, text="DayZ",        width=12, command=lambda: self._open_dir("dayz_dir")).pack(fill="x", pady=(0, 4))
        ttk.Button(folders, text="DayZ Server", width=12, command=lambda: self._open_dir("dayz_server_dir")).pack(fill="x", pady=(0, 4))
        ttk.Button(folders, text="Mod Source",  width=12, command=lambda: self._open_dir("mod_source_dir")).pack(fill="x")

        # Logs
        logs_frame = ttk.LabelFrame(top, text="Logs", padding=6)
        logs_frame.pack(side="left", fill="y")

        self.var_follow_server = tk.BooleanVar(value=False)
        self.var_follow_client = tk.BooleanVar(value=False)

        srv_row = ttk.Frame(logs_frame)
        srv_row.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(srv_row, text="Follow Server", variable=self.var_follow_server,
                        command=self._toggle_follow_server).pack(side="left")
        ttk.Button(srv_row, text="Open", width=5,
                   command=lambda: self._open_log("server")).pack(side="right", padx=(6, 0))

        cli_row = ttk.Frame(logs_frame)
        cli_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(cli_row, text="Follow Client", variable=self.var_follow_client,
                        command=self._toggle_follow_client).pack(side="left")
        ttk.Button(cli_row, text="Open", width=5,
                   command=lambda: self._open_log("client")).pack(side="right", padx=(6, 0))

        filter_row = ttk.Frame(logs_frame)
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self._filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var, width=16, state="disabled")
        self._filter_entry.pack(side="left", padx=(4, 0), fill="x", expand=True)

        self.var_follow_server.trace_add("write", self._on_follow_changed)
        self.var_follow_client.trace_add("write", self._on_follow_changed)

        # ── Output console ───────────────────────────────────────────────────
        out_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        out_frame.grid(row=1, column=0, sticky="nsew")
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)

        self.output = tk.Text(
            out_frame,
            bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white",
            font=("Consolas", 9),
            wrap="word",
            state="disabled",
        )
        self.output.grid(row=0, column=0, sticky="nsew")
        self.output.tag_configure("cmd",    foreground="#569cd6", font=("Consolas", 9, "bold"))
        self.output.tag_configure("ok",     foreground="#608b4e")
        self.output.tag_configure("err",    foreground="#f44747")
        self.output.tag_configure("server", foreground="#4ec9b0")
        self.output.tag_configure("client", foreground="#ce9178")
        self.output.tag_configure("ts",     foreground="#555555")

        sb = ttk.Scrollbar(out_frame, orient="vertical", command=self.output.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.output["yscrollcommand"] = sb.set

        bottom_bar = ttk.Frame(out_frame)
        bottom_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        bottom_bar.columnconfigure(0, weight=1)

        self._eta_label = ttk.Label(bottom_bar, text="", foreground="#808080")
        self._eta_label.grid(row=0, column=0, sticky="w")

        ttk.Button(bottom_bar, text="Clear", command=self._clear_output).grid(
            row=0, column=1, sticky="e"
        )

    def _on_follow_changed(self, *_):
        either_active = self.var_follow_server.get() or self.var_follow_client.get()
        self._filter_entry.configure(state="normal" if either_active else "disabled")

    # --------------------------------------------------------- output helpers -

    MAX_LINES = 5_000

    def _write(self, text: str, tag: str = ""):
        if not text:
            return
        self.output.configure(state="normal")
        # Split on newlines so we can inject a timestamp at the start of each
        # new line of actual content.
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if i > 0:
                self.output.insert("end", "\n")
                self._at_line_start = True
            if seg:
                if self._at_line_start:
                    ts = datetime.now().strftime("%H:%M:%S")
                    self.output.insert("end", f"[{ts}] ", "ts")
                    self._at_line_start = False
                self.output.insert("end", seg, tag or "")
        # Keep the widget bounded so long follow sessions don't bloat memory.
        line_count = int(self.output.index("end-1c").split(".")[0])
        if line_count > self.MAX_LINES:
            self.output.delete("1.0", f"{line_count - self.MAX_LINES}.0")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _clear_output(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def _poll_output(self):
        try:
            while True:
                tag, text = self._out_queue.get_nowait()
                self._write(text, tag)
                m = _PROGRESS_RE.search(text)
                if m:
                    self._eta.update(int(m.group(1)), int(m.group(2)))
                    eta = self._eta.eta_str()
                    self._eta_label.configure(text=eta)
        except queue.Empty:
            pass
        self.after(40, self._poll_output)

    # ---------------------------------------------------------- async runner -

    def _run_async(self, script_path: Path, args: list[str], prefix: str = "", tag: str = ""):
        cmd = [sys.executable, "-u", str(script_path), *[str(a) for a in args]]
        self._out_queue.put(("cmd", f"\n$ {' '.join(cmd)}\n"))
        self._eta.reset()
        self._eta_label.configure(text="")

        def _worker():
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=str(ROOT_DIR),
                )
                for line in proc.stdout:
                    if prefix:
                        self._out_queue.put((tag, f"{prefix} {line}"))
                    else:
                        self._out_queue.put(("", line))
                proc.wait()
                exit_tag = "ok" if proc.returncode == 0 else "err"
                self._out_queue.put((exit_tag, f"[exit {proc.returncode}]\n"))
            except Exception as e:
                self._out_queue.put(("err", f"ERROR: {e}\n"))

        threading.Thread(target=_worker, daemon=True).start()

    # --------------------------------------------------------- follow workers -

    def _toggle_follow_server(self):
        self._toggle_follow("server", self.var_follow_server, "[SERVER]", "server")

    def _toggle_follow_client(self):
        self._toggle_follow("client", self.var_follow_client, "[CLIENT]", "client")

    def _toggle_follow(self, kind: str, var: tk.BooleanVar, prefix: str, tag: str):
        if var.get():
            worker = FilteredFollowWorker(kind, prefix, tag, self._out_queue, self.filter_var)
            self._follow_workers[kind] = worker
            worker.start()
        else:
            worker = self._follow_workers.pop(kind, None)
            if worker:
                worker.stop()
                self._out_queue.put((tag, f"{prefix} Stopped following.\n"))

    def _open_log(self, kind: str):
        log = latest_log(kind)
        if log:
            os.startfile(str(log))
        else:
            self._write(f"No {kind} log found.\n", "err")

    # --------------------------------------------------------------- actions -

    def _run_pipeline(self):
        args = []
        if self.var_build.get():  args.append("-b")
        if self.var_server.get(): args.append("-s")
        if self.var_client.get(): args.append("-c")
        if not any(a in args for a in ("-b", "-s", "-c")):
            self._write("Select at least one of Build / Server / Client.\n", "err")
            return
        if self.var_client.get() and self.var_be.get():
            args.append("--be")
        self._run_async(SCRIPTS_DIR / "pipeline.py", args)

    def _launch_server(self):
        self._run_async(SCRIPTS_DIR / "launch.py", ["--server"])

    def _launch_client(self):
        args = ["--client"]
        if self.var_be.get():
            args.append("--be")
        self._run_async(SCRIPTS_DIR / "launch.py", args)

    def _open_dir(self, key: str):
        s = load_settings()
        path = s.get(key)
        if path and Path(path).exists():
            os.startfile(str(path))
        else:
            self._write(f"Directory not found: {path}\n", "err")

    def _kill_all(self):
        self._run_async(SCRIPTS_DIR / "kill.py", [])

    def _kill_server(self):
        self._run_async(SCRIPTS_DIR / "kill.py", ["--server"])

    def _kill_client(self):
        self._run_async(SCRIPTS_DIR / "kill.py", ["--client"])


if __name__ == "__main__":
    app = DevToolkit()
    app.mainloop()
