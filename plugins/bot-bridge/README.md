# VPX Bot Bridge plugin

Bridges a running Visual Pinball X table to an external bot process:
**telemetry out** (what the ball and table are doing) and **commands in**
(flippers, plunger, nudge). Table-agnostic, portable to the standalone build.

This is the foundation component for an AI pinball league. It contains **no game
understanding** — only sensors and actuators. See `../../FINDINGS.md` for the
full Task-0 investigation and the architecture rationale.

> **Status: Milestone 1 (smallest end-to-end loop).** Streams tick + all balls'
> position/velocity/spin + flipper angles every physics update, and accepts
> flip-left / flip-right commands. Measured round-trip latency below. Milestone 2
> (full sensor + command surface, static geometry, versioned binary schema) is
> the next step.

## Architecture (Option 1: additive host-API getter)

The sanctioned VPX plugin API already exposes **commands in** (`SetActionState`)
and a **physics-tick hook** (`OnUpdatePhysics`), but it does **not** expose
per-ball physics state or geometry, and plugins are C-ABI-isolated from the
engine (they cannot reach `g_pplayer`). So a tiny, additive, read-only telemetry
surface was added to the engine's plugin API:

- `plugins/plugins/VPXPlugin.h` — new `VPXBallState` / `VPXFlipperState` structs
  and `GetBalls()` / `GetFlippers()` function pointers on `VPXPluginAPI`.
- `src/core/VPXPluginAPIImpl.{h,cpp}` — implementations that read
  `g_pplayer->m_vball` and the flipper parts.

The plugin itself (`botbridge.cpp`) stays a pure SDK plugin: it fetches the VPX
API over the message bus, subscribes to `OnUpdatePhysics`, snapshots telemetry,
and applies commands via `SetActionState`. All blocking socket I/O runs on
worker threads; the physics callback only does a cheap snapshot + notify.

## Build

**Windows (MSBuild, this dev machine):**
```
msbuild .build/vsproject/vpx.vcxproj            /p:Configuration=Debug_BGFX /p:Platform=x64   # engine (adds GetBalls/GetFlippers)
msbuild .build/vsproject/plugin-bot-bridge.vcxproj /p:Configuration=Debug_BGFX /p:Platform=x64   # plugin
```
The plugin's PostBuildEvent deploys `plugin-bot-bridge64.dll` + `plugin.cfg` into
`.build/bin/vpx/<config>/plugins/bot-bridge/`.

**Standalone (Linux/Mac, CMake):** the plugin is registered in
`make/CMakeLists_plugins.txt` (`CMakeLists_plugin_BotBridge.txt`) and builds as a
`MODULE` shared library next to the binary, no engine headers required.

## Enable

The plugin must be enabled in `VPinballX.ini` (the default-enable did not apply
in testing — set it explicitly):
```
[Plugin.BotBridge]
Enable = 1
```

## Run

Launch a table in play mode; the plugin opens a TCP server on connect:
```
VPinballX_BGFX64.exe -Play "assets/exampleTable.vpx"
python plugins/bot-bridge/test/latency_test.py --trials 25
```

## Transport

- **Loopback TCP**, `127.0.0.1:13501`, `TCP_NODELAY` (no Nagle).
- **NDJSON**: one JSON object per line. Telemetry is one line per physics update
  (~1 kHz best-effort); commands are one line each, any time.
- Chosen for M1 because it is non-blocking on the physics thread, language-neutral,
  and lets us measure latency immediately. Shared memory is the documented upgrade
  path (see FINDINGS.md, Transport).

## Telemetry schema (v1)

One object per physics update. All spatial values use the **VPX table frame**:
VPU length units (50 VPU = 1.0625" = 26.9875 mm; 1 VPU = 0.53975 mm), origin at
the playfield **top-left**, **+X right**, **+Y down-table toward the drain**,
**+Z up** (playfield surface at z = 0). Linear velocity is in **VPU per VP time
tick** (1 VPT = 0.01 s) — multiply by 100 for VPU/s.

```jsonc
{
  "v": 1,                       // schema version
  "tick": 59524,                // plugin physics-update counter (monotonic, resets at game start)
  "time": 67.717580,            // engine game time, seconds
  "balls": [                    // EVERY live ball (multiball-safe); may be empty
    {
      "id": 0,                  // stable unique id for the ball's lifetime
      "x": 903.0082, "y": 1918.3496, "z": 25.0179,   // position, VPU
      "vx": 0.0, "vy": -0.02368, "vz": 0.18086,       // linear velocity, VPU/VPT
      "avx": 0.00034, "avy": 0.0, "avz": 0.0,         // angular velocity, rad/VPT (derived: L / inertia)
      "r": 25.000                                      // radius, VPU
    }
  ],
  "flippers": [                 // all flippers, in table part order
    { "i": 0, "angle": 120.5000 },   // current angle, degrees
    { "i": 1, "angle": -120.5000 }
  ]
}
```

## Command schema (v1)

One JSON object per line, sent any time. Press and release are distinct events
(so a bot can hold / cradle):

```jsonc
{ "cmd": "flip_left",  "press": 1 }   // left flipper down (press)
{ "cmd": "flip_left",  "press": 0 }   // left flipper up   (release)
{ "cmd": "flip_right", "press": 1 }
{ "cmd": "flip_right", "press": 0 }
```

Commands are applied on the next physics update via the sanctioned
`VPXPluginAPI::SetActionState` (the same path a real keypress takes), so they are
table-agnostic. (Milestone 2 adds plunger / nudge and a versioned binary frame.)

## Measured round-trip latency (Milestone 1)

`send "flip_left" → plugin applies it on the next physics tick → the left flipper
starts rotating → the new angle appears in a telemetry line we receive`.

Example table, Debug_BGFX x64, 25 trials, 2° movement threshold:

| metric | ms |
|---|---|
| min | 4.52 |
| median | 4.98 |
| mean | 5.16 |
| max | 7.20 |

The telemetry stream itself is ~1 kHz (consecutive ticks ~1 ms apart), so the
~5 ms is dominated by the flipper coil ramping to a visible 2° of travel, not by
transport. This caps how fast any bot built on the bridge can react — comfortably
under one 60 Hz render frame (16.7 ms).

## Out of scope (downstream, not built here)

Bot/strategy logic, the match harness, recording, rule-sheets, per-table shot
maps. This component is sensors + actuators only.
