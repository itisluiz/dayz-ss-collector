#!/usr/bin/env python3
"""
DayZ SSCollector — Azure Blob Storage Utility

Manages dataset and checkpoint storage in Azure Blob Storage.

Container layout:
  <container>/
    datasets/
      <dataset-name>/
        dataset_info.json
        train/  shard-000000.tar  shard-000001.tar  ...
        val/    shard-000000.tar  ...
        test/   shard-000000.tar  ...
    checkpoints/
      <run-name>/
        args.json
        best.pth
        final.pth
        checkpoint_epoch_005.pth  ...

Subcommands:
  upload-dataset     Upload a local dataset directory to blob
  download-dataset   Download a dataset from blob to local directory
  upload-checkpoint  Upload a local .pth file to blob
  download-checkpoint  Download a checkpoint artifact from blob
  list-datasets      List datasets in the container
  list-checkpoints   List checkpoint runs (or artifacts within a run)
  gen-sas            Generate a SAS token for dataset streaming
  print-train-cmd    Print a ready-to-run train.py command with all blob args filled in
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm


# ── Blob helpers ──────────────────────────────────────────────────────────────

def get_service_client(conn_str: str):
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(conn_str)


def get_container_client(conn_str: str, container: str):
    return get_service_client(conn_str).get_container_client(container)


def ensure_container(conn_str: str, container: str):
    client = get_container_client(conn_str, container)
    try:
        client.create_container()
        print(f"Created container: {container}")
    except Exception:
        pass  # already exists
    return client


def upload_file(container_client, local_path: Path, blob_path: str, overwrite: bool = True):
    blob = container_client.get_blob_client(blob_path)
    with open(local_path, "rb") as f:
        blob.upload_blob(f, overwrite=overwrite)


def download_file(container_client, blob_path: str, local_path: Path):
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob = container_client.get_blob_client(blob_path)
    with open(local_path, "wb") as f:
        f.write(blob.download_blob().readall())


def list_blobs_prefix(container_client, prefix: str) -> list[str]:
    return [b.name for b in container_client.list_blobs(name_starts_with=prefix)]


# ── Upload dataset ─────────────────────────────────────────────────────────────

def cmd_upload_dataset(args):
    local = Path(args.local_path)
    if not local.is_dir():
        print(f"ERROR: {local} is not a directory", file=sys.stderr)
        sys.exit(1)

    info_file = local / "dataset_info.json"
    if not info_file.exists():
        print(f"ERROR: dataset_info.json not found in {local}", file=sys.stderr)
        sys.exit(1)

    with open(info_file) as f:
        info = json.load(f)

    # Collect all files to upload
    files: list[tuple[Path, str]] = []
    files.append((info_file, f"datasets/{args.dataset_name}/dataset_info.json"))
    for split, split_info in info["splits"].items():
        for shard in split_info["shards"]:
            local_shard = local / shard
            if not local_shard.exists():
                print(f"[WARN] Missing shard: {local_shard}")
                continue
            files.append((local_shard, f"datasets/{args.dataset_name}/{shard}"))

    container_client = ensure_container(args.conn_str, args.container)
    print(f"Uploading {len(files)} files to {args.container}/datasets/{args.dataset_name}/")

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(upload_file, container_client, lp, bp, not args.no_overwrite): bp
                for lp, bp in files
            }
            for fut in tqdm(as_completed(futures), total=len(futures), unit="file"):
                bp = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[WARN] Failed to upload {bp}: {exc}")
    else:
        for lp, bp in tqdm(files, unit="file"):
            upload_file(container_client, lp, bp, not args.no_overwrite)

    print("Upload complete.")
    print()
    _print_sas_hint(args.conn_str, args.container, args.dataset_name)


# ── Download dataset ──────────────────────────────────────────────────────────

def cmd_download_dataset(args):
    container_client = get_container_client(args.conn_str, args.container)
    prefix           = f"datasets/{args.dataset_name}/"
    blobs            = list_blobs_prefix(container_client, prefix)

    if not blobs:
        print(f"ERROR: No blobs found at {args.container}/{prefix}", file=sys.stderr)
        sys.exit(1)

    local = Path(args.local_path)
    print(f"Downloading {len(blobs)} files to {local}/")

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(download_file, container_client, bp,
                            local / bp[len(prefix):]): bp
                for bp in blobs
            }
            for fut in tqdm(as_completed(futures), total=len(futures), unit="file"):
                bp = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[WARN] Failed to download {bp}: {exc}")
    else:
        for bp in tqdm(blobs, unit="file"):
            download_file(container_client, bp, local / bp[len(prefix):])

    print("Download complete.")


# ── Upload checkpoint ─────────────────────────────────────────────────────────

def cmd_upload_checkpoint(args):
    local = Path(args.local_path)
    if not local.exists():
        print(f"ERROR: {local} not found", file=sys.stderr)
        sys.exit(1)

    blob_name = f"checkpoints/{args.run_name}/{local.name}"
    container_client = ensure_container(args.conn_str, args.container)
    print(f"Uploading {local} → {args.container}/{blob_name}")
    upload_file(container_client, local, blob_name)
    print("Done.")


# ── Download checkpoint ───────────────────────────────────────────────────────

def cmd_download_checkpoint(args):
    container_client = get_container_client(args.conn_str, args.container)

    if args.artifact:
        # Specific artifact name
        artifact = args.artifact
        if not artifact.endswith(".pth"):
            artifact += ".pth"
        blob_name = f"checkpoints/{args.run_name}/{artifact}"
        blobs = [blob_name]
    else:
        # Download entire run directory
        prefix = f"checkpoints/{args.run_name}/"
        blobs  = list_blobs_prefix(container_client, prefix)
        if not blobs:
            print(f"ERROR: No checkpoints found for run '{args.run_name}'", file=sys.stderr)
            sys.exit(1)

    out = Path(args.output)
    print(f"Downloading {len(blobs)} file(s) to {out}/")
    for bp in tqdm(blobs, unit="file"):
        dest = out / Path(bp).name
        download_file(container_client, bp, dest)
    print("Done.")


# ── List datasets ─────────────────────────────────────────────────────────────

def cmd_list_datasets(args):
    container_client = get_container_client(args.conn_str, args.container)
    blobs = list_blobs_prefix(container_client, "datasets/")

    # Gather unique dataset names from paths like datasets/<name>/...
    datasets: dict[str, dict] = {}
    for b in blobs:
        parts = b.split("/")
        if len(parts) < 2:
            continue
        name = parts[1]
        if name not in datasets:
            datasets[name] = {"shards": 0, "has_info": False}
        if b.endswith("dataset_info.json"):
            datasets[name]["has_info"] = True
        elif b.endswith(".tar"):
            datasets[name]["shards"] += 1

    if not datasets:
        print("No datasets found.")
        return

    print(f"{'Dataset':<30}  {'Shards':>6}  Info")
    print("-" * 45)
    for name, meta in sorted(datasets.items()):
        info_mark = "yes" if meta["has_info"] else "MISSING"
        print(f"{name:<30}  {meta['shards']:>6}  {info_mark}")


# ── List checkpoints ──────────────────────────────────────────────────────────

def cmd_list_checkpoints(args):
    container_client = get_container_client(args.conn_str, args.container)

    if args.run_name:
        prefix = f"checkpoints/{args.run_name}/"
        blobs  = list_blobs_prefix(container_client, prefix)
        if not blobs:
            print(f"No checkpoints found for run '{args.run_name}'")
            return
        print(f"Run: {args.run_name}")
        for b in sorted(blobs):
            print(f"  {Path(b).name}")
    else:
        blobs = list_blobs_prefix(container_client, "checkpoints/")
        runs: set[str] = set()
        for b in blobs:
            parts = b.split("/")
            if len(parts) >= 2:
                runs.add(parts[1])
        if not runs:
            print("No checkpoint runs found.")
            return
        print("Checkpoint runs:")
        for r in sorted(runs):
            print(f"  {r}")


# ── Generate SAS token ────────────────────────────────────────────────────────

def cmd_gen_sas(args):
    from azure.storage.blob import (
        BlobServiceClient,
        generate_container_sas,
        ContainerSasPermissions,
    )

    svc         = BlobServiceClient.from_connection_string(args.conn_str)
    account     = svc.account_name
    account_key = svc.credential.account_key
    expiry      = datetime.now(timezone.utc) + timedelta(hours=args.expiry_hours)

    perms = ContainerSasPermissions(read=True, list=True, write=args.write, delete=args.write)
    sas   = generate_container_sas(
        account_name=account,
        container_name=args.container,
        account_key=account_key,
        permission=perms,
        expiry=expiry,
    )

    print(f"SAS token (expires {expiry.strftime('%Y-%m-%d %H:%M UTC')}):")
    print(sas)
    print()
    print("Container URL with token:")
    print(f"https://{account}.blob.core.windows.net/{args.container}?{sas}")


# ── Print train command ───────────────────────────────────────────────────────

def cmd_print_train_cmd(args):
    local_path = args.local_path or f"/tmp/{args.dataset_name}"

    flags = []
    if args.run_name:
        flags.append(f"--run-name {args.run_name}")
    if args.resume:
        flags.append(f"--resume {args.resume}")
    if args.fine_tune:
        flags.append("--fine-tune")
    extra = " \\\n  ".join(args.extra) if args.extra else ""
    if extra:
        flags.append(extra)

    flags_block = ""
    if flags:
        flags_block = "  " + " \\\n  ".join(flags) + " \\\n"

    cmd = (
        f"python train.py \\\n"
        f"  --dataset \"{local_path}\" \\\n"
        f"  --blob-dataset {args.dataset_name} \\\n"
        f"  --blob-conn-str \"{args.conn_str}\" \\\n"
        f"  --blob-container {args.container} \\\n"
        f"{flags_block}"
        f"  --amp"
    )

    run_name = args.run_name or "<run-name>"

    print("# ── Ready-to-run train command ────────────────────────────────────────")
    print(f"# Dataset auto-downloads to {local_path} if not already present")
    print()
    print(cmd)
    print()
    print("# Download best artifact after training:")
    print(f"python blob_utils.py download-checkpoint "
          f"--conn-str \"...\" --container {args.container} "
          f"--run-name {run_name} --artifact best --output ./artifacts")


def _print_sas_hint(conn_str: str, container: str, dataset_name: str):
    """After upload, print the next step hint."""
    print("Next: generate a SAS token and print the train command:")
    print(f"  python blob_utils.py print-train-cmd "
          f"--conn-str \"<conn-str>\" "
          f"--container {container} "
          f"--dataset-name {dataset_name}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Azure Blob Storage utility for SSCollector training",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sp = p.add_subparsers(dest="command", required=True)

    # Shared args helper
    def add_common(sub):
        sub.add_argument("--conn-str", required=True,
                         help="Azure Storage connection string "
                              "(from portal → Storage account → Access keys)")
        sub.add_argument("--container", default="dayz-ml",
                         help="Blob container name")

    # upload-dataset
    sub = sp.add_parser("upload-dataset", help="Upload a local dataset to blob")
    add_common(sub)
    sub.add_argument("--dataset-name", required=True,
                     help="Name the dataset will be stored under in blob "
                          "(e.g. chernarus-v1)")
    sub.add_argument("--local-path", required=True,
                     help="Local dataset directory containing dataset_info.json and shard tars")
    sub.add_argument("--workers", type=int, default=8,
                     help="Parallel upload threads")
    sub.add_argument("--no-overwrite", action="store_true",
                     help="Skip blobs that already exist (useful to resume interrupted uploads)")

    # download-dataset
    sub = sp.add_parser("download-dataset", help="Download a dataset from blob to local disk")
    add_common(sub)
    sub.add_argument("--dataset-name", required=True)
    sub.add_argument("--local-path", required=True,
                     help="Local destination directory")
    sub.add_argument("--workers", type=int, default=8)

    # upload-checkpoint
    sub = sp.add_parser("upload-checkpoint", help="Upload a .pth file to blob")
    add_common(sub)
    sub.add_argument("--run-name", required=True,
                     help="Run name — file lands in checkpoints/<run-name>/")
    sub.add_argument("--local-path", required=True,
                     help="Path to .pth file")

    # download-checkpoint
    sub = sp.add_parser("download-checkpoint", help="Download checkpoint artifact(s) from blob")
    add_common(sub)
    sub.add_argument("--run-name", required=True)
    sub.add_argument("--artifact", default=None,
                     help="Specific artifact to download: best, final, or checkpoint_epoch_NNN. "
                          "Omit to download entire run directory")
    sub.add_argument("--output", default="./artifacts",
                     help="Local destination directory")

    # list-datasets
    sub = sp.add_parser("list-datasets", help="List datasets in the container")
    add_common(sub)

    # list-checkpoints
    sub = sp.add_parser("list-checkpoints", help="List checkpoint runs or artifacts")
    add_common(sub)
    sub.add_argument("--run-name", default=None,
                     help="List artifacts within a specific run (omit to list all runs)")

    # gen-sas
    sub = sp.add_parser("gen-sas",
                        help="Generate a SAS token for read access (used by train.py --sas)")
    add_common(sub)
    sub.add_argument("--expiry-hours", type=int, default=48,
                     help="Token validity window in hours")
    sub.add_argument("--write", action="store_true",
                     help="Include write/delete permissions (for checkpoint push from training VM)")

    # print-train-cmd
    sub = sp.add_parser("print-train-cmd",
                        help="Print a ready-to-run train.py command with all blob args filled in")
    add_common(sub)
    sub.add_argument("--dataset-name", required=True)
    sub.add_argument("--local-path", default=None,
                     help="Local path for the dataset (default: /tmp/<dataset-name>)")
    sub.add_argument("--run-name", default=None,
                     help="Pass a --run-name to the generated command")
    sub.add_argument("--resume", default=None,
                     help="Pass a --resume path to the generated command")
    sub.add_argument("--fine-tune", action="store_true",
                     help="Add --fine-tune flag to the generated command")
    sub.add_argument("extra", nargs=argparse.REMAINDER,
                     help="Extra flags appended verbatim to the generated train.py command "
                          "(e.g. -- --epochs 50 --batch-size 32)")

    return p.parse_args()


def main():
    args = parse_args()
    dispatch = {
        "upload-dataset":      cmd_upload_dataset,
        "download-dataset":    cmd_download_dataset,
        "upload-checkpoint":   cmd_upload_checkpoint,
        "download-checkpoint": cmd_download_checkpoint,
        "list-datasets":       cmd_list_datasets,
        "list-checkpoints":    cmd_list_checkpoints,
        "gen-sas":             cmd_gen_sas,
        "print-train-cmd":     cmd_print_train_cmd,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
