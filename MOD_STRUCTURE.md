# SSCollector — Mod Structure

An automated screenshot metadata collection tool for DayZ. It captures labeled JSON metadata (position, camera direction, time, weather) on demand, intended for use in building machine learning datasets.

---

## Directory Layout

```
mod/
├── mod.cpp                          # Mod identity (name, author, version)
├── Scripts/
│   ├── config.cpp                   # Mod registration and script module declarations
│   ├── data/
│   │   └── inputs.xml               # Input action definitions (keybindings)
│   ├── 3_Game/SSCollector/
│   │   ├── SSCConfig.c              # Config data class + file load/save manager
│   │   └── SSCConstants.c           # Shared RPC type constants
│   ├── 4_World/SSCollector/
│   │   └── SSCMeta.c                # Server-side metadata writer + RPC receiver
│   └── 5_Mission/SSCollector/
│       ├── SSCClientChat.c          # Client-side input handling + RPC sender
│       └── SSCollectorMission.c     # Server mission init hook
```

---

## Data Flow

```
[Client]                                    [Server]
  │                                            │
  │  Key press or /ss-meta chat command        │
  │  ──────────────────────────────────►       │
  │  SendCaptureMeta()                         │
  │    - reads camera direction                │
  │    - sends ScriptRPC (id 20001)            │
  │                                            │  PlayerBase.OnRPC()
  │                                            │    - reads cameraDir from RPC
  │                                            │    - calls SSCMetaWriter.Write()
  │                                            │      - reads player position
  │                                            │      - reads time, world, weather
  │                                            │      - writes ss-meta-{n}.json
```

---

## Files

### `mod.cpp`
Metadata only. Sets `name`, `author`, `version`, and `overview` for the mod.

---

### `Scripts/config.cpp`
Registers the mod with DayZ's engine.

- **`CfgPatches.SSCollector_Scripts`** — Declares the script patch, depends on `DZ_Data` and `DZ_Scripts`.
- **`CfgMods.SSCollector`** — Declares the mod, points to the `inputs.xml` file, and defines three script modules:
  - `gameScriptModule` → `3_Game`
  - `worldScriptModule` → `4_World`
  - `missionScriptModule` → `5_Mission`

---

### `Scripts/data/inputs.xml`
Defines three input actions under the `sscollector` category:

| Action name         | Label               | Purpose                              |
|---------------------|---------------------|--------------------------------------|
| `UASSCCaptureMeta`  | Capture Meta        | Triggers metadata capture            |
| `UASSCPrevLocation` | Previous location   | Navigate to previous teleport point  |
| `UASSCNextLocation` | Next location       | Navigate to next teleport point      |

> `UASSCPrevLocation` and `UASSCNextLocation` are defined but not yet wired to any logic.

---

### `3_Game/SSCollector/SSCConstants.c`
Defines shared constants used on both client and server.

**`SSCRpc`**
- `CAPTURE_META = 20001` — RPC type ID for the metadata capture call.

---

### `3_Game/SSCollector/SSCConfig.c`
Config persistence system.

**`SSCConfig`** — Data object holding mod settings.
- `string outputDir` (default: `"output"`) — Subdirectory under `$profile:SSCollector/` where JSON files are written.

**`SSCConfigManager`** — Static manager for loading/saving `SSCConfig`.
- `CONFIG_PATH = "$profile:SSCollector/config.json"`
- `Init()` — Creates the profile directory if needed, then loads config.json or writes a default one.
- `Save()` — Serializes the current config to config.json via `JsonFileLoader`.
- `Get()` — Returns the current config object (creates a default if none is loaded).

---

### `4_World/SSCollector/SSCMeta.c`
Server-side metadata collection and file writing.

**`SSCMetaWriter`**
- `s_Counter` — Static integer, incremented on each write to produce unique filenames.
- `Write(PlayerBase player, vector cameraDir)` — Collects data and writes a JSON file:
  - Output path: `$profile:SSCollector/{outputDir}/ss-meta-{counter}.json`
  - JSON fields:
    - `map` — world name (`GetGame().GetWorldName()`)
    - `position` — `{x, y, z}` player world position
    - `cameraDirection` — `{x, y, z}` camera direction sent from client
    - `timeOfDay` — game clock value (`GetGame().GetDayTime()`)
    - `weather` — `{overcast, rain, fog, snowfall, windSpeed}` (all actual values, not forecast)

**Modded `PlayerBase`**
- `OnRPC()` — Intercepts RPCs on the server. When `rpc_type == SSCRpc.CAPTURE_META`, reads `cameraDir` from the context and calls `SSCMetaWriter.Write()`.

---

### `5_Mission/SSCollector/SSCollectorMission.c`
Server mission hook.

**Modded `MissionServer`**
- `OnInit()` — Calls `SSCConfigManager.Init()` at server startup to load or create the config file.

---

### `5_Mission/SSCollector/SSCClientChat.c`
Client-side input handling and RPC dispatch.

**Modded `MissionGameplay`**
- `m_SSCCaptureInput` — Cached reference to the `UASSCCaptureMeta` input action.
- `OnInit()` — Fetches and stores the capture input via `GetUApi().GetInputByName("UASSCCaptureMeta")`.
- `OnUpdate(float timeslice)` — Every frame: checks `m_SSCCaptureInput.LocalPress()` and calls `SendCaptureMeta()` on press.
- `OnEvent()` — Listens for `ChatMessageEventTypeID`. If the chat message equals `/ss-meta` (case-insensitive), calls `SendCaptureMeta()`.
- `SendCaptureMeta()` — Reads the current camera direction, creates a `ScriptRPC` with `SSCRpc.CAPTURE_META`, writes the direction vector, and sends it reliably to the server.

---

## Configuration

The config file is auto-created at first run:

**`$profile:SSCollector/config.json`**
```json
{
    "outputDir": "output"
}
```

Captured metadata files are written to:
```
$profile:SSCollector/{outputDir}/ss-meta-{n}.json
```

---

## Capture Triggers

Metadata capture can be triggered two ways, both client-side:

1. **Keybind** — The `UASSCCaptureMeta` action (configurable in DayZ controls menu under the `sscollector` category).
2. **Chat command** — Typing `/ss-meta` in game chat.
