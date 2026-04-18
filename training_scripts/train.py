#!/usr/bin/env python3
"""
DayZ SSCollector — Location Regression Training

Reads from a local dataset directory containing WebDataset-format tar shards.
Use blob_utils.py to pre-download the dataset, or pass --blob-dataset to have
this script download it automatically before training starts.

Quickstart:
  python train.py --dataset /tmp/dataset --blob-conn-str 'conn' --blob-container dayz-ml --amp

All-in-one (auto-downloads if dataset dir is missing):
  python train.py --dataset /tmp/dataset --blob-dataset chernarus-v1 \\
                  --blob-conn-str 'conn' --blob-container dayz-ml --amp

Resume training:
  python train.py --dataset /tmp/dataset --resume runs/<run>/checkpoint_epoch_010.pth

Fine-tune on new data:
  python train.py --dataset /tmp/dataset --resume runs/<run>/best.pth --fine-tune
"""

import argparse
import io
import json
import os
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import timm
import torch
import torch.nn as nn
from PIL import Image
from torch.amp import GradScaler, autocast
from torch.optim.lr_scheduler import CosineAnnealingLR, LinearLR, SequentialLR, StepLR
from torch.utils.data import DataLoader, Dataset, Subset
from torchvision import transforms
from tqdm import tqdm


# ── Model ─────────────────────────────────────────────────────────────────────

class LocationRegressor(nn.Module):
    """Pretrained CNN backbone with a 2-output (x, z) regression head."""

    def __init__(self, backbone: str, pretrained: bool = True, dropout: float = 0.3):
        super().__init__()
        self.backbone = timm.create_model(backbone, pretrained=pretrained, num_classes=0)
        feat_dim = self.backbone.num_features
        self.head = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(feat_dim, 512),
            nn.GELU(),
            nn.Dropout(dropout * 0.5),
            nn.Linear(512, 2),
            nn.Sigmoid(),  # constrain output to [0, 1] matching normalized labels
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.backbone(x))


# ── Dataset ───────────────────────────────────────────────────────────────────

class TarShardDataset(Dataset):
    """Reads image+label samples directly from WebDataset-format .tar shards.

    Scans all shards once at init to build a byte-offset index, then reads each
    sample with two raw file seeks — no extraction, no streaming library needed.
    Safe with any num_workers (each worker opens files independently).
    """

    def __init__(self, shard_paths: list[str], transform, image_ext: str = "jpg"):
        self.transform = transform
        # (tar_path, img_offset, img_size, jsn_offset, jsn_size)
        self.index: list[tuple] = []

        print(f"  Indexing {len(shard_paths)} shard(s) ...", end="", flush=True)
        for shard in shard_paths:
            with tarfile.open(shard, "r") as tf:
                by_stem: dict[str, dict] = {}
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    stem = Path(m.name).stem
                    ext  = Path(m.name).suffix.lstrip(".")
                    by_stem.setdefault(stem, {})[ext] = m
                for stem, exts in sorted(by_stem.items()):
                    img_m = exts.get(image_ext)
                    jsn_m = exts.get("json")
                    if img_m and jsn_m:
                        self.index.append((
                            shard,
                            img_m.offset_data, img_m.size,
                            jsn_m.offset_data, jsn_m.size,
                        ))
        print(f" {len(self.index)} samples")

    def __len__(self) -> int:
        return len(self.index)

    def __getitem__(self, idx: int):
        tar_path, img_off, img_sz, jsn_off, jsn_sz = self.index[idx]
        with open(tar_path, "rb") as f:
            f.seek(img_off)
            img_bytes = f.read(img_sz)
            f.seek(jsn_off)
            jsn_bytes = f.read(jsn_sz)
        img   = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        label = json.loads(jsn_bytes)
        x = self.transform(img)
        y = torch.tensor([label["x"], label["z"]], dtype=torch.float32)
        return x, y


IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD  = [0.229, 0.224, 0.225]


def build_transform(image_size: tuple[int, int], augment: bool = False):
    h, w = image_size
    ops = [transforms.Resize((h, w))]
    if augment:
        ops += [
            transforms.RandomHorizontalFlip(),
            transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.1),
        ]
    ops += [transforms.ToTensor(), transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)]
    return transforms.Compose(ops)


def build_dataset(info: dict, split: str, dataset_dir: Path, transform, limit: int = 0) -> Dataset:
    image_ext = "jpg" if info.get("image_format", "jpeg") == "jpeg" else "png"
    shards = [str(dataset_dir / s) for s in info["splits"][split]["shards"]]
    ds: Dataset = TarShardDataset(shards, transform, image_ext)
    if limit > 0 and len(ds) > limit:
        ds = Subset(ds, list(range(limit)))
    return ds


# ── Metrics ───────────────────────────────────────────────────────────────────

def metre_error(pred: torch.Tensor, target: torch.Tensor, bounds: dict) -> torch.Tensor:
    """Mean Euclidean distance error in metres."""
    x_range = bounds["x_max"] - bounds["x_min"]
    z_range = bounds["z_max"] - bounds["z_min"]
    dx = (pred[:, 0] - target[:, 0]) * x_range
    dz = (pred[:, 1] - target[:, 1]) * z_range
    return torch.sqrt(dx ** 2 + dz ** 2).mean()


# ── Train / val loop ──────────────────────────────────────────────────────────

def run_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    bounds: dict,
    device: torch.device,
    amp: bool,
    training: bool,
    optimizer: torch.optim.Optimizer | None = None,
    scaler: GradScaler | None = None,
    grad_clip: float = 1.0,
    desc: str = "",
) -> tuple[float, float]:
    model.train(training)
    total_loss = total_err = seen = 0

    ctx = torch.inference_mode() if not training else torch.enable_grad()
    with ctx:
        pbar = tqdm(loader, desc=desc, unit="batch", dynamic_ncols=True)
        for images, labels in pbar:
            images = images.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)

            with autocast("cuda", enabled=amp):
                preds = model(images)
                loss  = criterion(preds, labels)

            if training:
                optimizer.zero_grad(set_to_none=True)
                if amp and scaler:
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    if grad_clip > 0:
                        nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    if grad_clip > 0:
                        nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
                    optimizer.step()

            with torch.no_grad():
                err_m = metre_error(preds.float(), labels.float(), bounds).item()

            bs          = images.size(0)
            total_loss += loss.item() * bs
            total_err  += err_m * bs
            seen       += bs
            pbar.set_postfix(loss=f"{total_loss/seen:.4f}", err_m=f"{total_err/seen:.1f}m")

    denom = max(seen, 1)
    return total_loss / denom, total_err / denom


# ── Checkpointing ─────────────────────────────────────────────────────────────

def save_checkpoint(path: Path, epoch: int, model, optimizer, scheduler, best_err: float, args):
    torch.save({
        "epoch":           epoch,
        "model_state":     model.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "scheduler_state": scheduler.state_dict() if scheduler else None,
        "best_val_err_m":  best_err,
        "args":            vars(args),
    }, path)


def save_inference_artifact(path: Path, model, args, bounds: dict):
    """Minimal file needed for inference — no optimizer state."""
    torch.save({
        "model_state": model.state_dict(),
        "backbone":    args.backbone,
        "dropout":     args.dropout,
        "image_size":  args.image_size,
        "map":         args.map,
        "bounds":      bounds,
    }, path)


def maybe_push_blob(local_path: Path, blob_path: str, conn_str: str, container: str):
    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(conn_str)
        bc = client.get_blob_client(container=container, blob=blob_path)
        with open(local_path, "rb") as f:
            bc.upload_blob(f, overwrite=True)
        print(f"  [blob] pushed → {container}/{blob_path}")
    except Exception as exc:
        print(f"  [WARN] blob push failed: {exc}")


# ── Dataset auto-download ─────────────────────────────────────────────────────

def ensure_dataset(dataset_dir: Path, blob_dataset: str, conn_str: str, container: str):
    """Download dataset from blob if dataset_info.json is not already on disk."""
    if (dataset_dir / "dataset_info.json").exists():
        return
    if not blob_dataset or not conn_str:
        print(
            f"ERROR: {dataset_dir}/dataset_info.json not found.\n"
            f"       Pass --blob-dataset and --blob-conn-str to auto-download, or\n"
            f"       run: python blob_utils.py download-dataset --dataset-name <name> "
            f"--local-path {dataset_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    from azure.storage.blob import BlobServiceClient

    print(f"Downloading dataset '{blob_dataset}' → {dataset_dir} ...")
    client = BlobServiceClient.from_connection_string(conn_str).get_container_client(container)
    prefix = f"datasets/{blob_dataset}/"
    blobs  = [b.name for b in client.list_blobs(name_starts_with=prefix)]
    if not blobs:
        print(f"ERROR: no blobs found at {container}/{prefix}", file=sys.stderr)
        sys.exit(1)

    dataset_dir.mkdir(parents=True, exist_ok=True)

    def _dl(blob_name: str):
        dest = dataset_dir / blob_name[len(prefix):]
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(client.get_blob_client(blob_name).download_blob().readall())

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_dl, b): b for b in blobs}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="Downloading", unit="file"):
            try:
                fut.result()
            except Exception as exc:
                print(f"  [WARN] {futs[fut]}: {exc}")

    print("Download complete.")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def build_scheduler(optimizer, args):
    if args.scheduler == "none":
        return None
    if args.scheduler == "step":
        return StepLR(optimizer, step_size=max(args.epochs // 3, 1), gamma=0.5)
    warmup = LinearLR(optimizer, start_factor=0.01, end_factor=1.0,
                      total_iters=max(args.warmup_epochs, 1))
    cosine = CosineAnnealingLR(optimizer, T_max=max(args.epochs - args.warmup_epochs, 1),
                               eta_min=1e-7)
    if args.warmup_epochs > 0:
        return SequentialLR(optimizer, schedulers=[warmup, cosine],
                            milestones=[args.warmup_epochs])
    return cosine


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Train DayZ location regressor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Data ──────────────────────────────────────────────────────────────────
    g = p.add_argument_group("Data")
    g.add_argument("--dataset", required=True,
                   help="Local dataset directory (must contain dataset_info.json + shard tars). "
                        "Will be created by --blob-dataset auto-download if absent.")
    g.add_argument("--blob-dataset", default=None,
                   help="Blob dataset name to auto-download if --dataset dir is missing "
                        "(e.g. chernarus-v1). Requires --blob-conn-str.")
    g.add_argument("--map", default="chernarusplus",
                   help="Map key used to read bounds from dataset_info.json")
    g.add_argument("--val-split", default="val", choices=["val", "test"],
                   help="Dataset split used for validation")
    g.add_argument("--image-size", nargs=2, type=int, default=[300, 300],
                   metavar=("H", "W"), help="Spatial size fed to the backbone")
    g.add_argument("--limit", type=int, default=0,
                   help="Cap samples per split for quick dry runs (0 = no limit)")

    # ── Model ─────────────────────────────────────────────────────────────────
    g = p.add_argument_group("Model")
    g.add_argument("--backbone", default="tf_efficientnet_b3.ns_jft_in1k",
                   help="timm backbone identifier")
    g.add_argument("--dropout", type=float, default=0.3)
    g.add_argument("--no-pretrained", action="store_true",
                   help="Train backbone from scratch instead of loading ImageNet weights")

    # ── Training ──────────────────────────────────────────────────────────────
    g = p.add_argument_group("Training")
    g.add_argument("--resume", default=None,
                   help="Path to checkpoint (.pth) to resume or fine-tune from")
    g.add_argument("--fine-tune", action="store_true",
                   help="With --resume: load weights only, reset optimizer and epoch counter")
    g.add_argument("--epochs", type=int, default=30)
    g.add_argument("--batch-size", type=int, default=64)
    g.add_argument("--lr", type=float, default=1e-4,
                   help="Peak learning rate for the regression head")
    g.add_argument("--backbone-lr", type=float, default=None,
                   help="Separate peak LR for the backbone (default: --lr / 10)")
    g.add_argument("--weight-decay", type=float, default=1e-4)
    g.add_argument("--huber-delta", type=float, default=0.1,
                   help="Huber loss delta in normalized [0,1] coordinate units (~1520m on Chernarus x)")
    g.add_argument("--freeze-backbone-epochs", type=int, default=0,
                   help="Freeze backbone for first N epochs (head-only warmup)")
    g.add_argument("--warmup-epochs", type=int, default=2)
    g.add_argument("--scheduler", default="cosine", choices=["cosine", "step", "none"])
    g.add_argument("--amp", action="store_true",
                   help="Enable automatic mixed precision (fp16) — recommended on any modern GPU")
    g.add_argument("--grad-clip", type=float, default=1.0,
                   help="Max gradient norm (0 = disabled)")
    g.add_argument("--workers", type=int, default=4,
                   help="DataLoader worker processes")
    g.add_argument("--seed", type=int, default=42)

    # ── Output & checkpointing ────────────────────────────────────────────────
    g = p.add_argument_group("Output and checkpointing")
    g.add_argument("--run-name", default=None,
                   help="Human-readable run name (default: <backbone>-<timestamp>)")
    g.add_argument("--output-dir", default=None,
                   help="Local directory for artifacts (default: ./runs/<run-name>)")
    g.add_argument("--save-every", type=int, default=5,
                   help="Save a full resume checkpoint every N epochs")
    g.add_argument("--val-every", type=int, default=1,
                   help="Run validation every N epochs")
    g.add_argument("--blob-conn-str", default=None,
                   help="Azure Storage connection string. When set, best.pth and periodic "
                        "checkpoints are pushed to blob after each save.")
    g.add_argument("--blob-container", default="dayz-ml")
    g.add_argument("--no-push-blob", action="store_true",
                   help="Disable checkpoint push even if --blob-conn-str is set")

    return p.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    torch.manual_seed(args.seed)

    if not args.run_name:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        args.run_name = f"{args.backbone.split('.')[0]}-{ts}"
    out_dir = Path(args.output_dir or f"runs/{args.run_name}")
    out_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device  : {device}")
    if device.type == "cuda":
        print(f"GPU     : {torch.cuda.get_device_name(0)}")

    # Auto-download dataset if needed
    dataset_dir = Path(args.dataset)
    ensure_dataset(dataset_dir, args.blob_dataset, args.blob_conn_str or "", args.blob_container)

    with open(dataset_dir / "dataset_info.json") as f:
        info = json.load(f)

    bounds  = info["bounds"][args.map]
    n_train = info["splits"]["train"]["count"]
    n_val   = info["splits"][args.val_split]["count"]
    if args.limit > 0:
        n_train = min(n_train, args.limit)
        n_val   = min(n_val,   args.limit)

    print(f"Map     : {args.map}  x=[{bounds['x_min']}, {bounds['x_max']}]  "
          f"z=[{bounds['z_min']}, {bounds['z_max']}]")
    print(f"Samples : {n_train} train / {n_val} {args.val_split}")

    h, w = args.image_size
    print("Building train dataset ...")
    train_ds = build_dataset(info, "train",         dataset_dir, build_transform((h, w), augment=True),  args.limit)
    print("Building val dataset ...")
    val_ds   = build_dataset(info, args.val_split,  dataset_dir, build_transform((h, w), augment=False), args.limit)

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                              num_workers=args.workers, pin_memory=True, drop_last=False)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch_size, shuffle=False,
                              num_workers=args.workers, pin_memory=True, drop_last=False)

    # Model
    model = LocationRegressor(
        backbone   = args.backbone,
        pretrained = not args.no_pretrained,
        dropout    = args.dropout,
    ).to(device)

    backbone_lr = args.backbone_lr if args.backbone_lr is not None else args.lr / 10
    optimizer   = torch.optim.AdamW([
        {"params": model.backbone.parameters(), "lr": backbone_lr},
        {"params": model.head.parameters(),     "lr": args.lr},
    ], weight_decay=args.weight_decay)

    criterion = nn.HuberLoss(delta=args.huber_delta)
    scaler    = GradScaler("cuda", enabled=args.amp)
    scheduler = build_scheduler(optimizer, args)

    start_epoch = 0
    best_err_m  = float("inf")

    if args.resume:
        ckpt = torch.load(args.resume, map_location=device, weights_only=False)
        missing, unexpected = model.load_state_dict(ckpt["model_state"], strict=False)
        if missing:
            print(f"[WARN] Missing keys in checkpoint: {missing}")
        if unexpected:
            print(f"[WARN] Unexpected keys in checkpoint: {unexpected}")
        if args.fine_tune:
            print(f"Fine-tuning from {args.resume} — optimizer and epoch counter reset")
        else:
            optimizer.load_state_dict(ckpt["optimizer_state"])
            if scheduler and ckpt.get("scheduler_state"):
                scheduler.load_state_dict(ckpt["scheduler_state"])
            start_epoch = ckpt.get("epoch", 0) + 1
            best_err_m  = ckpt.get("best_val_err_m", float("inf"))
            print(f"Resumed from epoch {start_epoch} | best val err so far: {best_err_m:.1f} m")

    if args.freeze_backbone_epochs > 0:
        for param in model.backbone.parameters():
            param.requires_grad = False
        print(f"Backbone frozen for first {args.freeze_backbone_epochs} epoch(s)")

    push_blob = bool(args.blob_conn_str) and not args.no_push_blob

    print(f"\nRun     : {args.run_name}")
    print(f"Output  : {out_dir}")
    print(f"Backbone: {args.backbone}  feat_dim={model.backbone.num_features}")
    print(f"Epochs  : {start_epoch}→{args.epochs}  batch={args.batch_size}  amp={args.amp}")
    print(f"LR      : head={args.lr}  backbone={backbone_lr}  scheduler={args.scheduler}")
    print(f"Blob    : {'push enabled' if push_blob else 'disabled'}\n")

    with open(out_dir / "args.json", "w") as f:
        json.dump(vars(args), f, indent=2)

    # ── Training loop ─────────────────────────────────────────────────────────
    for epoch in range(start_epoch, args.epochs):

        if args.freeze_backbone_epochs > 0 and epoch == args.freeze_backbone_epochs:
            for param in model.backbone.parameters():
                param.requires_grad = True
            print("Backbone unfrozen")

        train_loss, train_err = run_epoch(
            model, train_loader, criterion, bounds, device, args.amp,
            training=True, optimizer=optimizer, scaler=scaler,
            grad_clip=args.grad_clip,
            desc=f"Epoch {epoch+1:3d}/{args.epochs} [train]",
        )

        if scheduler:
            scheduler.step()

        log = (f"Epoch {epoch+1:3d}/{args.epochs}"
               f"  train loss={train_loss:.4f}  err={train_err:.1f}m")

        if (epoch + 1) % args.val_every == 0:
            val_loss, val_err = run_epoch(
                model, val_loader, criterion, bounds, device, args.amp,
                training=False,
                desc=f"Epoch {epoch+1:3d}/{args.epochs} [val]  ",
            )
            log += f"  |  val loss={val_loss:.4f}  err={val_err:.1f}m"

            if val_err < best_err_m:
                best_err_m = val_err
                best_path  = out_dir / "best.pth"
                save_inference_artifact(best_path, model, args, bounds)
                log += "  ← best"
                if push_blob:
                    maybe_push_blob(best_path,
                                    f"checkpoints/{args.run_name}/best.pth",
                                    args.blob_conn_str, args.blob_container)

        print(log)

        if (epoch + 1) % args.save_every == 0:
            ckpt_name = f"checkpoint_epoch_{epoch+1:03d}.pth"
            ckpt_path = out_dir / ckpt_name
            save_checkpoint(ckpt_path, epoch, model, optimizer, scheduler, best_err_m, args)
            if push_blob:
                maybe_push_blob(ckpt_path,
                                f"checkpoints/{args.run_name}/{ckpt_name}",
                                args.blob_conn_str, args.blob_container)

    final_path = out_dir / "final.pth"
    save_inference_artifact(final_path, model, args, bounds)
    if push_blob:
        maybe_push_blob(final_path,
                        f"checkpoints/{args.run_name}/final.pth",
                        args.blob_conn_str, args.blob_container)

    print(f"\nDone. Best val error: {best_err_m:.1f} m")
    print(f"Inference artifact : {final_path}")
    print(f"Best artifact      : {out_dir / 'best.pth'}")


if __name__ == "__main__":
    main()
