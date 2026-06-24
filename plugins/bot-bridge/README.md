# VPX Bot Bridge plugin

Bridges a running Visual Pinball X table to an external bot process:
**telemetry out** (what the ball and table are doing) and **commands in**
(flippers, plunger, nudge). Table-agnostic, portable to the standalone build.

This is the foundation component for an AI pinball league. It contains **no game
understanding** — only sensors and actuators. See `../../FINDINGS.md` for the
full Task-0 investigation and the architecture rationale.

> **Status: Milestone 2 (full sensor + command surface).** Static geometry +
> lamp directory at load; per physics tick: all balls, full flipper state,
> plunger, lamps, and nudge/tilt; full digital command set + analog nudge/plunger.
> Schema is versioned (**v2**). Remaining/known gaps are listed at the end.

## Architecture (Option 1: additive host-API getters)

Plugins are C-ABI-isolated from the engine, so the sanctioned plugin API exposes
**commands in** (`SetActionState`) and a **physics-tick hook** (`OnUpdatePhysics`)
but not ball/part/geometry state. A small, additive, **read-only** telemetry
surface was added to the engine's plugin API; the bot-bridge plugin itself stays
a pure SDK plugin.

- `plugins/plugins/VPXPlugin.h` — telemetry structs + getters on `VPXPluginAPI`:
  `GetBalls`, `GetFlippers`, `GetPlungers`, `GetLamps`, `GetLampDescriptors`,
  `GetGeometry`, `GetTableState`.
- `src/core/VPXPluginAPIImpl.cpp` — implementations reading `g_pplayer`.
- Small public const accessors added to `Flipper` (angle speed / solenoid / EOS),
  `Plunger` (`GetMover`), and `PhysicsEngine` (`GetTableVelocity`).
- `src/input/InputManager.cpp` — the analog plunger/nudge override stubs were
  implemented (previously `// FIXME` no-ops).

## Build

**Windows (MSBuild, this dev machine):**
```
msbuild .build/vsproject/vpx.vcxproj                /p:Configuration=Debug_BGFX /p:Platform=x64   # engine
msbuild .build/vsproject/plugin-bot-bridge.vcxproj  /p:Configuration=Debug_BGFX /p:Platform=x64   # plugin
```
**Standalone (Linux/Mac, CMake):** registered in `make/CMakeLists_plugins.txt`
(`CMakeLists_plugin_BotBridge.txt`); builds as a `MODULE` next to the binary.

## Enable & run

The plugin must be enabled in `VPinballX.ini` (the default-enable did not apply in
testing — set it explicitly):
```
[Plugin.BotBridge]
Enable = 1
```
Then launch a table in play mode and connect a bot:
```
VPinballX_BGFX64.exe -Play "assets/exampleTable.vpx"
python plugins/bot-bridge/test/m2_check.py     # schema + command verification
python plugins/bot-bridge/test/latency_test.py # round-trip latency
```

## Transport

- **Loopback TCP**, `127.0.0.1:13501`, `TCP_NODELAY`.
- **NDJSON**: one JSON object per line. Non-blocking on the physics thread (the
  callback snapshots + notifies; a worker thread does all socket I/O).
- Two frame types, distinguished by `"type"`:
  - `"static"` — sent once on game start, and to every newly-connected client.
  - `"state"` — sent every physics update (~1 kHz best-effort).

All spatial values use the **VPX table frame**: VPU length units (50 VPU =
1.0625" = 26.9875 mm; 1 VPU = 0.53975 mm), origin **top-left**, **+X right**,
**+Y down-table toward the drain**, **+Z up** (playfield surface z = 0). Linear
velocity is **VPU per VP-tick** (1 VPT = 0.01 s) — ×100 for VPU/s. Angles are
degrees. Angular speeds are per VP-tick.

## `"static"` frame (schema v2)

```jsonc
{
  "type": "static", "v": 2,
  "geometry": [ { "type": 1, "name": "LeftFlipper", "x":278,"y":1803,"z":..,
                  "a":..,"b":..,"c":..,"d":..,"ex":..,"ey":..,"ez":.. }, ... ],
  "lamps":    [ { "i": 0, "name": "gi9", "mode": 1 }, ... ]   // index -> name directory
}
```

`type` is the VPX `ItemTypeEnum`. The `x,y,z` / `a,b,c,d` / `ex,ey,ez` fields are
**type-specific**:

| type | name | x,y,z | a | b | c | d | ex,ey,ez |
|---|---|---|---|---|---|---|---|
| 1 | flipper | pivot, height | lengthMax | baseRadius | endRadius | startAngle° | endAngle°(ex) |
| 12 | ramp | **entrance** x,y, z=heightBottom | widthBottom | widthTop | rampType | — | **exit** x,y, z=heightTop |
| 0 | surface | bbox-min x,y | heightBottom | heightTop | — | — | bbox-max x,y (ex,ey) |
| 8 | kicker (drains/saucers) | center, hitHeight | radius | kickerType | orientation° | — | — |
| 6 | trigger | center, hitHeight | radius | triggerShape | rotation° | — | — |
| 10 | gate | center, height | length | rotation° | angleMin° | angleMax° | — |
| 11 | spinner | center, height | length | rotation° | angleMin° | angleMax° | — |
| 22 | hittarget | position | rotZ° | targetType | dropped(0/1) | — | size (ex,ey,ez) |
| 5 | bumper | center | radius | — | — | — | — |
| 19 | primitive | position | — | — | — | — | size (ex,ey,ez) |

(Brief-required items: ramp **entrance** = `x,y,z`; flipper **pivot** = `x,y`,
**length** = `a`; kicker/**drain** locations = kicker `x,y`.)

## `"state"` frame (schema v2)

One per physics update.

```jsonc
{
  "type": "state", "v": 2, "tick": 361, "time": 0.45,
  "balls":    [ { "id":0,"x":..,"y":..,"z":..,"vx":..,"vy":..,"vz":..,
                  "avx":..,"avy":..,"avz":..,"r":25.0 } ],   // every live ball (multiball-safe)
  "flippers": [ { "i":0,"angle":120.5,"angleSpeed":0.0,"solenoid":0,"eos":0 }, ... ],
  "plungers": [ { "i":0,"pos":0.167,"posVPU":1943.3,"speed":-0.026,"rest":0.167 } ],
  "lamps":    [ { "i":0,"mode":1,"lit":1,"in":0.85 }, ... ],  // by index (see static lamp directory)
  "nudge":    { "ax":0,"ay":0, "vx":0,"vy":0, "dx":0,"dy":0,
                "tilt":0,"slam":0,"plumbSim":1,"plumbCount":0 }
}
```

Field notes:
- **flipper** `angleSpeed` deg/VP-tick; `solenoid` = coil energized (button); `eos`
  = resting against the end stop.
- **plunger** `pos` normalized 0..1 (0 forward/released, 1 fully pulled); `posVPU`
  raw rod tip; `speed` VPU/VP-tick (>0 retracting, <0 firing); `rest` park fraction
  (note "at rest" is `rest`, not 0).
- **lamp** `mode` configured {0 off, 1 on, 2 blinking}; `lit` instantaneous on/off
  (derived from live emission, so it captures dim + blink phase); `in` live faded
  intensity (relative, can exceed 1).
- **nudge** `ax,ay` applied acceleration VPU/VPT²; `vx,vy` table velocity VPU/VPT;
  `dx,dy` visual shake displacement VPU (screen frame, y flipped); `tilt`/`slam`
  current input booleans; `plumbCount` monotonic mechanical-tilt event count.

## Commands (one NDJSON object per line)

```jsonc
{ "cmd": "flip_left",    "press": 1 }   // press / release distinct -> cradling
{ "cmd": "flip_right",   "press": 0 }
{ "cmd": "launch",       "press": 1 }   // plunger / launch action (table-defined)
{ "cmd": "nudge_left",   "press": 1 }   // digital nudge (also nudge_center / nudge_right)
{ "cmd": "tilt",         "press": 1 }
{ "cmd": "nudge_accel",  "x": 0.5, "y": 0.0 }  // analog nudge acceleration override
{ "cmd": "plunger_pos",  "value": 0.9 }        // analog plunger position 0..1
```

Digital commands go through the sanctioned `SetActionState` (the same path a real
keypress takes) → table-agnostic. **Verified working**: flip L/R, digital nudge
(produces real table acceleration), analog `nudge_accel`. **Caveats**: analog
`plunger_pos` only moves the rod on tables with the *Mechanical Plunger* option
enabled; analog overrides are a **sticky one-way latch** in the engine (once the
bot drives a channel, keyboard/hardware input to it is suppressed until table
reload) — the bot should drive it continuously (send 0 = no input).

## Measured round-trip latency

`send flip → applied next tick → flipper rotates → angle returned in telemetry`:
median **~5 ms** (min ~4.5), 25 trials. The stream itself is ~1 kHz; the rest is
flipper coil ramp. Well under one 60 Hz frame.

## Known gaps / next

- **Discrete switch/hit event stream** (e.g. spinner spin, target drop as events)
  is not yet implemented — currently inferable from per-tick state. The
  least-invasive engine hook (a ring buffer drained on `OnUpdatePhysics`) is
  designed in `FINDINGS.md`/the M2 investigation and is the next addition.
- **Score / ball-in-play** is not exposed — no uniform engine representation
  (table-/ROM-specific; would come from a DMD/segment OCR or per-table config).
- Lamp `lit` is derived from live intensity (not the blink-pattern boolean, which
  could trip an MSVC-debug assert when polled every tick).
- Geometry reduces polygons (surface outline, full ramp curve) to bbox / endpoint
  pairs; full control-point export could be a later descriptor.

## Out of scope (downstream, not built here)

Bot/strategy logic, the match harness, recording, rule-sheets, per-table shot
maps. This component is sensors + actuators only.
