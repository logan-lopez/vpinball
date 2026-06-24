# VPX Bot Bridge — FINDINGS (Task 0 + Transport Decision)

**Scope:** This document answers Task 0 of the *VPX Bot Bridge — Plugin Build Brief* and the
Task 1 transport decision. It is the result of reading the `vpinball` source directly (not
guessing the API). Every load-bearing claim carries a `file:line` citation. Where the
sanctioned mechanism cannot do something, it is flagged with the fallback, per the brief.

**Method:** 13 parallel source investigations + 4 adversarial cross-checks of the
highest-stakes claims (all-ball access, actuation, physics-tick hook, coordinate frame),
plus first-hand reads of the SDK headers, the `helloworld`/`remote-control` plugins, the
build files, and the engine globals.

---

## 0. TL;DR — the one decision that shapes everything

The VPX plugin system is real, well-structured, and **cleanly solves "commands IN"**
(flippers / plunger button / digital nudge) and the **physics-tick timing hook**, all through
a sanctioned, table-agnostic, portable **C message-bus API** — no synthetic keystrokes, no
engine fork.

It does **not** solve "telemetry OUT". The sanctioned plugin API exposes **zero per-ball
physics state and zero table geometry**. That data exists in the engine (exact member names
below) but is unreachable from a plugin, because **plugins are C-ABI isolated**: they are
loaded with `SDL_LoadObject`, their build targets include only `plugins/` + `third-party/`
(never `src/`), and they link no engine library. This is true even for the static-link build
variant.

So the brief's two hard constraints are in genuine tension:
- *"Report what the ball and table are doing"* (needs ball + geometry telemetry), vs.
- *"Add a plugin; don't patch core."*

**Resolving that tension is a decision for the project owner.** Section 11 lays out the three
options with tradeoffs and a recommendation. Everything else in Task 0 is settled and
favorable.

| Capability | Reachable from a pure SDK plugin today? | Mechanism |
|---|---|---|
| Flip left/right (press + release, cradling) | ✅ Yes | `VPXPluginAPI::SetActionState` |
| Launch ball (digital plunger pull→fire) | ✅ Yes | `SetActionState(VPXACTION_LaunchBall, …)` |
| Digital nudge (left / center / right) | ✅ Yes | `SetActionState(VPXACTION_*Nudge, …)` |
| Analog plunger position / analog nudge vector | ❌ No | API exists but routes to `// FIXME` no-op stubs |
| Per-physics-tick callback (~1 kHz) | ✅ Yes (best-effort) | `OnUpdatePhysics` event (nullptr payload) |
| Game time, table width/height | ✅ Yes | `GetGameTime`, `GetTableInfo` |
| DMD / segment / lamp / switch frames | ⚠️ Only if a controller (PinMAME/B2S) is loaded | `ControllerPlugin.h` device/display sources |
| **Per-ball position / velocity / spin / radius** | ❌ **No** | not in the SDK; lives in `g_pplayer->m_vball` |
| **Table geometry (ramps/flippers/kickers/…)** | ❌ **No** | not in the SDK; lives in `PinTable::m_vedit` |
| Score / ball-in-play (uniform) | ❌ No | no engine representation; table-/ROM-specific |

---

## 1. Build validation status (Task 0, step 1)

- **The build works — proven by prebuilt artifacts in this checkout.** The engine binary
  `.build/bin/vpx/Debug_BGFX-x64/VPinballX_BGFX64.exe` (~49 MB) and 18 deployed plugins
  under `.build/bin/vpx/Debug_BGFX-x64/plugins/<name>/` (each with `plugin-<name>64.dll` +
  `plugin.cfg`) confirm the toolchain has produced a working engine + loadable plugins.
- **Two parallel build systems exist** (there is **no** `CMakePresets.json`):
  1. **Windows dev (this machine): MSBuild** — `make/*.vcxproj` + `make/VisualPinball.sln`
     (classic, VS2019/2022) / `make/VisualPinball.slnx` (VS2026), generated into
     `.build/vsproject/` by `make/create_vs_solution.bat`.
  2. **CMake** — `make/CMakeLists_*.txt`, used by CI, `make/win_build.bat`, and all
     standalone (Linux/Mac/iOS/Android) builds. Per-platform root files like
     `make/CMakeLists_bgfx-windows-x64.txt` `include()` `make/CMakeLists_plugins.txt`.
- **A plugin builds incrementally — no full engine rebuild required** (a plugin target is an
  independent `MODULE` shared library / `DynamicLibrary` `.vcxproj`, linking only SDK
  headers; `make/CMakeLists_plugin_HelloWorld.txt`).
- **Not yet done by me:** a clean from-scratch rebuild. This is deferred to the
  build-validation / Milestone-1 step (and the chosen architecture in §11 changes whether an
  engine rebuild is even needed). The recipe to build is in §12.

---

## 2. Q1 — Plugin mechanism

**Model: a generic C message bus (`MsgPlugin`) + a VPX-specific API layer (`VPXPlugin`).** A
plugin is a separate shared library; it reaches the host **only** through the message bus and
the C-ABI function-pointer structs it fetches over that bus. It never links engine objects.

### Entry/exit contract
A plugin exports two C functions named `<id>PluginLoad` / `<id>PluginUnload`, where `<id>` is
the `plugin.cfg` id verbatim (so static linking can't collide) —
[MsgPlugin.h:49-52](plugins/plugins/MsgPlugin.h:49). Real signatures
([helloworld.cpp:38-60](plugins/helloworld/helloworld.cpp:38)):
```cpp
MSGPI_EXPORT void MSGPIAPI HelloWorldPluginLoad(const uint32_t sessionId, const MsgPluginAPI* api);
MSGPI_EXPORT void MSGPIAPI HelloWorldPluginUnload();
```
`MSGPI_EXPORT` = `extern "C" __declspec(dllexport)` (MSVC) / `__attribute__((visibility("default")))`
(GCC) — [MsgPlugin.h:79-90](plugins/plugins/MsgPlugin.h:79). The loader resolves the symbols
by string name `m_id + "PluginLoad"` — [MsgPluginManager.cpp:485-527](plugins/plugins/MsgPluginManager.cpp:485).
On unload, every `GetMsgID`→`ReleaseMsgID`, every `SubscribeMsg`→`UnsubscribeMsg`, and a
`FlushPendingCallbacks` are mandatory (asserted) — [MsgPlugin.h:60-65](plugins/plugins/MsgPlugin.h:60).

### Message API (`MsgPluginAPI`, [MsgPlugin.h:207-224](plugins/plugins/MsgPlugin.h:207))
`GetMsgID(namespace,name)` interns a `(namespace, name)` string pair to an int id;
`SubscribeMsg` / `UnsubscribeMsg` / `BroadcastMsg` / `SendMsg` move messages;
`RunOnMainThread` / `FlushPendingCallbacks` marshal work to the main thread. Namespaces:
`"MsgPlugin"`, `"VPX"`, `"Controller"`, `"Scriptable"`, `"Logging"`.

**Fetch a service API** by broadcasting a "GetAPI" message with a pointer-to-pointer the host
fills — [remote-control RemoteControl.cpp:492](plugins/remote-control/RemoteControl.cpp:492):
```cpp
msgApi->BroadcastMsg(endpointId, msgApi->GetMsgID(VPXPI_NAMESPACE, VPXPI_MSG_GET_API), &vpxApi);
```
Host side fills it in [VPXPluginAPIImpl.cpp:766-771](src/core/VPXPluginAPIImpl.cpp:766).

### Threading (load-bearing for the bridge)
The bus is **single-threaded and not thread-safe**: `GetMsgID/Subscribe/Broadcast/SendMsg`
all `assert(this_thread == m_apiThread)` ([MsgPluginManager.cpp:123-211](plugins/plugins/MsgPluginManager.cpp:123)).
The **only** methods callable from a worker thread are `RunOnMainThread` and
`FlushPendingCallbacks` ([MsgPlugin.h:30-35](plugins/plugins/MsgPlugin.h:30)). A bot bridge
that runs an IPC/socket thread (like `remote-control` does) must keep all bus + command
application on the main/physics thread and hand data across via plain copies.

### Layered headers
- **`MsgPlugin.h`** — generic bus; **no game state.**
- **`VPXPlugin.h`** — VPX events (`OnGameStart/End/PrepareFrame/UpdatePhysics/ActionChanged`)
  + the `VPXPluginAPI` struct = the one place exposing game state + input. [VPXPlugin.h:30-44, 242-285](plugins/plugins/VPXPlugin.h:30).
- **`ControllerPlugin.h`** — generic pinball-controller collaboration (binary inputs, devices
  i.e. lamps/solenoids, segment + DMD displays, audio) via service discovery. Useful only
  when a controller (PinMAME/B2S) provides a source.
- **`ScriptablePlugin.h`** — lets plugins *contribute/override* scriptable COM objects; not a
  telemetry channel and does not expose the engine's native parts.

### What the sanctioned API exposes (`VPXPluginAPI`, [VPXPlugin.h:242-285](plugins/plugins/VPXPlugin.h:242))
Filled at [VPXPluginAPIImpl.cpp:687-706](src/core/VPXPluginAPIImpl.cpp:687). Complete surface:
`GetVpxInfo`, `GetTableInfo` (path + width/height only), `PushNotification`/`UpdateNotification`,
`DisableStaticPrerendering`, `Get/SetActiveViewSetup`, **`SetActionState` / `SetNudgeState` /
`SetPlungerState`** (input), **`GetGameTime`**, and texture helpers. **No ball/part/geometry getter.**

### Build model & "can a plugin reach `Player`/`PinTable`?" → **No.**
- Plugins build as `add_library(<X> MODULE …)` (desktop shared) or `STATIC` (iOS/Android),
  selected by `BUILD_SHARED`/`BUILD_STATIC` — [CMakeLists_plugin_HelloWorld.txt:12,90](make/CMakeLists_plugin_HelloWorld.txt:12).
- **Include dirs are only** `third-party/include`, `plugins`, `plugins/<name>` — never `src/`
  — and no engine library is linked. So even the static variant has no declaration of
  `Player`/`Ball`/`PinTable`/`g_pplayer`. A repo-wide check finds **zero** plugin sources
  including `src/` headers or referencing `g_pplayer`/`m_vball`.
- Dynamic discovery: `Player` ctor calls `MsgPluginManager::ScanPluginFolder` on the Plugins
  app folder ([player.cpp:146](src/core/player.cpp:146)); each plugin is loaded if its
  `Plugin.<id>.Enable` setting is true (**default true**) — [player.cpp:150-165](src/core/player.cpp:150).
  Loading happens **per table launch**, bracketed by `OnGameStart`/`OnGameEnd`.

**Conclusion:** the right model is **message-API-only**, structured like
[remote-control](plugins/remote-control/RemoteControl.cpp). Direct/static linkage to engine
objects is *not* how plugins are built and would not be portable.

---

## 3. Q2 — Live ball state (where it lives, and reachability)

**Engine data model (ground truth):**
- Global live game object: `extern class Player *g_pplayer;`
  ([extern.h:9](src/core/extern.h:9), defined [extern.cpp:7](src/core/extern.cpp:7)).
- **All active balls:** `vector<Ball *> Player::m_vball;` ([player.h:142](src/core/player.h:142)).
  Multiball-safe — balls are `push_back`/removed on create/destroy
  ([player.cpp:1260](src/core/player.cpp:1260), [player.cpp:1275](src/core/player.cpp:1275));
  no per-player cap; the physics loop iterates the whole vector each step.
- **⚠️ Do NOT use `m_pactiveball`** ([player.h:86](src/core/player.h:86)) / VBS `ActiveBall`:
  that is the *one* ball currently colliding and is reassigned mid-loop — wrong for multiball.
  **Iterate `m_vball`.**
- **Per-ball state** lives in `Ball::m_hitBall` ([ball.h:225](src/parts/ball.h:225)) →
  `HitBall::m_d` (a `BallS`, [hitball.h:25-33](src/physics/hitball.h:25)):

| Quantity | Member from `Ball*` | Type | Source |
|---|---|---|---|
| Position | `m_hitBall.m_d.m_pos` | `Vertex3Ds` (VPU) | [hitball.h:27](src/physics/hitball.h:27) |
| Linear velocity | `m_hitBall.m_d.m_vel` | `Vertex3Ds` (VPU/VPT) | [hitball.h:28](src/physics/hitball.h:28) |
| Radius | `m_hitBall.m_d.m_radius` | `float` (VPU; default 25) | [hitball.h:29](src/physics/hitball.h:29) |
| Mass | `m_hitBall.m_d.m_mass` | `float` | [hitball.h:30](src/physics/hitball.h:30) |
| **Angular momentum** (not velocity) | `m_hitBall.m_angularmomentum` | `Vertex3Ds` | [hitball.h:79](src/physics/hitball.h:79) |
| Orientation | `m_hitBall.m_orientation` | `Matrix3` | [hitball.h:81](src/physics/hitball.h:81) |
| Stable id | `m_id` (`const unsigned int`) | unique per game | [ball.h:235](src/parts/ball.h:235) |

- **Angular velocity is derived**, not stored: `ω = m_angularmomentum / Inertia()`, where
  `Inertia() = (2/5)·r²·m` ([hitball.h:59](src/physics/hitball.h:59)); this is exactly what
  the script getter does ([ball.cpp:687-691](src/parts/ball.cpp:687)).
- Convenience accessors (engine-internal): `Ball::GetPosition/GetVelocity/GetRadius`
  ([ball.h:218-220](src/parts/ball.h:218)).
- `m_pos`/`m_vel` are updated every physics step (velocities then displacements in
  `PhysicsSimulateCycle`), so reading right after a step yields current state for every ball.
  A ball locked in a kicker (`m_lockedInKicker`) is simply frozen, not missing.

**Reachability:** none of this is reachable from a pure SDK plugin (see §1/§2). `GetGameTime`
is the only piece a plugin gets directly. See §11 for how to bridge the gap.

---

## 4. Q3 — Other telemetry (flippers, plunger, lamps, switch/event stream, score)

All of the following data exists and is cheap to read **inside the engine**, but is **not on
the plugin bus** (same isolation as §3). Exact members for an engine-side reader:

- **Flippers** — live kinematics on the mover `Flipper::m_phitflipper->m_flipperMover`
  (`FlipperMoverObject`, [hitflipper.h:13-77](src/physics/hitflipper.h:13)):
  - current angle `m_angleCur` (rad) — [hitflipper.h:53](src/physics/hitflipper.h:53); script
    `get_CurrentAngle` returns `RADTOANG(m_angleCur)` ([flipper.cpp:1049](src/parts/flipper.cpp:1049)).
  - angular velocity `m_angleSpeed` (rad/step) — [hitflipper.h:52](src/physics/hitflipper.h:52)
    (**no script getter**).
  - energized/solenoid `m_solState` — [hitflipper.h:68](src/physics/hitflipper.h:68)
    (**no script getter**).
  - **End-of-stroke** is not a flag; derive it by comparing `m_angleCur` to `m_angleEnd`/`m_angleStart`
    (the EOS logic compares angle to the stop, [hitflipper.cpp:319-340](src/physics/hitflipper.cpp:319)).
- **Plunger** — `Plunger::m_phitplunger->m_plungerMover` (`PlungerMoverObject`): rod position
  `m_pos`, speed `m_speed` ([hitplunger.h:44-49](src/physics/hitplunger.h:44)). Script
  `Plunger::Position` returns a normalized `0..25` ([plunger.cpp:969-975](src/parts/plunger.cpp:969)).
- **Lamps / lights** — `Light` ([light.h:70](src/parts/light.h:70)). Tri-state off/on/blinking
  enum is `LightStateOff=0/On=1/Blinking=2` ([vpinball.idl:39-41](src/core/vpinball.idl:39)).
  Live members: `m_inPlayState` ([light.h:164](src/parts/light.h:164)) and live faded intensity
  `m_currentIntensity` ([light.h:165](src/parts/light.h:165), exposed via `GetInPlayIntensity`).
  "Blinking" is a *mode* + a pattern string `m_d.m_rgblinkpattern` walked over time via
  `m_iblinkframe` ([light.cpp:314](src/parts/light.cpp:314)) — effective on/off =
  `pattern[m_iblinkframe] == '1'`.
- **Switch / event / hit stream** — engine parts implement `IFireEvents` and the player fires
  events into the **table's VBScript** (`FireDispID`, e.g. [player.cpp:725-735](src/core/player.cpp:725)).
  **There is no core, table-agnostic "element hit / switch changed" broadcast on the plugin
  bus.** The only physics-side broadcast is the bare `OnUpdatePhysics` tick (no payload). The
  `ControllerPlugin.h` input/device stream (`GetInputs`/`GetDevices`,
  [ControllerPlugin.h:97-150](plugins/plugins/ControllerPlugin.h:97)) carries **PinMAME's
  emulated ROM switches/lamps/solenoids**, only when such a controller is loaded — not VPX's
  physical playfield switches. (Note: the `CTLPI_INPUT_GET_SRC_MSG` *input* source appears
  unimplemented by any in-tree provider; only `CTLPI_DEVICE_GET_SRC_MSG` is wired end-to-end,
  consumed by b2s/dof.)
- **Score & ball-in-play** — **no engine-level representation.** ROM tables keep these inside
  the PinMAME machine, surfaced only as DMD/segment frames + lamp/switch arrays (decoding a
  numeric score = ROM/hardware-specific OCR). Original tables keep them as arbitrarily-named
  VBScript variables. **Per the brief, this must be an optional, clearly-marked, non-uniform
  sensor** (or a per-table config the harness supplies) — never guaranteed ground truth.

---

## 5. Q4 — Static geometry

**Not available via the sanctioned API** (confirmed — the brief's suspicion was correct). A
pure plugin cannot walk the table object model; it has no path to `PinTable`. The only
geometry datum exposed is the play-area rectangle via `GetTableInfo`
(`tableWidth=m_right`, `tableHeight=m_bottom`, in VPU).

**Engine-side model (for a reader that can see `g_pplayer`):**
- All parts: `PinTable::m_vedit` (`vector<IEditable*>`, [pintable.h:500](src/parts/pintable.h:500)),
  read-only via `GetParts()` ([pintable.h:483](src/parts/pintable.h:483)); lookup by name via
  `GetElementByName` ([pintable.h:441](src/parts/pintable.h:441)). **Note `PinTable` lives in
  `src/parts/pintable.h`, not `src/core/`.** Reach it via `g_pplayer->m_ptable`
  ([player.h:75](src/core/player.h:75)).
- Type discriminator: `IEditable::GetItemType()` → `ItemTypeEnum`
  ([iselect.h:13-42](src/core/iselect.h:13)); `static_cast` to the concrete part then read `m_d`.
- **Flipper pivot + length:** pivot `m_d.m_Center` (`Vertex2D`, [flipper.h:26](src/parts/flipper.h:26));
  nominal length `m_d.m_FlipperRadiusMax` (= script `.Length`, [flipper.cpp:981-990](src/parts/flipper.cpp:981));
  effective length `m_FlipperRadius` (difficulty-interpolated at init,
  [flipper.cpp:248-254](src/parts/flipper.cpp:248)); cap radii `m_BaseRadius`/`m_EndRadius`;
  angles `m_StartAngle`/`m_EndAngle` (deg). Tip ≈ `center + (sin a, −cos a)·(m_FlipperRadius + m_EndRadius)`
  ([flipper.cpp:386-390](src/parts/flipper.cpp:386)).
- **Ramp entrance/exit:** ramps are dragpoint splines; call `Ramp::GetCentralCurve(vv)`
  ([ramp.h:156](src/parts/ramp.h:156)) → endpoints `vv.front()`/`vv.back()` are the two ramp
  ends (XY, VPU); end heights `m_d.m_heightbottom`/`m_heighttop` ([ramp.h:19-20](src/parts/ramp.h:19)).
  There is **no explicit "entrance" flag** — which end is the entrance is table-author-dependent
  (heuristic needed, e.g. lowest-Z end or proximity to flippers).
- **Kicker** (drains/saucers): center `m_d.m_vCenter` + `m_radius` ([kicker.h:19-20](src/parts/kicker.h:19)).
  **Drains are kickers or triggers — there is no dedicated "drain" class.**
- **Surface/wall** (closed dragpoint polygon + `m_heightbottom/top`), **gate**
  (`m_vCenter`+`m_length`+`m_rotation`), **spinner** (same), **trigger**
  (`m_vCenter`+`m_radius`/shape), **hit/drop target** (`m_vPosition`+`m_vSize`+`m_targetType`) —
  members cited in [surface.h:26](src/parts/surface.h:26), [gate.h:16](src/parts/gate.h:16),
  [spinner.h:18](src/parts/spinner.h:18), [trigger.h:19](src/parts/trigger.h:19),
  [hittarget.h:29](src/parts/hittarget.h:29).
- **Read once at `OnGameStart`** — geometry is authored data, constant during play.
- (Collidable `Primitive` toys also carry mesh/transform geometry; not enumerated here.)

---

## 6. Q5 — Actuation (the command-IN linchpin) ✅

**Use `VPXPluginAPI::SetActionState(VPXAction actionId, int isPressed)`**
([VPXPlugin.h:258](plugins/plugins/VPXPlugin.h:258)). This is sanctioned, plugin-reachable,
table-agnostic, portable, and requires **no synthetic OS keystrokes**.

How it works (verified end-to-end, [VPXPluginAPIImpl.cpp:108-122](src/core/VPXPluginAPIImpl.cpp:108)):
`SetActionState` → looks up the action in `m_actionMap` (built at `OnGameStart`,
[VPXPluginAPIImpl.cpp:529-554](src/core/VPXPluginAPIImpl.cpp:529)) → `InputAction::SetDirectState`
([InputAction.cpp:182](src/input/InputAction.cpp:182)) → on a press↔release transition fires the
**same script `DISPID_GameEvents_KeyDown/KeyUp`** a physical key fires, with keycode
`0x10000 | actionId` ([InputManager.cpp:684-700](src/input/InputManager.cpp:684)). The table's
own `_KeyDown`/`_KeyUp` handler then drives its flipper (`RotateToEnd`/`RotateToStart`,
[flipper.cpp:513-535](src/parts/flipper.cpp:513)). Because injected and physical input are
OR-merged via "direct state slots", **press and release are distinct** (`isPressed != 0`) →
**cradling works**, and it is **table-agnostic** (you actuate the *action*; each table maps it
to its own objects, [ScriptGlobalTable.cpp:159-205](src/core/ScriptGlobalTable.cpp:159)).

`VPXAction` enum ([VPXPlugin.h:207-234](plugins/plugins/VPXPlugin.h:207)) includes
`LeftFlipper`, `RightFlipper`, `StagedLeft/RightFlipper`, `Left/Right MagnaSave`, `LaunchBall`,
`LeftNudge`, `CenterNudge`, `RightNudge`, `Tilt`, `StartGame`, …

**Confirmed working from a plugin:** flip L/R (+staged), `LaunchBall` (digital plunger:
press = pull, release = fire), digital `Left/Center/RightNudge` (→ real `PhysicsEngine::Nudge`
force), `Tilt`.

**⚠️ Confirmed NON-functional today (verified first-hand at
[InputManager.cpp:1115-1128](src/input/InputManager.cpp:1115)):**
- **Analog plunger** position/speed via `SetPlungerState` → `InputManager::SetPlungerPos/Speed`
  are empty `// FIXME` stubs.
- **Analog nudge** vector via `SetNudgeState` → `InputManager::SetNudge` is an empty `// FIXME`
  stub.
- So precise plunger pull-strength and precise (x,y) nudge acceleration require implementing
  those engine stubs (an engine change). Milestone 1/2 command surface is otherwise complete
  with digital actions.

**Threading:** `SetActionState` touches `g_pplayer->m_pininput` directly and is not documented
thread-safe — **call it on the physics/main thread** (e.g. inside an `OnUpdatePhysics`
subscriber), not from the IPC thread.

**Rejected alternative:** direct engine calls (`Flipper::RotateToEnd`, `PhysicsEngine::Nudge`)
are real but are engine internals not in any plugin header, and are per-object (not
table-agnostic) — unreachable from a loadable plugin.

`OnActionChanged` ([VPXPlugin.h:40](plugins/plugins/VPXPlugin.h:40)) is for **observing/
suppressing** actions (a subscriber may clear `isPressed`), not for originating them — useful
for reading/overriding human input, not as the bot's command channel.

---

## 7. Q6 — Timing

- **Physics rate: 1000 Hz / 1 ms per step.** `PHYSICS_STEPTIME = 1000` µs
  ([physconst.h:8](src/physics/physconst.h:8)). (The `DEFAULT_STEPTIME 10000` constant is the
  legacy "VP Time" unit, mislabeled "1000Hz"; the true step is `PHYSICS_STEPTIME`.)
- **Per-tick hook:** subscribe to **`VPXPI_EVT_ON_UPDATE_PHYSICS`** ("OnUpdatePhysics",
  [VPXPlugin.h:39](plugins/plugins/VPXPlugin.h:39)), broadcast from
  [PhysicsEngine.cpp:591](src/physics/PhysicsEngine.cpp:591). The `remote-control` plugin
  subscribes to exactly this ([RemoteControl.cpp:442](plugins/remote-control/RemoteControl.cpp:442)).
- **Important caveat:** the broadcast sits **outside** the per-step `while` loop, guarded by
  one `if` — it fires **once per `UpdatePhysics()` call, not once per 1 ms step**. If a call
  catches up N steps, you get **one** callback for the batch. Payload is `nullptr`.
- **Achievable cadence:** in the modern **BGFX / multithreaded loop** (the standalone/Linux/Mac
  path), `UpdateGameLogic()`→`UpdatePhysics(usec())` is polled in a tight spin
  ([player.cpp:1831,1887](src/core/player.cpp:1831)), so `OnUpdatePhysics` fires at roughly the
  1 ms-boundary rate — **approaching ~1000 Hz, best-effort**. In the legacy
  `GPUQueueStuffingGameLoop` it fires only ~3× per rendered frame (~tens–hundreds Hz).
- **Why physics, not frame:** the render-frame event `OnPrepareFrame`
  ([player.cpp:2102](src/core/player.cpp:2102)) fires at display refresh (tens–low-hundreds Hz)
  — one to two orders of magnitude slower; it would alias fast ball motion.
- **Execution context:** the callback runs **synchronously, inline, on the physics/logic
  thread** ([MsgPluginManager.cpp:188](plugins/plugins/MsgPluginManager.cpp:188)). **Any
  blocking work here stalls physics.** Pattern: snapshot state + apply pending commands only;
  do all socket I/O on a separate thread (exactly what `remote-control` does).

---

## 8. Q7 — Coordinate frame & units ✅ (fully confirmed)

```
VPX PLAYFIELD COORDINATE FRAME (table/physics space; table-agnostic)
  Units : VPU ("VP length unit"). 50 VPU = 1.0625 in = 26.9875 mm.
          1 VPU = 0.53975 mm = 5.3975e-4 m   (1 m ≈ 1852.71 VPU)
          Use SDK macros MMTOVPU/VPUTOMM/INCHESTOVPU/VPUTOINCHES/CMTOVPU/VPUTOCM
          (VPXPlugin.h:165-175 — no platform #ifdefs, fully portable).
          Do NOT hardcode the legacy 0.540425 mm/VPU (0.125% off).
  Origin: (0,0) = playfield TOP-LEFT. Play area = [0..m_right] x [0..m_bottom]
          (m_left=m_top=0, "always zero for now"). m_right=width, m_bottom=length.
  +X    : toward m_right (horizontal "right" in the top-down view).
  +Y    : DOWN-table, toward the player / drain.
  +Z    : UP, out of the playfield. Playfield surface plane = z=0.
          Glass at z ≈ 210 VPU (m_glassTop/BottomHeight default 210).
          A resting ball's center z = surface + radius (default radius 25 VPU).
  Ball velocity (m_vel): VPU per VPT, where 1 VPT = 0.01 s  ⇒  ×100 for VPU/s.
                         (NOT per 1 ms physics sub-step.)
```
Evidence: VPU constant [physconst.h:30-46](src/physics/physconst.h:30) + macros
[VPXPlugin.h:165-175](plugins/plugins/VPXPlugin.h:165); origin/extents
[pintable.h:626-629](src/parts/pintable.h:626), playfield plane z=0
[pintable.cpp:3230-3233](src/parts/pintable.cpp:3230); axes proven by the gravity vector
`m_gravity.y=+sin(slope)·s` (down-table = +Y), `m_gravity.z=−cos(slope)·s` (up = +Z) —
[PhysicsEngine.cpp:146-151](src/physics/PhysicsEngine.cpp:146); velocity units
[hitball.cpp:443-489](src/physics/hitball.cpp:443), `PHYS_FACTOR=0.1`
([physconst.h:15](src/physics/physconst.h:15)). The frame is **identical on every table**;
only the extent (`m_right`/`m_bottom`) and glass heights vary.

---

## 9. Task 1 — Transport decision

**Recommendation for Milestone 1: a loopback TCP socket carrying length-framed fixed binary
frames, with a dedicated I/O thread and a triple-buffer / semaphore handoff** so the physics
tick only does a cheap copy. Upgrade path: **shared memory + documented struct (SPSC ring /
seqlock)** if/when the socket latency floor matters.

**Why TCP-binary first:**
- **Lowest effort given prior art.** [`remote-control`](plugins/remote-control/RemoteControl.cpp)
  already ships a portable, vendored socket layer (`#ifdef _WIN32` Winsock / `#else` BSD,
  MIT-licensed, [RemoteControl.cpp:37-60](plugins/remote-control/RemoteControl.cpp:37)), a
  fixed-struct wire protocol, a dedicated I/O `std::thread`, and a `std::binary_semaphore`
  handoff — the physics callback never touches the socket
  ([RemoteControl.cpp:232-243,308-389](plugins/remote-control/RemoteControl.cpp:232)). Lift it.
- **Measurable RTT in M1** — put a `seq`/`timestamp` in the frame and echo it; TCP is reliable
  and ordered. (Use TCP, not UDP, for commands so flips are never dropped/reordered.)
- **Language-neutral** — every language speaks TCP + length-prefixed binary / MessagePack /
  NDJSON. Prefer an explicit little-endian fixed layout (or MessagePack) over
  `remote-control`'s raw `reinterpret_cast` struct, which isn't safe across languages/compilers.
- **Portable** — one cross-platform code path, already proven on Win/Linux/Mac (its
  `plugin.cfg` ships all three).

**Non-blocking design (mandatory, transport-independent):**
1. In the `OnUpdatePhysics` callback (physics thread): snapshot telemetry into a producer-owned
   slot and publish lock-free (triple-buffer / seqlock for "latest wins"; ~20 self-contained
   lines — there is **no** reusable SPSC queue in `src/` to borrow). Apply any pending commands
   via `SetActionState` here too. **No blocking I/O.**
2. A dedicated emitter `std::thread` owns the socket and does all `send`/`recv`.
3. Commands in: the receiver thread writes a lock-free command slot; it is applied on the next
   `OnUpdatePhysics` (or via `RunOnMainThread`) — **never** call the engine input API from the
   I/O thread (bus is not thread-safe).

**Note:** there is **no** shared-memory / named-pipe / websocket prior art anywhere in-tree, so
SHM would be net-new (two OS code paths + your own ring + cleanup) — worth it later, overkill
for M1. The b2s "Server" plugins are **not** network IPC — they read controller device state
in-process over the message bus (a good model for *sourcing* controller telemetry, not for
crossing the process boundary).

---

## 10. Build & load recipe (mirror HelloWorld)

Minimal plugin = **two files + build registration**. Use cfg `id = "BotBridge"` (must be a
valid C identifier — it becomes the `BotBridgePluginLoad`/`Unload` symbol prefix; the folder/
dll may use hyphens).

**Create:**
- `plugins/bot-bridge/botbridge.cpp` — copy [helloworld.cpp](plugins/helloworld/helloworld.cpp),
  rename exports to `BotBridgePluginLoad`/`BotBridgePluginUnload`.
- `plugins/bot-bridge/plugin.cfg` — copy [helloworld plugin.cfg](plugins/helloworld/plugin.cfg),
  set `id="BotBridge"`, libraries `plugin-bot-bridge[64].{dll,so,dylib}`.
- `make/CMakeLists_plugin_BotBridge.txt` — copy
  [CMakeLists_plugin_HelloWorld.txt](make/CMakeLists_plugin_HelloWorld.txt), s/HelloWorld/BotBridge/,
  s/helloworld/bot-bridge/.
- `make/plugin-bot-bridge.vcxproj` (+ `.filters`) — copy helloworld's, repoint source paths,
  **fresh `<ProjectGuid>`**.

**Edit (register):**
- `make/CMakeLists_plugins.txt` — add `include(.../CMakeLists_plugin_BotBridge.txt)`.
- `make/VisualPinball.sln` (+ `.slnx`) — add the project + its 12 config mappings (pattern at
  [VisualPinball.sln:303-314](make/VisualPinball.sln:303)).
- `make/create_vs_solution.bat` — add a copy block (it lists each plugin explicitly).
- **Static/standalone only:** to include it in iOS/Android/`__LIBVPINBALL__`, add a
  `{ "BotBridge", &BotBridgePluginLoad, &BotBridgePluginUnload }` row to `SetupStaticPlugins`
  ([VPinballLib.cpp:261-290](lib/src/VPinballLib.cpp:261)). Not needed for desktop.

**Build just the plugin (Windows dev):** regenerate via `make/create_vs_solution.bat`, then
`msbuild .build/vsproject/plugin-bot-bridge.vcxproj /p:Configuration=Debug_BGFX /p:Platform=x64`
(the PostBuildEvent copies the DLL + cfg into
`.build/bin/vpx/Debug_BGFX-x64/plugins/bot-bridge/`). Launch the existing
`VPinballX_BGFX64.exe`; it auto-discovers and (default) enables the plugin. **No engine rebuild
needed** for a pure plugin.

**Standalone (Linux/Mac):** compiles unchanged (SDK headers only) via the CMake path.

---

## 11. ⚑ The central decision: how to get ball + geometry telemetry OUT

This is the only unresolved design question, and it is the project owner's call because it
trades directly against the brief's "don't patch core" constraint. The data the brief needs
(all-ball pos/vel/spin/radius, geometry) is **not** on the sanctioned plugin bus and is
**unreachable from any pure plugin** (dynamic or static). Three ways forward:

### Option 1 — Additive host-API extension *(recommended)*
Add a small, **read-only, table-agnostic** telemetry surface to the sanctioned API — e.g.
`GetBalls(BallState* out, uint32_t max)` and a geometry-snapshot getter — implemented in
[VPXPluginAPIImpl.cpp](src/core/VPXPluginAPIImpl.cpp) by reading `g_pplayer->m_vball` and
`g_pplayer->m_ptable->GetParts()`, declared in the SDK header
[VPXPlugin.h](plugins/plugins/VPXPlugin.h). The **bot-bridge plugin stays a pure SDK plugin**;
only the host's plugin-API surface grows — exactly how `SetActionState` itself was added.
- ✅ Cleanest, table-agnostic, portable (standalone gets it for free), idiomatic (the SDK
  header explicitly says it is a "work in progress" expected to grow).
- ✅ Additive and localized → low rebase risk against upstream.
- ⚠️ Technically edits `src/core/` (the plugin-API impl + SDK header). It is **not** a physics/
  engine-logic fork, but it *is* a host change — so it bends the literal "don't patch core"
  rule. Requires an engine rebuild.
- ↪ Bonus: this is genuinely upstreamable (could be offered as a VPX PR), which would erase the
  "fork" concern entirely.

### Option 2 — Engine-integrated (statically-linked) plugin
Keep the bridge under `plugins/` but give its target `src/` includes + engine symbols and read
`g_pplayer` directly.
- ✅ Maximal capability, no API design needed.
- ❌ Deviates from the plugin build model; couples tightly to engine internals (fragile when
  upstream refactors `m_vball`/`HitBall`); only works in a custom static build; arguably *more*
  fork-like than Option 1 despite living in `plugins/`. Not recommended.

### Option 3 — Pure SDK plugin, accept the telemetry ceiling
No engine change at all. The bridge emits only what the bus exposes: game time, table
dimensions, `OnActionChanged` (what the human/other plugins do), and — only when a controller
(PinMAME/B2S) is loaded — DMD/segment/lamp/switch **device frames**. **No ball physics, no
geometry.** Commands-in still fully work.
- ✅ Strictly honors "don't patch core"; fully portable.
- ❌ Cannot deliver the ball/table telemetry that is the whole point of the bridge; defers the
  hard problem. The brief calls telemetry-out half the mandate and stresses the bot "reasons
  geometrically", so this guts the deliverable.

**Recommendation: Option 1.** It is the only path that satisfies *table-agnostic ball +
geometry telemetry* + *portability* while keeping the plugin itself clean, and the engine
delta is small, additive, and upstreamable. Milestone 1 can still begin immediately on the
**commands-in + timing** half (fully solved, zero engine change) while the read-only getter is
added in parallel.

---

## 12. Open questions / risks to track

1. **Architecture decision (§11)** — blocks the *content* of Milestone 1/2 telemetry. Needs an
   owner ruling on Option 1 vs 3 (Option 2 not recommended).
2. **Analog plunger/nudge are `// FIXME` no-op stubs** ([InputManager.cpp:1115-1128](src/input/InputManager.cpp:1115)).
   M1/M2 command surface is digital-only unless these are implemented (engine change). Confirm
   whether variable plunger strength / precise nudge vector are required.
3. **`OnUpdatePhysics` is per-`UpdatePhysics()`-call, not per-1ms-tick**, and best-effort
   ~1 kHz only on the BGFX loop. If a guaranteed per-tick callback is needed, that's an
   engine change (move the broadcast inside the step loop). Measure the real cadence in M1.
4. **Score / ball-in-play is non-uniform** — must be optional/marked or harness-supplied
   per-table; never guaranteed.
5. **Ramp "entrance" is ambiguous** (no engine flag) — needs a heuristic if geometry is exposed.
6. **`SetActionState` thread-safety** — apply commands on the physics/main thread, not the IPC
   thread.
7. **Latency number (M1's key output) is not yet measured** — the transport choice in §9 is
   optimized precisely to measure it first.

---

*Schema version note:* the telemetry/command schema (units, coordinate frame, versioned) will
be authored alongside Milestone 1/2 once the §11 decision is made; the coordinate frame and
units in §8 are its normative basis.
