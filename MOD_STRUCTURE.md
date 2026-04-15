# SSCollector — Project Structure

A DayZ server mod for collecting labeled screenshots to train a map-localization ML model. The player cycles through pre-generated locations; at each one the server applies weather and time of day and orients FreeDebugCamera. The Python automation script navigates, captures metadata, takes screenshots, and saves state for unattended overnight runs. The resulting dataset can then be packed into WebDataset shards for training on Azure.

---

## Directory Layout

```
mod/
├── mod.cpp
├── Scripts/
│   ├── config.cpp
│   ├── data/inputs.xml
│   ├── 3_Game/SSCollector/
│   │   ├── SSCConstants.c       — RPC IDs + SSCCameraCommand cross-layer bridge
│   │   ├── SSCConfig.c          — Config schema + loader (config.json)
│   │   └── SSCLocation.c        — JSON data classes for locations.json
│   ├── 4_World/SSCollector/
│   │   ├── SSCNavigator.c       — Location store, generation logic, PlayerBase modding
│   │   └── SSCMeta.c            — Metadata file writer + CAPTURE_META RPC handler
│   └── 5_Mission/SSCollector/
│       ├── SSCClientChat.c      — MissionGameplay: keybinds, chat commands, RPC senders
│       └── SSCollectorMission.c — MissionServer.OnInit: boots config + navigator

scripts/
├── settings.json        — Shared config: paths, server/client settings
├── requirements.txt     — Python deps: pywin32, mss, pynput, colorama, psutil, Pillow, numpy
│
├── — Capture pipeline —
├── collect.py           — Automated screenshot loop (navigate → metadata → screenshot)
├── validate_dataset.py  — Validate captured dataset against locations.json; reports issues
├── prepare_dataset.py   — Pack output/ into WebDataset tar shards for ML training
│
├── — Dev workflow —
├── pipeline.py          — Orchestrate: build → kill → server → client
├── launch.py            — Launch server or client individually
├── kill.py              — Kill DayZ server / client processes
├── pack.py              — Pack mod PBO and deploy to server/client dirs
├── logs.py              — Read, search, tail DayZ script logs
├── toolkit.py           — GUI wrapper for all dev-scripts
│
├── — Utilities —
├── viewer.py            — Interactive map viewer for locations.json
└── _common.py           — Shared helpers (settings loader, log finder, etc.)

collect.bat              — One-click launcher for collect.py
```

---

## Data Flow

### 1. Location generation

```
Client: /ss-generate <step> [N]
  → SSCClientChat sends GENERATE_GRID RPC to server
  → PlayerBase.SSC_GenerateGrid (SSCNavigator.c):
      For each grid point on the map:
        • SurfaceRoadY → snap to floor height
        • IsClearAngle → raycast (BUILDING|TERRAIN|ROADWAY) rejects wall-facing angles
        • 4 time/weather presets × N valid yaw angles written per point
          (preset is the outer loop — all positions for one condition before the next)
  → SSCNavigator.Save() writes $profile:SSCollector/locations.json

Client: /ss-add [N]
  → Same as above but for the player's current position only
```

Generation presets (4 per valid yaw angle):

| Preset | Time | Overcast | Fog | Rain | Notes |
|--------|------|---------|-----|------|-------|
| Pre-dawn | 05:30 | 0.0 | 0.0 | 0.0 | Deep blue sky |
| Overcast noon | 12:00 | 0.7 | 0.0 | 0.0 | Flat, shadowless |
| Dusk | 19:00 | 0.0 | 0.0 | 0.0 | Golden hour |
| Night | 22:00 | 0.0 | 0.0 | 0.0 | Dark |

Default grid for Chernarus produces ~4,500 spatial points × 6 yaw angles × 4 presets = ~97k entries.

---

### 2. Navigation

```
Client: UASSCNextLocation / UASSCPrevLocation keybind  — or —  /ss-goto <N>
  → SSCClientChat sends NAVIGATE (delta ±1) or SET_INDEX (absolute) RPC
  → PlayerBase.SSC_Navigate / SSC_GoTo (SSCNavigator.c):
      • Updates m_SSCLocIndex
      • Weather.Set → applies overcast/fog/rain server-wide
      • GetWorld().SetDate → sets time of day
      • Sends SET_CAMERA RPC → eye position (floor+1.5m) + yaw + pitch
  → PlayerBase.OnRPC (client side) writes to SSCCameraCommand statics (3_Game)
  → MissionGameplay.OnUpdate (SSCClientChat.c):
      • Detects SSCCameraCommand.pending
      • Places and activates FreeDebugCamera at eye position + orientation
```

**Note — player position:** The player character is NOT teleported to shot locations. `FreeDebugCamera` is positioned independently. The player only needs to be within terrain-streaming range (~1 km). Use `/ss-exile` at session start to move the character to the map corner.

---

### 3. Screenshot capture

```
Client: UASSCCaptureMeta keybind  or  /ss-meta
  → SSCClientChat reads GetCurrentCameraDirection()
  → Sends CAPTURE_META RPC to server
  → SSCMetaWriter.Write (SSCMeta.c) runs server-side:
      Writes $profile:SSCollector/output/ss-meta-N.json:
        { locationIndex, map, position {x,y,z}, cameraDirection {x,y,z},
          timeOfDay, weather {overcast, rain, fog, snowfall, windSpeed} }
```

---

### 4. Automated capture (collect.py)

```
python scripts/collect.py  (or double-click collect.bat)

  1. Find DayZ_x64.exe window for screenshots
  2. Wait for HOME keypress (user focuses DayZ first)
  3. Send /ss-exile + /ss-freecam via AutoHotkey
  4. For each index in [start, total):
       a. Skip if ss-<N>.png + ss-<N>.json already exist  ← resume / retake safety
       b. Send /ss-goto <N> via AHK
       c. Wait --delay seconds (default 1.5s) for lighting to settle
          If weather/time preset changed from last navigated location: wait --transition-delay (60s)
       d. Snapshot existing ss-meta-*.json files
       e. Send /ss-meta via AHK
       f. Poll for new ss-meta-*.json with matching locationIndex (8s timeout)
       g. Screenshot DayZ window → ss-<N>.png
       h. Rename matched JSON → ss-<N>.json
       i. Save state to output/collect_state.json
  5. HOME toggles pause/resume mid-run; Ctrl+C saves state and exits cleanly

Retakes: delete both ss-<N>.png and ss-<N>.json; the loop recaptures them automatically.
```

---

### 5. Dataset validation (validate_dataset.py)

```
python scripts/validate_dataset.py

  Scans every index 0..(total-1) from locations.json and checks:
    • Both ss-<N>.png and ss-<N>.json are present
    • JSON is parseable
    • locationIndex field matches filename index
    • Captured position matches locations.json entry  (default threshold: 10m)
    • Camera direction matches locations.json entry   (default threshold: 10°)

  Silent on clean files; prints issues only. Consecutive missing indices shown
  as compact ranges (e.g. 120-134). Summary counts at the end.

Options: --pos-threshold M  --cam-threshold DEG
```

---

### 6. Dataset preparation (prepare_dataset.py)

```
python scripts/prepare_dataset.py [options]

  Packs output/ into WebDataset tar shards for ML training.

  Split strategy: by spatial position, not by individual sample.
    All 24 views (6 yaws × 4 conditions) of each location go to the same split,
    so the test set contains genuinely unseen spatial positions.

  Each shard is a standard tar. Files sharing a stem are one sample:
    000042.jpg / 000042.png  — screenshot (optionally resized)
    000042.json              — label:
      { "x": 0.312,          ← normalized [0, 1]
        "z": 0.741,          ← normalized [0, 1]
        "x_m": 4800.0,       ← raw metres
        "z_m": 11380.0,      ← raw metres
        "map": "chernarusplus" }
      With --full-meta also includes: position.y, cameraDirection, timeOfDay, weather

Output layout:
  dataset/
    train/  shard-000000.tar  shard-000001.tar  ...
    val/    shard-000000.tar  ...
    test/   shard-000000.tar  ...
    dataset_info.json   ← bounds, shard list, split sizes, image format

Recommended invocation (Chernarus ~97k samples, targeting Azure):
  python scripts/prepare_dataset.py --image-size 512 --jpeg --jpeg-quality 90 --full-meta

Key options:
  --image-size N or WxH  Resize: '512' → 512×512, '640x360' → 640×360 (omit = keep original 1920×1080)
  --jpeg                 Encode as JPEG (~10× smaller than PNG)
  --jpeg-quality N       JPEG quality, default 90
  --full-meta            Embed all metadata per sample for future tasks
  --shard-size N         Samples per shard, default 1000
  --split T V T          Train/val/test fractions, default 0.8 0.1 0.1
  --workers N            Parallel image-encoding processes, default cpu_count-1
  --limit N              Cap total samples — useful for dry runs
  --bounds PATH          Load pre-computed bounds JSON (skips re-scanning)

Loading in Python (webdataset library):
  import webdataset as wds, json
  info   = json.load(open("dataset/dataset_info.json"))
  shards = ["dataset/" + s for s in info["splits"]["train"]["shards"]]
  ds     = wds.WebDataset(shards).decode("rgb").to_tuple("jpg", "json")
```

---

### Dataset output layout

```
dataset/
├── dataset_info.json
├── train/
│   ├── shard-000000.tar
│   ├── shard-000001.tar
│   └── ...
├── val/
│   └── shard-000000.tar  ...
└── test/
    └── shard-000000.tar  ...
```

#### `dataset_info.json`

Top-level manifest written after all shards are complete.

```json
{
  "created":      "2025-04-10T02:14:00+00:00",  // ISO-8601 UTC
  "total":        96988,                          // total samples across all splits
  "seed":         42,                             // random seed used for the split
  "image_size":   "512x512",                      // "WxH" or "original"
  "image_format": "jpeg",                         // "jpeg" or "png"
  "full_meta":    true,                           // whether per-sample meta was embedded
  "bounds": {
    "chernarusplus": {
      "x_min": 0.0, "x_max": 15200.0,            // metres; used to normalize x to [0,1]
      "z_min": 1200.0, "z_max": 15200.0           // z_min ~1200 because south is ocean
    }
  },
  "splits": {
    "train": {
      "count":      77516,
      "num_shards": 78,
      "shards": ["train/shard-000000.tar", "train/shard-000001.tar", ...]
    },
    "val":   { "count": 9748,  "num_shards": 10, "shards": [...] },
    "test":  { "count": 9724,  "num_shards": 10, "shards": [...] }
  }
}
```

#### Shard tar contents

Each tar contains `--shard-size` samples (default 1000). Files with the same stem form one sample (WebDataset convention):

```
shard-000000.tar
├── 000000.jpg        — screenshot (resized/re-encoded per options)
├── 000000.json       — label (see below)
├── 000001.jpg
├── 000001.json
└── ...
```

The stem is the zero-padded original `ss-N` index from the capture output.

#### Per-sample label JSON (minimal, no `--full-meta`)

```json
{
  "x":   0.3123,          // position normalized to [0, 1] via map x bounds — primary target
  "z":   0.7405,          // position normalized to [0, 1] via map z bounds — primary target
  "x_m": 4746.6,          // raw metres (east)
  "z_m": 10527.1,         // raw metres (north)
  "map": "chernarusplus"  // map identifier
}
```

#### Per-sample label JSON (with `--full-meta`)

All fields above plus a `"meta"` key containing the original capture JSON verbatim:

```json
{
  "x": 0.3123, "z": 0.7405, "x_m": 4746.6, "z_m": 10527.1, "map": "chernarusplus",
  "meta": {
    "locationIndex": 42,
    "map": "chernarusplus",
    "position":        { "x": 4746.6, "y": 182.4, "z": 10527.1 },
    "cameraDirection": { "x": 0.866,  "y": -0.09, "z": 0.5     },
    "timeOfDay": 12.0,
    "weather": { "overcast": 0.7, "rain": 0.0, "fog": 0.0, "snowfall": 0.0, "windSpeed": 3.2 }
  }
}
```

`meta.position.y` (elevation), `meta.cameraDirection`, `meta.timeOfDay`, and `meta.weather` are available for future multi-input tasks but are not used by the baseline image-only regression model.

---

## Mod Files

### `3_Game/SSCollector/SSCConstants.c`

**`SSCCameraCommand`** — Static cross-layer message bus. Written by `PlayerBase.OnRPC` in 4_World and read by `MissionGameplay.OnUpdate` in 5_Mission, because `FreeDebugCamera` lives in 5_Mission and is unreachable from 4_World.

Fields: `pending` (bool), `x / y / z` (eye position), `yaw`, `pitch`.

**`SSCRpc`** — RPC type ID constants.

| Constant | ID | Direction | Payload |
|----------|----|-----------|---------|
| `CAPTURE_META` | 20001 | client → server | `vector cameraDir` |
| `NAVIGATE` | 20002 | client → server | `int delta` (+1 / -1) |
| `SET_CAMERA` | 20003 | server → client | `float x, y, z, yaw, pitch` |
| `ADD_LOCATION` | 20004 | client → server | `int yawCount` |
| `CLEAR_LOCATIONS` | 20005 | client → server | *(none)* |
| `GENERATE_GRID` | 20006 | client → server | `int step, yawCount` |
| `RELOAD` | 20007 | client → server | *(none)* |
| `TOGGLE_GOD` | 20008 | client → server | *(none)* |
| `SET_INDEX` | 20009 | client → server | `int index` |
| `EXILE` | 20010 | client → server | *(none)* |

All RPCs use `guaranteed = true`.

---

### `3_Game/SSCollector/SSCConfig.c`

**`SSCConfig`** — Config data class, serialised to/from `config.json` by `JsonFileLoader`.

| Field | Default | Purpose |
|-------|---------|---------|
| `outputDir` | `"output"` | Subdirectory under `$profile:SSCollector/` for metadata files |
| `mapMinX` | `0.0` | Grid sweep west bound (metres) |
| `mapMaxX` | `15360.0` | Grid sweep east bound |
| `mapMinZ` | `0.0` | Grid sweep south bound |
| `mapMaxZ` | `15360.0` | Grid sweep north bound |

Defaults cover Chernarus. Edit `config.json` to target Livonia or custom maps before running `/ss-generate`.

**`SSCConfigManager`** — Static loader/saver. `Init()` runs at server startup; creates a default config if none exists.

---

### `3_Game/SSCollector/SSCLocation.c`

JSON data classes used by `JsonFileLoader<SSCLocationList>`.

```
SSCLocationList
  └── array<SSCLocation>
        ├── SSCLocationPos  (x, y, z)
        ├── cameraYaw       float
        ├── cameraPitch     float
        ├── timeOfDay       float  (0–24, fractional minutes)
        └── SSCLocationWeather (overcast, fog, rain)
```

---

### `4_World/SSCollector/SSCNavigator.c`

**`SSCNavigator`** — Static class owning `s_Locations` (`protected static ref array<ref SSCLocation>`).

| Method | Purpose |
|--------|---------|
| `Init()` | Load locations.json; called by `SSC_Reload` and `MissionServer.OnInit` |
| `Save()` | Persist in-memory list to locations.json |
| `GetCount()` | Entry count |
| `GetAt(index)` | Null-safe index lookup |
| `AppendLocation(loc)` | Append one entry |
| `ClearLocations()` | Empty the list |
| `IsClearAngle(eyePos, yawDeg, pitchDeg, maxDist, minClear, ignore)` | Returns false if raycast hits within `minClear` metres |

**Modded `PlayerBase`** — All methods are server-side unless noted.

| Method | RPC trigger | Notes |
|--------|------------|-------|
| `SSC_ApplyLocation(index)` | *(internal)* | Apply time/weather/camera for a given index without changing m_SSCLocIndex |
| `SSC_Navigate(delta)` | NAVIGATE | Advance m_SSCLocIndex ±1, call SSC_ApplyLocation |
| `SSC_GoTo(index)` | SET_INDEX | Jump directly to index (clamped), call SSC_ApplyLocation |
| `SSC_AddLocation(yawCount)` | ADD_LOCATION | Floor-snap, raycast filter, 4 presets per valid angle |
| `SSC_GenerateGrid(step, yawCount)` | GENERATE_GRID | Same as AddLocation over full map grid; skips sea |
| `SSC_ClearLocations()` | CLEAR_LOCATIONS | Wipe + save |
| `SSC_Reload()` | RELOAD | Re-reads locations.json without restart |
| `SSC_ToggleGod()` | TOGGLE_GOD | Toggle `m_SSCGodMode`; see God mode section |
| `SSC_Exile()` | EXILE | Teleport player to (mapMinX+200, mapMinZ+200) |

**God mode** (`m_SSCGodMode` bool, per-player):
- `SetAllowDamage(false)` — blocks all incoming damage
- `ModifiersManager.SetModifiers(false)` — freezes hunger, thirst, temperature, disease
- `ModifiersManager.ResetAll()` — clears broken legs, infections, etc.
- `GetBleedingManagerServer().RemoveAllSources()` — stops bleeding
- Health / blood / shock / water / energy topped to max on activation
- Toggle off restores damage and resumes modifiers

---

### `4_World/SSCollector/SSCMeta.c`

**`SSCMetaWriter.Write(player, cameraDir, locationIndex)`** — Writes one JSON file per call.

Output path: `$profile:SSCollector/{outputDir}/ss-meta-{counter}.json`

Counter is process-scoped and resets on server restart. `cameraDirection` comes from the client's `GetCurrentCameraDirection()` at capture time.

```json
{
    "locationIndex": 42,
    "map": "chernarusplus",
    "position":        { "x": 6500.0, "y": 200.3, "z": 6500.0 },
    "cameraDirection": { "x": 0.71,   "y": -0.09, "z": 0.71   },
    "timeOfDay": 12.0,
    "weather": {
        "overcast": 0.7, "rain": 0.0, "fog": 0.0,
        "snowfall": 0.0, "windSpeed": 3.2
    }
}
```

`locationIndex` is the server-side `m_SSCLocIndex` at the time of capture — used by `collect.py` to match the file to the correct screenshot.

---

### `5_Mission/SSCollector/SSCollectorMission.c`

**Modded `MissionServer.OnInit()`** — Calls `SSCConfigManager.Init()` then `SSCNavigator.Init()` at server startup.

---

### `5_Mission/SSCollector/SSCClientChat.c`

**Modded `MissionGameplay`**

Keybind inputs cached in `OnInit`: `UASSCCaptureMeta`, `UASSCPrevLocation`, `UASSCNextLocation`.

`OnEvent(ChatMessageEventTypeID)` intercepts `/ss-*` commands **before** calling `super.OnEvent()`, so they are never rendered in the HUD chat widget.

Chat commands (message is lowercased before matching):

| Command | Sender method |
|---------|--------------|
| `/ss-meta` | `SendCaptureMeta()` |
| `/ss-add [N]` | `SendAddLocation(yawCount)` |
| `/ss-generate <step> [N]` | `SendGenerateGrid(step, yawCount)` |
| `/ss-clear` | `SendClearLocations()` |
| `/ss-reload` | `SendReload()` |
| `/ss-god` | `SendToggleGod()` |
| `/ss-exile` | `SendExile()` |
| `/ss-freecam` | Toggle `FreeDebugCamera` locally (no RPC) |
| `/ss-goto <N>` | `SendSetIndex(N)` |

`OnUpdate` applies `SSCCameraCommand` when pending: places `FreeDebugCamera` at the given eye position and orientation and calls `cam.SetActive(true)`.

---

## Inputs (`data/inputs.xml`)

| Action | Category | Purpose |
|--------|----------|---------|
| `UASSCCaptureMeta` | `sscollector` | Write metadata JSON for current view |
| `UASSCPrevLocation` | `sscollector` | Navigate to previous location |
| `UASSCNextLocation` | `sscollector` | Navigate to next location |

---

## Persistent Files

All mod files under `$profile:SSCollector/` (e.g. `Documents/DayZ Other Profiles/Server/SSCollector/`).

| File | Written by | When |
|------|-----------|------|
| `config.json` | `SSCConfigManager.Init()` | First server run; edit manually to change map bounds |
| `locations.json` | `SSCNavigator.Save()` | After any generate/add/clear/reload operation |
| `output/ss-meta-N.json` | `SSCMetaWriter.Write()` | On each capture trigger; renamed to `ss-N.json` by collect.py |
| `output/ss-N.png` | `collect.py` | Screenshot taken after metadata confirmed |
| `output/ss-N.json` | `collect.py` | Renamed from ss-meta-N.json; final label file |
| `output/collect_state.json` | `collect.py` | `{ "next_index": N }` — survives Ctrl+C / crashes for resume |
