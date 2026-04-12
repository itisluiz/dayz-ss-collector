#!/usr/bin/env python3
"""
viewer.py — Interactive map viewer for locations.json

Renders the map + all location points as a single composited PIL image per
frame (much faster than thousands of tkinter canvas primitives).

Each geographic cluster shows one arrow per unique camera-yaw angle, so
six directions at one spot = six arrows.  Overlapping entries that differ
only in time-of-day or weather still count toward the cluster total but
don't add duplicate arrows.

Usage:
    python scripts/viewer.py [--map PATH] [--locations PATH]
    python scripts/viewer.py          (reads paths from settings.json)

Controls:
    Scroll wheel    Zoom in / out (anchored at cursor)
    Left-drag       Pan
    Click           Select group → detail panel
    R               Reset view
    Escape          Deselect
"""

import argparse
import json
import math
import sys
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:
    sys.exit("ERROR: tkinter not available.")

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
except ImportError:
    sys.exit("ERROR: Pillow not installed.  Run: pip install Pillow")

try:
    import numpy as np
except ImportError:
    sys.exit("ERROR: numpy not installed.  Run: pip install numpy")

# ── constants ─────────────────────────────────────────────────────────────────
WORLD_SIZE    = 15360.0
MAP_SRC_MAX   = 2048      # pre-downscale source to this so all resizes are fast
CLUSTER_R     = 14        # grid-bucket radius (screen pixels)
POINT_R       = 5         # base circle radius
ARROW_LEN     = 15        # tip-to-centre distance (extra on top of circle r)
ARROW_HEAD    = 7         # arrowhead length
MAX_YAW_ARROWS = 24       # hard cap on arrows per cluster (avoids starburst soup)
MIN_ZOOM      = 0.2
MAX_ZOOM      = 120.0
YAW_SNAP_DEG  = 1.0       # round yaws to this before deduplication

# ── colours (RGB tuples for PIL) ──────────────────────────────────────────────
C_SINGLE      = (255,  64,  64)
C_SINGLE_SEL  = (  0, 229, 255)
C_SINGLE_OUT  = (180,   0,   0)
C_SINGLE_OUT_SEL = (255, 255, 255)
C_CLUSTER     = (255, 187,   0)
C_CLUSTER_SEL = (  0, 229, 255)
C_CLUSTER_OUT = (220,  90,   0)
C_CLUSTER_OUT_SEL = (255, 255, 255)
C_ARROW       = (255, 140,   0)
C_ARROW_SEL   = (  0, 229, 255)
C_BG          = (17, 17, 17)

# ── PIL font ──────────────────────────────────────────────────────────────────
try:
    _FONT = ImageFont.load_default(size=9)
except TypeError:
    _FONT = ImageFont.load_default()


# ── data helpers ──────────────────────────────────────────────────────────────

def world_to_norm(x: float, z: float):
    """World coords → normalised [0,1] image coords (top-left origin, north=up)."""
    return x / WORLD_SIZE, 1.0 - z / WORLD_SIZE


def load_locations(path: Path) -> list:
    print(f"Loading {path} …", end=" ", flush=True)
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    locs = raw.get("locations", [])
    print(f"{len(locs):,} locations.")
    return locs


# ── viewer ────────────────────────────────────────────────────────────────────

class MapViewer:
    def __init__(self, root: tk.Tk, map_path, locations: list):
        self.root       = root
        self._locations = locations

        # Pre-compute numpy coord arrays
        n    = len(locations)
        nx   = np.empty(n, dtype=np.float32)
        ny   = np.empty(n, dtype=np.float32)
        yaws = np.empty(n, dtype=np.float32)
        for i, loc in enumerate(locations):
            pos     = loc.get("position", {})
            nx[i], ny[i] = world_to_norm(pos.get("x", 0.0), pos.get("z", 0.0))
            yaws[i] = loc.get("cameraYaw", 0.0)
        self._nx   = nx
        self._ny   = ny
        self._yaws = yaws

        # Viewport
        self._zoom  = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

        # Drag
        self._drag_origin = None
        self._drag_moved  = False

        # Selection
        self._selected       = None      # int index into locations
        self._visible_groups = []        # built by _cluster(); used for click/hover

        # Map source (pre-scaled for performance)
        self._map_source: Image.Image | None = None

        # Keep a reference to prevent GC
        self._photo: ImageTk.PhotoImage | None = None

        # Redraw debounce
        self._redraw_id = None

        self._build_ui()

        if map_path and Path(map_path).exists():
            self._load_map(Path(map_path))

        root.after(80, self._fit_view)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.configure(bg="#1a1a1a")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        body = tk.Frame(self.root, bg="#1a1a1a")
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # Canvas — receives ALL mouse events; contains one PhotoImage item
        self._canvas = tk.Canvas(body, bg="#111111", highlightthickness=0,
                                  cursor="crosshair")
        self._canvas.grid(row=0, column=0, sticky="nsew")

        # Side panel
        side = tk.Frame(body, bg="#1a1a1a", width=210)
        side.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)

        self._lbl_header = tk.Label(
            side, text=f"{len(self._locations):,} locations",
            bg="#1a1a1a", fg="#888888", font=("Consolas", 9, "bold"), anchor="w")
        self._lbl_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))

        self._info_box = tk.Text(
            side, bg="#0d0d0d", fg="#cccccc", font=("Consolas", 8),
            relief="flat", bd=0, state="disabled", wrap="word", width=26)
        self._info_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)

        btn_area = tk.Frame(side, bg="#1a1a1a")
        btn_area.grid(row=2, column=0, sticky="ew", padx=4, pady=6)
        for label, cmd in (
            ("Open Map Image…", self._open_map_dialog),
            ("Reset View  [R]", self._fit_view),
        ):
            tk.Button(btn_area, text=label, command=cmd,
                      bg="#2d2d2d", fg="#bbbbbb", relief="flat",
                      activebackground="#3d3d3d", activeforeground="#ffffff",
                      font=("Consolas", 8)).pack(fill="x", pady=2)

        self._status = tk.Label(
            self.root,
            text="Scroll: zoom  |  Drag: pan  |  R: reset  |  Click: select",
            bg="#0d0d0d", fg="#555555", font=("Consolas", 8), anchor="w", padx=6)
        self._status.grid(row=1, column=0, sticky="ew")

        # Bindings
        c = self._canvas
        c.bind("<Configure>",       self._on_resize)
        c.bind("<MouseWheel>",      self._on_scroll)
        c.bind("<Button-4>",        self._on_scroll)
        c.bind("<Button-5>",        self._on_scroll)
        c.bind("<ButtonPress-1>",   self._on_btn_down)
        c.bind("<B1-Motion>",       self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_btn_up)
        c.bind("<Motion>",          self._on_mouse_move)
        self.root.bind("<r>",       lambda _: self._fit_view())
        self.root.bind("<R>",       lambda _: self._fit_view())
        self.root.bind("<Escape>",  lambda _: self._deselect())

    def _open_map_dialog(self):
        p = filedialog.askopenfilename(
            title="Open DayZ map image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tiff *.tif"),
                       ("All files", "*.*")])
        if p:
            self._load_map(Path(p))
            self._fit_view()

    def _load_map(self, path: Path):
        try:
            img = Image.open(path).convert("RGB")
            # Pre-downscale so all future resizes are on a small source
            if img.width > MAP_SRC_MAX or img.height > MAP_SRC_MAX:
                print(f"Pre-scaling {img.width}×{img.height} → {MAP_SRC_MAX}×{MAP_SRC_MAX}…",
                      end=" ", flush=True)
                img = img.resize((MAP_SRC_MAX, MAP_SRC_MAX), Image.LANCZOS)
                print("done.")
            self._map_source = img
        except Exception as exc:
            messagebox.showerror("Cannot open image", str(exc))

    # ── viewport helpers ──────────────────────────────────────────────────────

    @property
    def _cw(self) -> int:
        return self._canvas.winfo_width() or 800

    @property
    def _ch(self) -> int:
        return self._canvas.winfo_height() or 800

    def _fit_view(self):
        self._zoom  = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._schedule_redraw()

    def _map_transform(self):
        """Return (px_size, ox, oy) — full map pixel size and top-left offset on canvas."""
        size = min(self._cw, self._ch) * self._zoom
        ox   = (self._cw - size) / 2 + self._pan_x
        oy   = (self._ch - size) / 2 + self._pan_y
        return size, ox, oy

    def _canvas_to_norm(self, cx, cy):
        size, ox, oy = self._map_transform()
        return (cx - ox) / size, (cy - oy) / size

    def _canvas_to_world(self, cx, cy):
        nx, ny = self._canvas_to_norm(cx, cy)
        return nx * WORLD_SIZE, (1.0 - ny) * WORLD_SIZE

    # ── events ────────────────────────────────────────────────────────────────

    def _on_resize(self, _):
        self._schedule_redraw()

    def _on_scroll(self, event):
        if   event.num == 4:  delta =  1
        elif event.num == 5:  delta = -1
        else:                 delta =  1 if event.delta > 0 else -1

        nx, ny = self._canvas_to_norm(event.x, event.y)

        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * (1.15 ** delta)))

        # Re-anchor cursor position after zoom
        size = min(self._cw, self._ch) * self._zoom
        self._pan_x = event.x - (self._cw - size) / 2 - nx * size
        self._pan_y = event.y - (self._ch - size) / 2 - ny * size

        self._schedule_redraw()

    def _on_btn_down(self, event):
        self._drag_origin = (event.x, event.y, self._pan_x, self._pan_y)
        self._drag_moved  = False

    def _on_drag(self, event):
        if not self._drag_origin:
            return
        sx, sy, px, py = self._drag_origin
        dx, dy = event.x - sx, event.y - sy
        if abs(dx) > 3 or abs(dy) > 3:
            self._drag_moved = True
        self._pan_x = px + dx
        self._pan_y = py + dy
        self._schedule_redraw()

    def _on_btn_up(self, event):
        if self._drag_origin and not self._drag_moved:
            self._handle_click(event.x, event.y)
        self._drag_origin = None

    def _on_mouse_move(self, event):
        wx, wz = self._canvas_to_world(event.x, event.y)
        hover  = self._find_group(event.x, event.y)
        if hover:
            n = len(hover["indices"])
            self._canvas.config(cursor="hand2")
            self._status.config(
                text=f"({wx:.0f}, {wz:.0f})  |  "
                     f"{n} location{'s' if n > 1 else ''} here — click to inspect")
        else:
            self._canvas.config(cursor="crosshair")
            self._status.config(
                text=f"({wx:.0f}, {wz:.0f})  |  "
                     f"Scroll: zoom  Drag: pan  R: reset  Click: select")

    def _handle_click(self, ex: int, ey: int):
        g = self._find_group(ex, ey)
        if g:
            self._selected = g["indices"][0]
            self._show_info(g)
        else:
            self._deselect()
        self._schedule_redraw()

    def _deselect(self):
        self._selected = None
        self._clear_info()
        self._schedule_redraw()

    def _find_group(self, ex: int, ey: int):
        best_d, best_g = CLUSTER_R + 8, None
        for g in self._visible_groups:
            d = math.hypot(ex - g["cx"], ey - g["cy"])
            if d < best_d:
                best_d, best_g = d, g
        return best_g

    # ── info panel ────────────────────────────────────────────────────────────

    def _show_info(self, group: dict):
        indices = group["indices"]
        first   = indices[0]
        loc     = self._locations[first]
        pos     = loc.get("position", {})
        w       = loc.get("weather") or {}

        lines = [
            f"Index    {first}",
            f"X        {pos.get('x', 0):.1f}",
            f"Y        {pos.get('y', 0):.2f}",
            f"Z        {pos.get('z', 0):.1f}",
            "─" * 22,
            f"Yaw      {loc.get('cameraYaw',   0):.1f}°",
            f"Pitch    {loc.get('cameraPitch', 0):.1f}°",
            f"Time     {loc.get('timeOfDay',   0):.2f} h",
        ]
        if w:
            lines += [
                "─" * 22,
                f"Overcast {w.get('overcast', 0):.2f}",
                f"Fog      {w.get('fog',      0):.2f}",
                f"Rain     {w.get('rain',     0):.2f}",
            ]
        if len(indices) > 1:
            lines += ["─" * 22, f"{len(indices)} overlap here"]
            # Show unique yaws
            seen, uniqs = set(), []
            for i in indices:
                y = round(self._locations[i].get("cameraYaw", 0)) % 360
                if y not in seen:
                    seen.add(y)
                    uniqs.append(y)
            yaw_strs = [f"{y}°" for y in uniqs[:20]]
            for row_start in range(0, len(yaw_strs), 4):
                lines.append("  " + "  ".join(yaw_strs[row_start:row_start + 4]))
            if len(uniqs) > 20:
                lines.append(f"  … +{len(uniqs) - 20} more yaws")

        self._lbl_header.config(text=f"Location #{first}")
        self._info_box.config(state="normal")
        self._info_box.delete("1.0", "end")
        self._info_box.insert("end", "\n".join(lines))
        self._info_box.config(state="disabled")

    def _clear_info(self):
        self._lbl_header.config(text=f"{len(self._locations):,} locations")
        self._info_box.config(state="normal")
        self._info_box.delete("1.0", "end")
        self._info_box.config(state="disabled")

    # ── redraw scheduling ─────────────────────────────────────────────────────

    def _schedule_redraw(self):
        if self._redraw_id is not None:
            self.root.after_cancel(self._redraw_id)
        self._redraw_id = self.root.after(12, self._redraw)

    # ── main render ───────────────────────────────────────────────────────────

    def _redraw(self):
        self._redraw_id = None
        cw, ch = self._cw, self._ch
        px_size, ox, oy = self._map_transform()

        # ── 1. Composite image (map + overlay) ────────────────────────────────
        composite = Image.new("RGB", (cw, ch), C_BG)

        if self._map_source:
            src_sz = self._map_source.width   # MAP_SRC_MAX (square)

            # Visible portion of the map in canvas space
            vis_x1 = max(0.0, -ox)
            vis_y1 = max(0.0, -oy)
            vis_x2 = min(px_size, cw - ox)
            vis_y2 = min(px_size, ch - oy)

            if vis_x2 > vis_x1 and vis_y2 > vis_y1:
                # Corresponding source crop
                inv = src_sz / px_size
                crop = self._map_source.crop((
                    max(0, int(vis_x1 * inv)),
                    max(0, int(vis_y1 * inv)),
                    min(src_sz, math.ceil(vis_x2 * inv)),
                    min(src_sz, math.ceil(vis_y2 * inv)),
                ))
                dest_w = int(vis_x2 - vis_x1)
                dest_h = int(vis_y2 - vis_y1)
                if dest_w > 0 and dest_h > 0 and crop.width > 0 and crop.height > 0:
                    rendered = crop.resize((dest_w, dest_h), Image.BILINEAR)
                    composite.paste(rendered, (int(max(0, ox)), int(max(0, oy))))

        # ── 2. Draw point overlay ─────────────────────────────────────────────
        overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)
        self._draw_points(draw, px_size, ox, oy, cw, ch)
        composite = Image.alpha_composite(composite.convert("RGBA"), overlay).convert("RGB")

        # ── 3. Display ────────────────────────────────────────────────────────
        self._photo = ImageTk.PhotoImage(composite)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)

    # ── clustering ────────────────────────────────────────────────────────────

    def _draw_points(self, draw: ImageDraw.Draw,
                     px_size: float, ox: float, oy: float,
                     cw: int, ch: int):
        if len(self._nx) == 0:
            self._visible_groups = []
            return

        # Batch-transform to canvas pixels
        cx_all = (ox + self._nx * px_size).astype(np.float32)
        cy_all = (oy + self._ny * px_size).astype(np.float32)

        # Visibility cull
        margin  = float(CLUSTER_R * 3)
        visible = (
            (cx_all >= -margin) & (cx_all <= cw + margin) &
            (cy_all >= -margin) & (cy_all <= ch + margin)
        )
        if not np.any(visible):
            self._visible_groups = []
            return

        cx_v    = cx_all[visible]
        cy_v    = cy_all[visible]
        yaws_v  = self._yaws[visible]
        idxs_v  = np.where(visible)[0]

        # O(n log n) grid-bucket clustering in screen space
        bx     = np.floor(cx_v / CLUSTER_R).astype(np.int64)
        by     = np.floor(cy_v / CLUSTER_R).astype(np.int64)
        OFFSET = 100_000
        keys   = (bx + OFFSET) * 1_000_000 + (by + OFFSET)
        order  = np.argsort(keys, kind="stable")
        keys_s = keys[order]
        splits = np.flatnonzero(np.diff(keys_s)) + 1
        starts = np.concatenate(([0], splits))
        ends   = np.concatenate((splits, [len(keys_s)]))

        groups = []
        for s, e in zip(starts.tolist(), ends.tolist()):
            seg = order[s:e]
            gx  = float(np.mean(cx_v[seg]))
            gy  = float(np.mean(cy_v[seg]))
            idx = idxs_v[seg].tolist()
            yaw = yaws_v[seg].tolist()
            groups.append({"cx": gx, "cy": gy, "indices": idx, "yaws": yaw})

        self._visible_groups = groups

        for g in groups:
            self._draw_group(draw, g["cx"], g["cy"],
                             len(g["indices"]), g["yaws"],
                             self._selected in g["indices"])

    # ── per-group rendering ───────────────────────────────────────────────────

    def _draw_group(self, draw: ImageDraw.Draw,
                    cx: float, cy: float,
                    count: int, yaws: list,
                    selected: bool):
        r = max(POINT_R, min(int(POINT_R + math.log10(max(count, 1)) * 3.5), 18))

        if selected:
            fill, out = C_CLUSTER_SEL, C_CLUSTER_OUT_SEL
        elif count > 1:
            fill, out = C_CLUSTER, C_CLUSTER_OUT
        else:
            fill, out = C_SINGLE, C_SINGLE_OUT
        arrow_c = C_ARROW_SEL if selected else C_ARROW

        # Circle
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=fill + (220,), outline=out + (255,), width=2)

        # Count label for clusters
        if count > 1:
            txt = str(count)
            try:
                bbox = draw.textbbox((0, 0), txt, font=_FONT)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.text((cx - tw / 2, cy - th / 2), txt,
                           fill=(0, 0, 0, 255), font=_FONT)
            except Exception:
                draw.text((int(cx) - 3, int(cy) - 4), txt, fill=(0, 0, 0, 255))

        # Unique-yaw arrows
        seen: set = set()
        drawn = 0
        for y in yaws:
            key = round(y / YAW_SNAP_DEG) % int(360 / YAW_SNAP_DEG)
            if key in seen:
                continue
            seen.add(key)
            self._draw_arrow(draw, cx, cy, y, r + ARROW_LEN, arrow_c)
            drawn += 1
            if drawn >= MAX_YAW_ARROWS:
                break

    def _draw_arrow(self, draw: ImageDraw.Draw,
                    cx: float, cy: float,
                    yaw_deg: float, length: float,
                    colour: tuple):
        rad   = math.radians(yaw_deg)
        sin_a = math.sin(rad)
        cos_a = math.cos(rad)

        # Screen direction vector: x=sin(yaw), y=-cos(yaw)  (north=up)
        dx, dy = sin_a, -cos_a

        # Tip
        tx = cx + dx * length
        ty = cy + dy * length

        # Arrowhead base (pulled back from tip)
        shaft_end = length - ARROW_HEAD
        bx = cx + dx * shaft_end
        by = cy + dy * shaft_end

        # Perpendicular for head width
        hw  = ARROW_HEAD * 0.55
        px_ = -dy * hw
        py_ =  dx * hw

        col = colour + (230,)
        draw.line([(cx, cy), (bx, by)], fill=col, width=2)
        draw.polygon(
            [(tx, ty), (bx + px_, by + py_), (bx - px_, by - py_)],
            fill=col)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Interactive DayZ location map viewer.")
    ap.add_argument("--map",       type=Path, default=None,
                    help="Path to map image (PNG/JPG/…).")
    ap.add_argument("--locations", type=Path, default=None,
                    help="Path to locations.json (default: from settings.json).")
    args = ap.parse_args()

    if args.locations:
        loc_path = args.locations
    else:
        settings_path = Path(__file__).parent / "settings.json"
        if not settings_path.exists():
            sys.exit("ERROR: settings.json not found — pass --locations explicitly.")
        with open(settings_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        loc_path = Path(cfg["logs_dir"]) / "SSCollector" / "locations.json"

    if not loc_path.exists():
        sys.exit(f"ERROR: {loc_path} not found.")

    locations = load_locations(loc_path)

    root = tk.Tk()
    root.title("DayZ Location Viewer")
    root.geometry("1200x800")
    root.minsize(640, 480)

    MapViewer(root, args.map, locations)
    root.mainloop()


if __name__ == "__main__":
    main()
