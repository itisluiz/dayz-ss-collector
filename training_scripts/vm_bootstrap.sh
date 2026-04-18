#!/usr/bin/env bash
# =============================================================================
# vm_bootstrap.sh — DayZ SSCollector training VM bootstrap
# =============================================================================
# One-liner (run inside tmux):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/itisluiz/dayz-ss-collector/main/training_scripts/vm_bootstrap.sh) \
#        "DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
#
# Options:
#   --container  <name>   Blob container / environment (default: dayz-ml)
#   --dataset-path <dir>  Local dataset directory      (default: /tmp/dataset)
#   --resume <run-name>   Resume from a specific run   (default: auto-detect latest)
#   --no-resume           Start fresh, ignore all checkpoints
#
# Prerequisites on the VM:
#   python3, pip3, curl — all present on standard Vast.ai images.
#
# Blob container layout expected:
#   <container>/
#     config/
#       train_config.json        <- settings file (see train_config.example.json)
#     datasets/
#       <dataset_name>/          <- shard tars + dataset_info.json
#     checkpoints/               <- populated by train.py automatically
#     logs/                      <- JSONL epoch metrics, one file per run
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${BLUE}[bootstrap]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ok  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ warn ]${NC} $*"; }
die()  { echo -e "${RED}[error ]${NC} $*" >&2; exit 1; }
hdr()  { echo -e "\n${BOLD}${CYAN}──── $* ────${NC}"; }

# ── Defaults ──────────────────────────────────────────────────────────────────
CONTAINER="dayz-ml"
DATASET_PATH="/tmp/dataset"
RESUME_RUN=""
NO_RESUME=0
SCRIPT_DIR="/tmp/dayz-training"
REPO_RAW="https://raw.githubusercontent.com/itisluiz/dayz-ss-collector/main/training_scripts"

# ── Parse args ────────────────────────────────────────────────────────────────
if [[ $# -ge 1 && "$1" != --* ]]; then
    CONN_STR="$1"
    shift
elif [[ -n "${DAYZ_CONN_STR:-}" ]]; then
    CONN_STR="$DAYZ_CONN_STR"
else
    die "No connection string provided. Pass it as the first argument or set \$DAYZ_CONN_STR."
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --container)    CONTAINER="$2";    shift 2 ;;
        --dataset-path) DATASET_PATH="$2"; shift 2 ;;
        --resume)       RESUME_RUN="$2";   shift 2 ;;
        --no-resume)    NO_RESUME=1;       shift   ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   DayZ SSCollector — Training Bootstrap  ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
log "Container   : $CONTAINER"
log "Dataset dir : $DATASET_PATH"
if [[ $NO_RESUME -eq 1 ]]; then
    log "Resume      : disabled (fresh start)"
elif [[ -n "$RESUME_RUN" ]]; then
    log "Resume      : $RESUME_RUN"
else
    log "Resume      : auto-detect latest checkpoint"
fi

# ── 1. System deps ────────────────────────────────────────────────────────────
hdr "System dependencies"

for cmd in python3 pip3 curl; do
    command -v "$cmd" &>/dev/null || die "$cmd not found — install it and re-run"
done
ok "python3 $(python3 --version 2>&1 | awk '{print $2}'), pip3, curl"

# ── 2. azcopy ─────────────────────────────────────────────────────────────────
hdr "azcopy"

if ! command -v azcopy &>/dev/null; then
    log "Installing azcopy v10..."
    AZCOPY_TMP=$(mktemp -d)
    curl -fsSL "https://aka.ms/downloadazcopy-v10-linux" | tar xz -C "$AZCOPY_TMP" --strip-components=1
    install -m 755 "$AZCOPY_TMP/azcopy" /usr/local/bin/azcopy
    rm -rf "$AZCOPY_TMP"
    ok "azcopy installed → $(azcopy --version 2>&1 | head -1)"
else
    ok "azcopy present → $(azcopy --version 2>&1 | head -1)"
fi

# ── 3. Training scripts ───────────────────────────────────────────────────────
hdr "Training scripts"

mkdir -p "$SCRIPT_DIR"
for f in train.py requirements.txt; do
    log "Fetching $f..."
    curl -fsSL "$REPO_RAW/$f" -o "$SCRIPT_DIR/$f"
done
ok "Scripts downloaded to $SCRIPT_DIR"

# ── 4. Python packages ────────────────────────────────────────────────────────
hdr "Python packages"

pip3 install -q -r "$SCRIPT_DIR/requirements.txt"
ok "Dependencies installed"

# ── 5. Blob config + SAS + checkpoint discovery ───────────────────────────────
hdr "Blob config"

log "Fetching config/train_config.json from '$CONTAINER'..."

cat > /tmp/dayz_blob_setup.py << 'PYEOF'
import json, sys, os
from datetime import datetime, timedelta, timezone

conn_str   = os.environ["DAYZ_CONN_STR"]
container  = os.environ["DAYZ_CONTAINER"]
resume_run = os.environ.get("DAYZ_RESUME_RUN", "")
no_resume  = os.environ.get("DAYZ_NO_RESUME", "0") == "1"
dataset_path = os.environ.get("DAYZ_DATASET_PATH", "/tmp/dataset")
script_dir   = os.environ.get("DAYZ_SCRIPT_DIR", "/tmp/dayz-training")

try:
    from azure.storage.blob import (BlobServiceClient, generate_container_sas,
                                     ContainerSasPermissions)
except ImportError:
    print("ERROR: azure-storage-blob not installed", file=sys.stderr)
    sys.exit(1)

def sh_export(k, v):
    escaped = str(v).replace("'", "'\\''")
    return f"export {k}='{escaped}'"

# ── Connect ───────────────────────────────────────────────────────────────────
try:
    svc = BlobServiceClient.from_connection_string(conn_str)
    cc  = svc.get_container_client(container)
    # Verify connection by listing (cheap call)
    next(iter(cc.list_blobs(name_starts_with="config/")), None)
except Exception as e:
    print(f"ERROR: cannot connect to container '{container}': {e}", file=sys.stderr)
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
try:
    cfg = json.loads(cc.get_blob_client("config/train_config.json").download_blob().readall())
except Exception as e:
    print(f"ERROR: could not fetch config/train_config.json from '{container}': {e}", file=sys.stderr)
    print(f"       Upload it first (see train_config.example.json in the repo)", file=sys.stderr)
    sys.exit(1)

dataset_name = cfg.get("dataset_name", "")
if not dataset_name:
    print("ERROR: train_config.json is missing 'dataset_name'", file=sys.stderr)
    sys.exit(1)

# ── SAS token (6h — enough for download + full training session) ───────────────
account = svc.account_name
key     = svc.credential.account_key
expiry  = datetime.now(timezone.utc) + timedelta(hours=6)
sas     = generate_container_sas(
    account, container, account_key=key,
    permission=ContainerSasPermissions(read=True, list=True),
    expiry=expiry)
base_url = f"https://{account}.blob.core.windows.net/{container}"

# Dataset URL: use /* so azcopy drops files directly into dest without a subdir
dataset_azcopy_url = f"{base_url}/datasets/{dataset_name}/*?{sas}"

# ── Find latest checkpoint ────────────────────────────────────────────────────
ckpt_url       = ""
ckpt_run       = ""
ckpt_fine_tune = "0"

if not no_resume:
    all_blobs = [b.name for b in cc.list_blobs(name_starts_with="checkpoints/")]

    if resume_run:
        chosen_run = resume_run
    else:
        # Runs are named <backbone>-<timestamp>; alphabetical sort = chronological
        runs = sorted(set(
            b.split("/")[1] for b in all_blobs if len(b.split("/")) >= 3
        ))
        chosen_run = runs[-1] if runs else ""

    if chosen_run:
        run_blobs   = [b for b in all_blobs if b.startswith(f"checkpoints/{chosen_run}/")]
        epoch_ckpts = sorted([b for b in run_blobs if "checkpoint_epoch_" in b])
        best_ckpts  = [b for b in run_blobs if b.endswith("best.pth")]

        if epoch_ckpts:
            # Full resume — optimizer and scheduler state intact
            ckpt_blob      = epoch_ckpts[-1]
            ckpt_fine_tune = "0"
        elif best_ckpts:
            # Inference artifact — weights only, reset optimizer
            ckpt_blob      = best_ckpts[0]
            ckpt_fine_tune = "1"
        else:
            ckpt_blob = ""

        if ckpt_blob:
            ckpt_url = f"{base_url}/{ckpt_blob}?{sas}"
            ckpt_run = chosen_run

# ── Build train.py command from config ────────────────────────────────────────
def cfg_flag(key, flag, is_bool=False, default=None):
    val = cfg.get(key, default)
    if val is None:
        return ""
    if is_bool:
        return f"  {flag} \\\n" if val else ""
    if isinstance(val, list):
        return f"  {flag} {' '.join(str(v) for v in val)} \\\n"
    return f"  {flag} {val} \\\n"

ckpt_local = f"{script_dir}/resume.pth"

cmd_lines = [f"python3 {script_dir}/train.py \\\n"]
cmd_lines.append(f"  --dataset {dataset_path} \\\n")
cmd_lines.append(f"  --blob-conn-str \"$DAYZ_CONN_STR\" \\\n")
cmd_lines.append(f"  --blob-container {container} \\\n")
cmd_lines.append(cfg_flag("map",             "--map"))
cmd_lines.append(cfg_flag("image_size",      "--image-size"))
cmd_lines.append(cfg_flag("model",           "--backbone"))
cmd_lines.append(cfg_flag("batch_size",      "--batch-size"))
cmd_lines.append(cfg_flag("epochs",          "--epochs"))
cmd_lines.append(cfg_flag("lr",              "--lr"))
cmd_lines.append(cfg_flag("backbone_lr",     "--backbone-lr"))
cmd_lines.append(cfg_flag("weight_decay",    "--weight-decay"))
cmd_lines.append(cfg_flag("dropout",         "--dropout"))
cmd_lines.append(cfg_flag("warmup_epochs",   "--warmup-epochs"))
cmd_lines.append(cfg_flag("scheduler",       "--scheduler"))
cmd_lines.append(cfg_flag("grad_clip",       "--grad-clip"))
cmd_lines.append(cfg_flag("workers",         "--workers"))
cmd_lines.append(cfg_flag("save_every",      "--save-every"))
cmd_lines.append(cfg_flag("val_every",       "--val-every"))
cmd_lines.append(cfg_flag("early_stop_patience", "--early-stop"))
cmd_lines.append(cfg_flag("amp",             "--amp",  is_bool=True))
if ckpt_url:
    cmd_lines.append(f"  --resume {ckpt_local} \\\n")
    if ckpt_fine_tune == "1":
        cmd_lines.append("  --fine-tune \\\n")
# Strip trailing backslash from last real line
cmd = "".join(l for l in cmd_lines if l.strip()).rstrip(" \\\n")

# ── Output shell-sourceable vars ──────────────────────────────────────────────
lines = [
    sh_export("DATASET_NAME",        dataset_name),
    sh_export("DATASET_AZCOPY_URL",  dataset_azcopy_url),
    sh_export("CKPT_URL",            ckpt_url),
    sh_export("CKPT_RUN",            ckpt_run),
    sh_export("CKPT_FINE_TUNE",      ckpt_fine_tune),
    sh_export("CKPT_LOCAL",          ckpt_local),
    sh_export("TRAIN_CMD",           cmd),
]
print("\n".join(lines))

# Human-readable summary to stderr so it shows in the terminal
def info(msg): print(f"  {msg}", file=sys.stderr)
info(f"Dataset    : {dataset_name}")
info(f"Image size : {cfg.get('image_size', '?')}")
info(f"Batch size : {cfg.get('batch_size', '?')}  |  AMP: {cfg.get('amp', False)}")
info(f"Epochs     : {cfg.get('epochs', '?')}  |  Early stop patience: {cfg.get('early_stop_patience', 0)}")
if ckpt_url:
    resume_type = "full resume (epoch checkpoint)" if ckpt_fine_tune == "0" else "fine-tune (best.pth)"
    info(f"Checkpoint : {ckpt_blob}  [{resume_type}]")
else:
    info("Checkpoint : none — starting fresh")
PYEOF

DAYZ_CONN_STR="$CONN_STR" \
DAYZ_CONTAINER="$CONTAINER" \
DAYZ_RESUME_RUN="$RESUME_RUN" \
DAYZ_NO_RESUME="$NO_RESUME" \
DAYZ_DATASET_PATH="$DATASET_PATH" \
DAYZ_SCRIPT_DIR="$SCRIPT_DIR" \
python3 /tmp/dayz_blob_setup.py > /tmp/dayz_vm_env.sh

# shellcheck disable=SC1091
source /tmp/dayz_vm_env.sh
ok "Config loaded"

# ── 6. Dataset download ───────────────────────────────────────────────────────
hdr "Dataset"

if [[ -f "$DATASET_PATH/dataset_info.json" ]]; then
    ok "Dataset already present at $DATASET_PATH — skipping download"
else
    log "Downloading dataset '$DATASET_NAME' → $DATASET_PATH (using azcopy)..."
    mkdir -p "$DATASET_PATH"

    # azcopy exits non-zero on partial failures but we want to check ourselves
    set +e
    azcopy copy "$DATASET_AZCOPY_URL" "$DATASET_PATH" --recursive 2>&1
    AZCOPY_EXIT=$?
    set -e

    # Verify dataset_info.json landed in the right place
    if [[ ! -f "$DATASET_PATH/dataset_info.json" ]]; then
        # azcopy may have created a subdir — find and flatten it
        FOUND=$(find "$DATASET_PATH" -name "dataset_info.json" -maxdepth 3 | head -1)
        if [[ -z "$FOUND" ]]; then
            die "dataset_info.json not found after download. azcopy exit: $AZCOPY_EXIT"
        fi
        SUBDIR=$(dirname "$FOUND")
        warn "Files landed in subdir $SUBDIR — moving up..."
        mv "$SUBDIR"/* "$DATASET_PATH/"
        rmdir "$SUBDIR" 2>/dev/null || true
    fi

    ok "Dataset ready at $DATASET_PATH"
fi

# Quick sanity check
SHARD_COUNT=$(find "$DATASET_PATH" -name "*.tar" | wc -l)
log "Shards found: $SHARD_COUNT"
[[ $SHARD_COUNT -eq 0 ]] && warn "No .tar shards found — dataset may be incomplete!"

# ── 7. Checkpoint download ────────────────────────────────────────────────────
hdr "Checkpoint"

if [[ -n "$CKPT_URL" ]]; then
    log "Downloading checkpoint from run '$CKPT_RUN'..."
    RESUME_TYPE="full resume"
    [[ "$CKPT_FINE_TUNE" == "1" ]] && RESUME_TYPE="fine-tune (best.pth — optimizer reset)"
    log "Type: $RESUME_TYPE"
    azcopy copy "$CKPT_URL" "$CKPT_LOCAL" 2>&1
    ok "Checkpoint at $CKPT_LOCAL"
else
    ok "No checkpoint found — starting fresh"
fi

# ── 8. Launch training ────────────────────────────────────────────────────────
hdr "Training"

LOG_FILE="/tmp/dayz_train_$(date +%Y%m%d_%H%M%S).log"
log "Log file: $LOG_FILE"
echo
echo -e "${BOLD}Command:${NC}"
echo "$TRAIN_CMD" | sed 's/^/  /'
echo

# Export conn str so train.py picks it up via the $DAYZ_CONN_STR reference in the command
export DAYZ_CONN_STR="$CONN_STR"

# Evaluate the command (it references $DAYZ_CONN_STR)
eval "$TRAIN_CMD" 2>&1 | tee "$LOG_FILE"
TRAIN_EXIT=${PIPESTATUS[0]}

echo
if [[ $TRAIN_EXIT -eq 0 ]]; then
    ok "Training complete. Log saved to $LOG_FILE"
else
    die "train.py exited with code $TRAIN_EXIT. Log: $LOG_FILE"
fi
