# SSCollector — Mod Structure

A DayZ server mod for collecting labeled screenshots to train a coordinate-recognition ML model. The player cycles through pre-generated locations; at each one, the server teleports them, applies weather and time of day, and orients the camera. The player captures a metadata JSON, takes a screenshot, and moves on.

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
      • 4 preset entries written per valid angle (morning / midday / afternoon / evening)
  → SSCNavigator.Save() writes $profile:SSCollector/locations.json
```

### Navigation

```
Client: UASSCNextLocation / UASSCPrevLocation keybind
  → SSCClientChat sends NAVIGATE RPC (delta +1 / -1)
  → PlayerBase.SSC_Navigate (SSCNavigator.c):
      • Advances m_SSCLocIndex (wraps around, first press always lands on index 0)
      • SetPosition           → teleports player
      • Weather.Set           → applies overcast/fog/rain server-wide for 1 hour
      • GetWorld().SetDate    → sets time of day (keeps calendar date)
      • Sends SET_CAMERA RPC  → eye position (floor+1.5m) + yaw + pitch
  → PlayerBase.OnRPC client side writes yaw/pitch to SSCCameraCommand statics (3_Game)
  → MissionGameplay.OnUpdate (SSCClientChat.c):
      • Detects SSCCameraCommand.pending
      • Stores yaw/pitch, sets m_SSCOrientActive = true
      • Calls player.SetOrientation(Vector(yaw, pitch, 0)) every frame while active
```

**Known limitation — camera pitch:** DayZ's first-person camera pitch is driven by an engine-internal aim accumulator that is not writable from script. `SetOrientation` sets the entity transform but the accumulator fights it, causing visible oscillation. Yaw (horizontal) is mostly stable. The eye position sent in SET_CAMERA is stored in `SSCCameraCommand` but currently unused — `FreeDebugCamera` and `GetFreeDebugCamera()` are not available in standard (non-diagnostic) builds.

### Screenshot capture

```
Client: UASSCCaptureMeta keybind  or  /ss-meta
  → SSCClientChat reads GetCurrentCameraDirection()
  → Sends CAPTURE_META RPC to server
  → SSCMetaWriter.Write (SSCMeta.c) runs server-side:
      • Player world position
      • Server-side weather actuals + time of day + world name
      • Writes $profile:SSCollector/output/ss-meta-N.json
```

---

## Files

### `3_Game/SSCollector/SSCConstants.c`

**`SSCCameraCommand`** — Static cross-layer message bus. Written by `PlayerBase.OnRPC` in 4_World and read by `MissionGameplay.OnUpdate` in 5_Mission, because `FreeDebugCamera` lives in 5_Mission and is unreachable from 4_World.

Fields: `pending` (bool), `x / y / z` (eye position, currently unused), `yaw`, `pitch`.

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
| `SSC_Navigate(delta)` | NAVIGATE | Teleport + weather + time + SET_CAMERA reply |
| `SSC_AddLocation(yawCount)` | ADD_LOCATION | Floor-snap, raycast filter, 4 presets per valid angle |
| `SSC_GenerateGrid(step, yawCount)` | GENERATE_GRID | Same as AddLocation over full map grid; skips sea |
| `SSC_ClearLocations()` | CLEAR_LOCATIONS | Wipe + save |
| `SSC_Reload()` | RELOAD | Re-reads locations.json without restart |
| `SSC_ToggleGod()` | TOGGLE_GOD | Toggle `m_SSCGodMode`; see God mode section |

**Generation presets** (4 entries per valid yaw angle):

| Preset | Time | Overcast | Fog | Rain |
|--------|------|---------|-----|------|
| Morning | 08:00 | 0.1 | 0.1 | 0.0 |
| Midday | 12:00 | 0.0 | 0.0 | 0.0 |
| Afternoon | 15:00 | 0.55 | 0.0 | 0.0 |
| Evening | 18:30 | 0.2 | 0.1 | 0.0 |

**God mode** (`m_SSCGodMode` bool, per-player):
- `SetAllowDamage(false)` — blocks all incoming damage
- `ModifiersManager.SetModifiers(false)` — freezes hunger, thirst, temperature, disease
- `ModifiersManager.ResetAll()` — clears broken legs, infections, etc.
- `GetBleedingManagerServer().RemoveAllSources()` — stops bleeding
- Health / blood / shock / water / energy topped to max on activation
- Toggle off restores damage and resumes modifiers

---

### `4_World/SSCollector/SSCMeta.c`

**`SSCMetaWriter.Write(player, cameraDir)`** — Writes one JSON file per call.

Output path: `$profile:SSCollector/{outputDir}/ss-meta-{counter}.json`

```json
{
    "map": "chernarusplus",
    "position":        { "x": 6500.0, "y": 200.3, "z": 6500.0 },
    "cameraDirection": { "x": 0.71,   "y": -0.09, "z": 0.71   },
    "timeOfDay": 12.0,
    "weather": {
        "overcast": 0.0, "rain": 0.0, "fog": 0.0,
        "snowfall": 0.0, "windSpeed": 3.2
    }
}
```

Counter is process-scoped and resets on server restart. `cameraDirection` comes from the client's `GetCurrentCameraDirection()` at capture time.

---

### `5_Mission/SSCollector/SSCollectorMission.c`

**Modded `MissionServer.OnInit()`** — Calls `SSCConfigManager.Init()` then `SSCNavigator.Init()` at server startup.

---

### `5_Mission/SSCollector/SSCClientChat.c`

**Modded `MissionGameplay`**

Keybind inputs cached in `OnInit`: `UASSCCaptureMeta`, `UASSCPrevLocation`, `UASSCNextLocation`.

Camera orientation state: `m_SSCOrientActive`, `m_SSCOrientYaw`, `m_SSCOrientPitch` — set when `SSCCameraCommand.pending` is true; `SetOrientation` called every frame while active.

Chat commands parsed in `OnEvent(ChatMessageEventTypeID)` (message lowercased before matching):

| Command | Sender method |
|---------|--------------|
| `/ss-meta` | `SendCaptureMeta()` |
| `/ss-add [N]` | `SendAddLocation(yawCount)` |
| `/ss-generate <step> [N]` | `SendGenerateGrid(step, yawCount)` |
| `/ss-clear` | `SendClearLocations()` |
| `/ss-reload` | `SendReload()` |
| `/ss-god` | `SendToggleGod()` |

---

## Inputs (`data/inputs.xml`)

| Action | Category | Purpose |
|--------|----------|---------|
| `UASSCCaptureMeta` | `sscollector` | Write metadata JSON for current view |
| `UASSCPrevLocation` | `sscollector` | Navigate to previous location |
| `UASSCNextLocation` | `sscollector` | Navigate to next location |

---

## Persistent Files

All under `$profile:SSCollector/` (e.g. `Documents/DayZ Other Profiles/Server/SSCollector/`).

| File | Written by | When |
|------|-----------|------|
| `config.json` | `SSCConfigManager.Init()` | First server run; edit manually to change map bounds |
| `locations.json` | `SSCNavigator.Save()` | After any generate/add/clear/reload operation |
| `output/ss-meta-N.json` | `SSCMetaWriter.Write()` | On each capture trigger |
