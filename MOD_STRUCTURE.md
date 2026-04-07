# SSCollector — Mod Structure

A DayZ server mod for collecting labeled screenshots to train a coordinate-recognition ML model. The player cycles through pre-generated locations; at each one, the server applies weather and time of day and orients FreeDebugCamera. The Python automation script navigates, captures metadata, takes screenshots, and saves state for overnight unattended runs.

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
├── collect.py       — Automated screenshot capture loop (Python, Windows)
├── settings.json    — Paths: logs_dir, ahk_path
├── requirements.txt — pywin32, mss, pynput
└── ...              — Other dev scripts (launch, pack, etc.)

collect.bat          — One-click launcher for collect.py
```

---

## Data Flow

### Location generation

```
Client: /ss-add [N]  or  /ss-generate <step> [N]
  → SSCClientChat sends ADD_LOCATION or GENERATE_GRID RPC to server
  → PlayerBase.OnRPC (SSCNavigator.c) dispatches to SSC_AddLocation / SSC_GenerateGrid
  → For each grid/player position:
      • SurfaceRoadY  → snap to floor height
      • IsClearAngle  → raycast (BUILDING|TERRAIN|ROADWAY) rejects wall-facing angles
      • 4 preset entries written per valid angle (pre-dawn / overcast-noon / dusk / night)
  → SSCNavigator.Save() writes $profile:SSCollector/locations.json
```

### Navigation

```
Client: UASSCNextLocation / UASSCPrevLocation keybind  — or —  /ss-goto <N>
  → SSCClientChat sends NAVIGATE (delta ±1) or SET_INDEX (absolute index) RPC
  → PlayerBase.SSC_Navigate / SSC_GoTo (SSCNavigator.c):
      • Updates m_SSCLocIndex
      • Weather.Set  → applies overcast/fog/rain server-wide for 1 hour
      • GetWorld().SetDate  → sets time of day (keeps calendar date)
      • Sends SET_CAMERA RPC  → eye position (floor+1.5m) + yaw + pitch
  → PlayerBase.OnRPC client side writes data to SSCCameraCommand statics (3_Game)
  → MissionGameplay.OnUpdate (SSCClientChat.c):
      • Detects SSCCameraCommand.pending
      • Places and activates FreeDebugCamera at the given eye position + orientation
```

**Note — player position:** The player character is NOT teleported to the shot location. `FreeDebugCamera` is positioned independently. The player only needs to be within terrain-streaming range (~1 km). Use `/ss-exile` at session start to move the character to the map corner and away from zombie spawn zones.

### Screenshot capture

```
Client: UASSCCaptureMeta keybind  or  /ss-meta
  → SSCClientChat reads GetCurrentCameraDirection()
  → Sends CAPTURE_META RPC to server
  → SSCMetaWriter.Write (SSCMeta.c) runs server-side:
      • locationIndex (current m_SSCLocIndex)
      • Player world position
      • Server-side weather actuals + time of day + world name
      • Writes $profile:SSCollector/output/ss-meta-N.json
```

### Automated capture (collect.py)

```
python scripts/collect.py  (or double-click collect.bat)

  1. Find DayZ_x64.exe window for screenshots
  2. Wait for HOME keypress (user focuses DayZ first)
  3. Send /ss-exile + /ss-freecam via AutoHotkey
  4. For each index in [start, total):
       a. Skip if ss-<N>.png + ss-<N>.json already exist (resume safety)
       b. Send /ss-goto <N> via AHK
       c. Wait --delay seconds (default 1.5s) for lighting to settle
       d. Snapshot existing ss-meta-*.json files
       e. Send /ss-meta via AHK
       f. Poll for new ss-meta-*.json with matching locationIndex (8s timeout)
       g. Screenshot DayZ window → ss-<N>.png
       h. Rename matched JSON → ss-<N>.json
       i. Save state to output/collect_state.json
  5. HOME toggles pause/resume mid-run; Ctrl+C saves state and exits cleanly
```

---

## Files

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

**Generation presets** (4 entries per valid yaw angle, preset is the outer loop so time only changes at group boundaries during playback):

| Preset | Time | Overcast | Fog | Rain | Notes |
|--------|------|---------|-----|------|-------|
| Pre-dawn | 05:30 | 0.0 | 0.0 | 0.0 | Deep blue sky |
| Overcast noon | 12:00 | 0.7 | 0.0 | 0.0 | Flat, shadowless |
| Dusk | 19:00 | 0.0 | 0.0 | 0.0 | Golden hour |
| Night | 22:00 | 0.0 | 0.0 | 0.0 | Dark |

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
| `output/collect_state.json` | `collect.py` | `{ "next_index": N }` — survives Ctrl+C / crashes for resume |
