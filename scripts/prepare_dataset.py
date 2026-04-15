#!/usr/bin/env python3
"""
prepare_dataset.py — Pack SSCollector output into WebDataset tar shards.

Each shard is a standard tar file where files sharing the same stem belong to
one sample (WebDataset convention):
    000042.png / 000042.jpg   — the screenshot
    000042.json               — label + optional full metadata

Label JSON schema (always present):
    {
        "x":    float,   # normalized [0, 1] — primary training target
        "z":    float,   # normalized [0, 1] — primary training target
        "x_m":  float,   # raw meters
        "z_m":  float,   # raw meters
        "map":  str      # e.g. "chernarusplus"
    }

With --full-meta, a "meta" key is appended containing the original JSON
(position y, camera direction, timeOfDay, weather) for future tasks.

Output layout:
    <output-dir>/
        train/  shard-000000.tar  shard-000001.tar  ...
        val/    shard-000000.tar  ...
        test/   shard-000000.tar  ...
        dataset_info.json

Loading example (Python, using the `webdataset` library):
    import webdataset as wds, json
    info   = json.load(open("dataset/dataset_info.json"))
    shards = ["dataset/" + s for s in info["splits"]["train"]["shards"]]
    ds     = wds.WebDataset(shards).decode("rgb").to_tuple("jpg", "json")

Usage:
    python prepare_dataset.py [options]

    # Quick test — 512×512 JPEG, only first 500 samples:
    python prepare_dataset.py --image-size 512 --jpeg --limit 500

    # 16:9 aspect ratio, full dataset:
    python prepare_dataset.py --image-size 640x360 --jpeg --jpeg-quality 90 --full-meta

    # Full dataset, store all metadata for future tasks:
    python prepare_dataset.py --image-size 512 --jpeg --jpeg-quality 90 --full-meta
"""

import argparse
import io
import json
import math
import os
import random
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image
    _PIL = True
except ImportError:
    _PIL = False


# ---------------------------------------------------------------------------
# Settings / discovery
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    path = Path(__file__).parent / "settings.json"
    with open(path) as f:
        return json.load(f)


def discover_samples(output_dir: Path) -> list[dict]:
    """Return sorted list of {index, png, json} for every complete pair."""
    samples = []
    for png in sorted(output_dir.glob("ss-*.png"), key=lambda p: int(p.stem.split("-")[1])):
        j = png.with_suffix(".json")
        if j.exists():
            samples.append({"index": int(png.stem.split("-")[1]), "png": png, "json": j})
    return samples


# ---------------------------------------------------------------------------
# Map bounds
# ---------------------------------------------------------------------------

def compute_bounds(samples: list[dict]) -> dict:
    """Compute per-map coordinate extents by scanning all label JSONs."""
    bounds: dict[str, dict] = {}
    for i, s in enumerate(samples):
        if i % 5000 == 0 and i > 0:
            print(f"  scanning bounds... {i:,}/{len(samples):,}", flush=True)
        with open(s["json"]) as f:
            meta = json.load(f)
        m = meta.get("map", "unknown")
        x, z = meta["position"]["x"], meta["position"]["z"]
        if m not in bounds:
            bounds[m] = {"x_min": x, "x_max": x, "z_min": z, "z_max": z}
        else:
            b = bounds[m]
            if x < b["x_min"]: b["x_min"] = x
            if x > b["x_max"]: b["x_max"] = x
            if z < b["z_min"]: b["z_min"] = z
            if z > b["z_max"]: b["z_max"] = z
    return bounds


def norm(value: float, lo: float, hi: float) -> float:
    span = hi - lo
    return (value - lo) / span if span else 0.0


# ---------------------------------------------------------------------------
# Image size argument
# ---------------------------------------------------------------------------

def parse_image_size(s: str) -> tuple[int, int]:
    """Parse '512' → (512, 512) or '640x360' → (640, 360)."""
    lo = s.lower()
    if "x" in lo:
        w, h = lo.split("x", 1)
        return int(w), int(h)
    n = int(s)
    return n, n


# ---------------------------------------------------------------------------
# Per-sample worker (runs in subprocess when workers > 1)
# ---------------------------------------------------------------------------

def _process_one(job: tuple) -> tuple[str, str, bytes, bytes]:
    """Load, optionally resize/re-encode one sample. Returns (key, img_ext, img_bytes, label_bytes)."""
    s, bounds, image_w, image_h, use_jpeg, jpeg_quality, full_meta = job

    with open(s["json"]) as f:
        meta = json.load(f)

    map_name = meta.get("map", "unknown")
    pos      = meta["position"]
    x_m, z_m = pos["x"], pos["z"]
    b        = bounds.get(map_name, bounds[next(iter(bounds))])
    x_n      = norm(x_m, b["x_min"], b["x_max"])
    z_n      = norm(z_m, b["z_min"], b["z_max"])

    key = f"{s['index']:06d}"

    label: dict = {"x": x_n, "z": z_n, "x_m": x_m, "z_m": z_m, "map": map_name}
    if full_meta:
        label["meta"] = meta
    label_bytes = json.dumps(label, separators=(",", ":")).encode()

    try:
        from PIL import Image as _Image
        img = _Image.open(s["png"])
        if image_w and image_h:
            img = img.resize((image_w, image_h), _Image.LANCZOS)
        buf = io.BytesIO()
        if use_jpeg:
            img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality)
            img_ext = "jpg"
        else:
            img.save(buf, format="PNG")
            img_ext = "png"
        img_bytes = buf.getvalue()
    except ImportError:
        img_bytes = Path(s["png"]).read_bytes()
        img_ext   = "png"

    return key, img_ext, img_bytes, label_bytes


# ---------------------------------------------------------------------------
# Shard writing
# ---------------------------------------------------------------------------

def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def write_split(
    samples: list[dict],
    split_name: str,
    output_dir: Path,
    bounds: dict,
    args: argparse.Namespace,
    workers: int,
) -> tuple[list[str], int]:
    """Write all shards for one split. Returns (shard_rel_paths, total_count)."""
    if not samples:
        return [], 0

    split_dir = output_dir / split_name
    split_dir.mkdir(parents=True, exist_ok=True)

    image_w, image_h = args.image_size if args.image_size else (0, 0)
    jobs = [
        (s, bounds, image_w, image_h, args.jpeg, args.jpeg_quality, args.full_meta)
        for s in samples
    ]

    shard_idx    = 0
    written      = 0
    in_shard     = 0
    shard_paths: list[str] = []
    tar: tarfile.TarFile | None = None
    cur_path: Path | None = None
    total = len(samples)

    def open_shard() -> None:
        nonlocal tar, cur_path, shard_idx, in_shard
        cur_path = split_dir / f"shard-{shard_idx:06d}.tar"
        tar = tarfile.open(cur_path, "w")
        shard_idx += 1
        in_shard = 0

    def close_shard() -> None:
        if tar:
            tar.close()
            rel = str(cur_path.relative_to(output_dir)).replace("\\", "/")
            shard_paths.append(rel)

    def write_result(result: tuple) -> None:
        nonlocal written, in_shard
        key, img_ext, img_bytes, label_bytes = result
        _add_bytes(tar, f"{key}.{img_ext}", img_bytes)
        _add_bytes(tar, f"{key}.json",      label_bytes)
        in_shard += 1
        written  += 1
        if in_shard >= args.shard_size:
            close_shard()
            if written < total:
                open_shard()
        if written % args.shard_size == 0 or written == total:
            pct = written / total * 100
            print(f"  [{split_name}] {written:>{len(str(total))}}/{total}  "
                  f"({pct:5.1f}%)  shard {shard_idx - 1:,}", flush=True)

    open_shard()

    if workers > 1:
        chunksize = max(1, min(64, len(jobs) // (workers * 4)))
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for result in executor.map(_process_one, jobs, chunksize=chunksize):
                write_result(result)
    else:
        for job in jobs:
            write_result(_process_one(job))

    close_shard()
    return shard_paths, written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pack SSCollector output into WebDataset tar shards.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--output-dir",    type=Path,  default=Path("dataset"),
                    help="Root directory for the output dataset")
    ap.add_argument("--shard-size",    type=int,   default=1000,
                    help="Number of samples per shard tar")
    ap.add_argument("--split",         nargs=3,    type=float, default=[0.8, 0.1, 0.1],
                    metavar=("TRAIN", "VAL", "TEST"),
                    help="Train / val / test fractions (must sum to 1)")
    ap.add_argument("--seed",          type=int,   default=42)
    ap.add_argument("--image-size",    type=parse_image_size, default=None,
                    metavar="N|WxH",
                    help="Resize: '512' → 512×512, '640x360' → 640×360 (omit = keep original)")
    ap.add_argument("--jpeg",          action="store_true",
                    help="Encode images as JPEG instead of PNG (much smaller shards)")
    ap.add_argument("--jpeg-quality",  type=int,   default=90,
                    help="JPEG quality (1-95) when --jpeg is used")
    ap.add_argument("--full-meta",     action="store_true",
                    help="Embed full metadata JSON in each sample for future tasks")
    ap.add_argument("--bounds",        type=Path,  default=None,
                    help="Load pre-computed bounds from a JSON file (skips scanning)")
    ap.add_argument("--limit",         type=int,   default=0,
                    help="Cap total samples (0 = all) — useful for dry runs")
    ap.add_argument("--workers",       type=int,   default=max(1, (os.cpu_count() or 1) - 1),
                    help="Parallel worker processes for image encoding (default: cpu_count-1)")
    args = ap.parse_args()

    if abs(sum(args.split) - 1.0) > 1e-6:
        sys.exit("Error: --split values must sum to 1.0")
    if args.jpeg and not _PIL:
        sys.exit("Error: --jpeg requires Pillow (pip install Pillow)")
    if args.image_size and not _PIL:
        sys.exit("Error: --image-size requires Pillow (pip install Pillow)")

    settings = load_settings()
    src_dir  = Path(settings["logs_dir"]) / "SSCollector" / "output"

    print(f"Scanning {src_dir} ...")
    samples = discover_samples(src_dir)
    if not samples:
        sys.exit("No complete sample pairs found.")
    print(f"Found {len(samples):,} samples")

    if args.limit:
        samples = samples[: args.limit]
        print(f"Capped to {len(samples):,} samples (--limit)")

    # --- bounds ---
    if args.bounds:
        with open(args.bounds) as f:
            bounds = json.load(f)
        print(f"Loaded bounds from {args.bounds}")
    else:
        print("Computing map bounds (scanning all JSONs)...")
        bounds = compute_bounds(samples)

    for m, b in bounds.items():
        print(f"  {m}: x=[{b['x_min']:.0f}m, {b['x_max']:.0f}m]  "
              f"z=[{b['z_min']:.0f}m, {b['z_max']:.0f}m]")

    # --- position-grouped split ---
    random.seed(args.seed)

    pos_groups: dict[tuple, list[dict]] = {}
    for s in samples:
        with open(s["json"]) as f:
            pos = json.load(f)["position"]
        key = (pos["x"], pos["z"])
        pos_groups.setdefault(key, []).append(s)

    group_keys = list(pos_groups.keys())
    random.shuffle(group_keys)

    n_groups  = len(group_keys)
    n_train_g = int(n_groups * args.split[0])
    n_val_g   = int(n_groups * args.split[1])

    def flatten(keys):
        out = []
        for k in keys:
            out.extend(pos_groups[k])
        random.shuffle(out)
        return out

    splits = {
        "train": flatten(group_keys[:n_train_g]),
        "val":   flatten(group_keys[n_train_g: n_train_g + n_val_g]),
        "test":  flatten(group_keys[n_train_g + n_val_g:]),
    }

    n       = len(samples)
    n_train = len(splits["train"])
    n_val   = len(splits["val"])
    n_test  = len(splits["test"])

    img_desc = (f"{args.image_size[0]}×{args.image_size[1]}" if args.image_size else "original")
    fmt_desc = "JPEG" if args.jpeg else "PNG"

    print(f"\nSpatial points: {n_groups:,}  "
          f"→ train: {n_train_g:,}  val: {n_val_g:,}  "
          f"test: {n_groups - n_train_g - n_val_g:,}")
    print(f"Split   — train: {n_train:,}  val: {n_val:,}  test: {n_test:,}")
    print(f"Image   — {img_desc} {fmt_desc}")
    print(f"Shards  — {args.shard_size} samples/shard  "
          f"(~{math.ceil(n_train / args.shard_size)} train shards)")
    print(f"Workers — {args.workers}\n")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    image_size_str = (f"{args.image_size[0]}x{args.image_size[1]}"
                      if args.image_size else "original")
    dataset_info: dict = {
        "created":      datetime.now(timezone.utc).isoformat(),
        "total":        n,
        "seed":         args.seed,
        "image_size":   image_size_str,
        "image_format": "jpeg" if args.jpeg else "png",
        "full_meta":    args.full_meta,
        "bounds":       bounds,
        "splits":       {},
    }

    for split_name, split_samples in splits.items():
        shard_paths, count = write_split(
            split_samples, split_name, args.output_dir, bounds, args, args.workers
        )
        dataset_info["splits"][split_name] = {
            "count":      count,
            "num_shards": len(shard_paths),
            "shards":     shard_paths,
        }

    info_path = args.output_dir / "dataset_info.json"
    with open(info_path, "w") as f:
        json.dump(dataset_info, f, indent=2)

    print(f"\ndataset_info.json → {info_path}")
    print("Done.")


if __name__ == "__main__":
    main()
