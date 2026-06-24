# vpxbot — Python client for the VPX Bot Bridge

This is the Python layer an AI pinball bot is written against. It sits on top of
the **bot-bridge plugin** (`../botbridge.cpp`), which streams telemetry out of a
running Visual Pinball X table and accepts commands in at sub-5ms round-trip. This
layer does **not** contain strategy — it is access + documentation only. What a
bot *does* with the data is up to the bot.

It contains four things, all sharing one transport spine:

| Component | File | What it is |
|---|---|---|
| **Client library** (the spine) | `vpxbot/` | connect / decode / send. The single transport. Everything imports this. |
| **Bot template** | `bot_template.py` | the scaffold you fill in — write `decide()`, nothing else. |
| **Reference panic bot** | `panic_bot.py` | the deliberately-dumb control: if a ball is near a flipper, flip. The league floor. |
| **Debug console** | `console.py` | eyeball telemetry live and fire actuators by hand. |

The authoritative, fully-commented definition of every field is in
[`vpxbot/state.py`](vpxbot/state.py). This README is the human summary.

---

## Quick start

1. Build + enable the plugin and launch a table (see `../README.md`). Confirm
   `[Plugin.BotBridge] Enable = 1` in `VPinballX.ini`, then launch in play mode.
2. Run something:

   ```sh
   python console.py        # interactive: watch telemetry, fire actuators by hand
   python panic_bot.py      # the reference bot plays (badly, on purpose)
   ```

3. Write your own bot: copy `bot_template.py`, fill in `decide()`.

   ```python
   import vpxbot

   def decide(state: vpxbot.State) -> vpxbot.DesiredActions:
       # your logic here
       return vpxbot.DesiredActions(left=True)

   vpxbot.run(decide)
   ```

> **Only one client at a time.** The bridge serves a single connection. Run the
> console *or* a bot, not both at once.

No third-party dependencies — standard-library Python 3.9+.

---

## The action model (declarative)

`decide(state)` returns a `DesiredActions` describing what the actuators should
**be** right now. It never sends commands and never manages press/release — the
runner diffs your desired state against the current actuator state and sends only
what changed.

```python
DesiredActions(
    left    = bool,           # left flipper:  True = up (held), False = down
    right   = bool,           # right flipper: True = up, False = down
    plunger = bool,           # True = pull/hold the plunger, False = release (fire)
    nudge   = None | "left" | "right" | "forward",   # one momentary nudge impulse
)
```

Two behaviours fall out of this for free, with no helper doing it for you:

- **Cradle / hold / timed release.** Keep returning `left=True` and the flipper
  stays up; return `left=False` to drop it. Timing is just *when* you change your
  mind — nothing to manage.
- **Momentary nudge.** Digital nudge is edge-triggered in the engine (one impulse
  per press), so a nudge fires once on the rising edge (`None → a direction`). To
  nudge again, return `None` for a tick, then the direction again.

For anything beyond this (analog nudge vector, analog plunger position, tilt as a
held action, staged flippers) use the lower-level `BridgeClient` methods directly
— see [`vpxbot/client.py`](vpxbot/client.py).

---

## The state surface

All spatial values use the **VPX table frame** (identical on every table):

```
Units : VPU (VP length units).  50 VPU = 1.0625 in = 26.9875 mm  ->  1 VPU = 0.53975 mm
Origin: (0,0) = playfield TOP-LEFT corner.
  +X  = toward the right of the playfield
  +Y  = DOWN-table, toward the player / the drain   (so a ball falling to the
        flippers has vy > 0 and a large y)
  +Z  = UP, out of the playfield (surface plane z = 0; a resting ball's center
        sits at z = radius, default 25)
Linear velocity : VPU per VP-tick (1 VP-tick = 0.01 s).  x100 -> VPU/s.
Angular velocity: radians per VP-tick.   Angles: degrees.   Angle speeds: deg/VP-tick.
```

Converters (`vpxbot.vpu_to_mm`, `mm_to_vpu`, `vpu_to_inch`, `vel_to_vpu_per_sec`)
and the constant `VPT_PER_SEC = 100` are in the package.

### Per-tick state — `state` (one `State` per `decide` call)

| Field | Type | Meaning / units |
|---|---|---|
| `state.tick` | int | bridge frame counter (monotonic per game) |
| `state.time` | float | engine game time, seconds |
| `state.balls` | list[Ball] | **every** live ball (multiball-safe) |
| `state.flippers` | list[Flipper] | by index `i` (lines up with `geometry.flippers`) |
| `state.plungers` | list[Plunger] | by index `i` |
| `state.lamps` | list[Lamp] | by index `i` (name via `geometry.lamp_names[i]`) |
| `state.nudge` | Nudge | global table dynamics + tilt |
| `state.events` | list[HitEvent] | discrete hits **since your last read** (coalesced) |

**Ball** — `id` (stable per-ball, *not* a list index), `x y z` (VPU), `vx vy vz`
(VPU/VP-tick), `avx avy avz` (spin, rad/VP-tick), `radius` (VPU). `.speed_vpt` is
a derived planar-speed convenience.

**Flipper** — `i`, `angle` (deg), `angle_speed` (deg/VP-tick), `solenoid` (coil
energized == button held), `end_of_stroke` (resting against the stop).

**Plunger** — `i`, `pos` (0..1; 0 = forward/released, 1 = fully pulled), `pos_vpu`
(raw rod tip), `speed` (VPU/VP-tick; >0 retracting, <0 firing), `rest` (park
fraction — note "at rest" is `rest`, not 0).

**Lamp** — `i`, `mode` (0 off / 1 on / 2 blinking), `lit` (instantaneous on/off
with blink + dim resolved), `intensity` (live faded emission, relative, may
exceed 1).

**Nudge** — `ax ay` (applied nudge accel, VPU/VP-tick²), `vx vy` (table velocity),
`dx dy` (visual shake, screen frame), `tilt`, `slam`, `plumb_simulated`,
`plumb_count` (monotonic plumb-tilt event count).

**HitEvent** — `t` (game time, s), `type` (ItemTypeEnum; `.type_name`), `kind`
(`.kind_name`: hit / unhit / slingshot / spin / eos / bos / flipperCollide),
`scalar` (payload, e.g. spinner spin speed), `name` (element name). These are
**physical** contacts, not ROM logical switch numbers.

### Static geometry — `bridge.geometry` (loaded once per game)

A `Geometry` object with raw `parts` plus named, typed views (all VPU, table
frame). Passed to your bot's constructor when you use `bot_factory`/the template.

| View | Key fields |
|---|---|
| `geometry.flippers` | `name`, `pivot_x/pivot_y`, `length_max`, `base_radius`, `end_radius`, `start_angle`, `end_angle` |
| `geometry.ramps` | `name`, `entrance_x/y/z`, `exit_x/y/z`, `width_bottom/top`, `ramp_type` |
| `geometry.kickers` | saucers/scoops **and drains**: `name`, `x/y/z`, `radius`, `kicker_type`, `orientation` |
| `geometry.bumpers` | `name`, `x/y/z`, `radius` |
| `geometry.triggers` | `name`, `x/y/z`, `radius`, `shape`, `rotation` |
| `geometry.gates` / `.spinners` | `name`, `x/y/z`, `length`, `rotation`, `angle_min/max` |
| `geometry.targets` | `name`, `x/y/z`, `rot_z`, `target_type`, `dropped`, `size_x/y/z` |
| `geometry.surfaces` | walls reduced to bbox: `min_x/y`, `max_x/y`, `height_bottom/top` |
| `geometry.primitives` | `name`, `x/y/z`, `size_x/y/z` |
| `geometry.lamp_names` | index → lamp name (lines up with `state.lamps`) |

> Ramp "entrance" vs "exit": the bridge reports the two centre-line endpoints;
> which end is the true entrance is table-author-dependent. Treat them as "the two
> ends". (VPX has no dedicated drain part — drains are kickers at the bottom.)

---

## Freshness & the event stream

Telemetry arrives (~1 kHz) far faster than a policy loop runs. The client handles
this for you:

- A background thread drains the socket continuously and keeps only the **latest**
  state. `latest_state()` always returns the freshest snapshot; slow consumers
  never back up a queue, and stale continuous frames are dropped on purpose.
- **One refinement to "drop stale frames":** discrete `events` are per-tick deltas,
  so they would be lost if their frame were dropped. The client therefore
  **coalesces events** — every event since your previous read is preserved and
  attached to the next `latest_state()`. Continuous state is latest-wins; the
  discrete event stream is lossless. (This is the one non-obvious design call in
  the client; it is deliberate.)

Connection: `BridgeClient` auto-reconnects if the engine restarts, and re-reads
the static frame when a new game starts. `wait_until_ready()` blocks until the
first state frame (a game is running).

---

## Commands (low-level, on `BridgeClient`)

These map 1:1 onto the bridge's command vocabulary; the action model above is
built on them. Digital actions take a `press` flag (press/release distinct).

```python
bridge.flip_left(press)      bridge.flip_right(press)
bridge.launch(press)         # digital plunger/launch: press=pull, release=fire
bridge.nudge_left(press)     bridge.nudge_center(press)   bridge.nudge_right(press)
bridge.tilt(press)
bridge.nudge_accel(x, y)     # analog nudge acceleration override (table frame)
bridge.plunger_pos(value)    # analog plunger position 0..1
```

Caveats inherited from the engine/bridge:
- **Analog overrides are a sticky one-way latch.** Once a bot drives `nudge_accel`
  or `plunger_pos`, keyboard/hardware input to that channel is suppressed until the
  table reloads. Drive them continuously (send `0` for "no input").
- `plunger_pos` only moves the rod on tables with the *Mechanical Plunger* option.
- **Score / ball-in-play is not exposed** — there is no uniform engine
  representation (it lives in ROM/PinMAME or table-specific script). If a bot needs
  it, it must come from a per-table source, not the bridge.

---

## Verification

### Offline (no table) — run the test suites
```sh
python tests/test_client.py   # decode, multiball, freshness/drop, event coalescing, commands, action diffing
python tests/test_bots.py     # panic bot closes decide->action end-to-end (subprocess vs mock bridge)
```
All checks pass against a mock bridge that speaks the exact v2 wire format.

### Offline — client load capacity
```sh
python latency_load_test.py --mock --rate 1000 --balls 6 --lamps 200
```
Measured on this machine (6 balls, 200 lamps, ~10 KB/frame):

- **Parse ceiling ≈ 14,000 frames/s** — the reader's `json.loads` hot path, ~14×
  headroom over the bridge's 1 kHz. The client is not the bottleneck.
- Over a flooded loopback socket: **100% ingest completeness**, **~1 frame**
  freshness lag, `latest_state()` ≈ 70 µs. Latest-wins dropping behaves as designed.

(The Python mock producer tops out ~3,400 fps for 10 KB frames; the real C++
bridge serializes far faster. The parse-ceiling number above is the meaningful one
for "can the client keep up with a 1 kHz stream" — yes.)

### Live (on a real table) — the checklist the brief calls for
Step-by-step instructions with the exact commands and pass criteria are in
[`verify_live.md`](verify_live.md). In short, use `console.py` and confirm by eye:
- [ ] **Coordinate frame:** nudge the ball — does `x` move with +X right, `y` with
      +Y down toward the drain? Is the active ball's position where the ball is?
- [ ] **Units sane:** ball at rest sits at `z ≈ radius`; speeds look like VPU/tick.
- [ ] **Multiball:** each ball keeps a **stable `id`** through a multiball; ball
      count matches.
- [ ] **Actuation:** press a flipper — angle + `solenoid` change in telemetry.

Then run the **panic bot** live and confirm it keeps a ball alive meaningfully
better than chance — it is the league's permanent floor.

### Live — latency under load (the headline number)
```sh
python latency_load_test.py            # measure RTT while reporting observed load
```
It reports round-trip latency (send flip → motion seen in telemetry) once per
second alongside the live load (fps, balls, lamps, dropped, KiB/s). **Induce the
load** — start multiball + a light show, or let `panic_bot.py` keep balls going —
and watch whether the median holds **under 5 ms**. (This is the check that catches
a bridge that's clean at rest but buckles in multiball.)

> The offline numbers above are captured and reproducible. The live RTT-under-load
> number must be taken on a running table and is left for that environment; the
> tool prints a `sub-5ms holds: YES/NO` verdict.

---

## Layout

```
client/
  vpxbot/
    __init__.py        public API surface
    state.py           the data dictionary — every field, units, coordinate frame
    client.py          BridgeClient: connect / decode / send + freshness + reconnect
    runner.py          DesiredActions, Actuators (diffing), run() loop
  bot_template.py      Component 2 — fill in decide()
  panic_bot.py         Component 3 — reference dumb bot
  console.py           Component 4 — interactive debug console
  latency_load_test.py latency (live) + client load capacity (mock)
  tests/
    mock_bridge.py     a test double that speaks the exact v2 wire format
    test_client.py     spine unit/behaviour checks
    test_bots.py       panic-bot end-to-end check
```
