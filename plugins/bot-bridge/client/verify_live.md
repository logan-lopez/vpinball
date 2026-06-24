# Live verification runbook

Everything in `client/` is verified offline against a mock bridge. These three
checks need a **real running table** because they test the bridge + engine, not
just the Python client: the coordinate frame, that a program keeps a ball alive,
and round-trip latency under load.

Allow ~15 minutes. No code changes required.

---

## 0. One-time setup

### a. Enable the plugin (required — it is NOT on by default)

Your `[Plugin.BotBridge]` section is currently missing. Add it to:

```
C:\Users\logan\AppData\Roaming\VPinballX\VPinballX.ini
```

```ini
[Plugin.BotBridge]
Enable = 1
```

### b. Python

Standard-library Python 3.9+. No `pip install` needed. All client commands below
run from:

```
C:\Users\logan\Development\vpinball\plugins\bot-bridge\client
```

### c. Things to know before you start

- **One client at a time.** The bridge serves a single TCP connection. Run the
  console **or** a bot **or** the latency tool — never two at once. "no telemetry"
  usually means something else is already connected.
- **The bot drives the table even when the VPX window is not focused.** Commands
  go through the plugin API (`SetActionState`), not OS keystrokes — so you can keep
  the terminal focused and watch the table react. Your physical keyboard still
  works too (engine OR-merges plugin + keyboard input).
- **A "light show" does not increase telemetry size** — every lamp is sent every
  frame regardless of whether it's lit. The load that can hurt latency is
  *engine CPU*: many balls colliding + heavy rendering/light updates competing with
  the physics thread. So "under load" means "make the engine work hard", and the
  real test is **rest vs. busy**, not the raw lamp count.
- **Multiball depends on the table.** The bundled `exampleTable.vpx` may be
  single-ball; you create load by playing actively / nudging, not necessarily by
  literal multiball. Use any table that has a real multiball if you have one.

---

## 1. Launch a table in play mode

From the deploy directory (so the table's `assets/` resolve):

```powershell
cd "C:\Users\logan\Development\vpinball\.build\bin\vpx\Debug_BGFX-x64"
.\VPinballX_BGFX64.exe -Play "assets\exampleTable.vpx"
```

Other bundled tables: `assets\blankTable.vpx`, `assets\lightSeqTable.vpx`
(animated lights), `assets\strippedTable.vpx`.

**Confirm the bridge loaded:** on game start you should see an on-screen
notification: **"Bot Bridge v2: tcp 127.0.0.1:13501"**. If it doesn't appear, the
plugin isn't enabled — recheck step 0a.

Leave the table running for all checks below.

---

## 2. Console sanity check — coordinate frame, units, ids, actuation

In a terminal at `client\`:

```powershell
python console.py
```

You'll see a live dashboard (refreshes ~10 Hz). Verify each of these by eye:

| Check | What to do | Expected |
|---|---|---|
| **Ball position is real** | Find the ball on the table | The `ball #… pos=(x,y,z)` matches where the ball physically is |
| **+X is right** | Nudge right: press **`c`** | Ball `x` **increases** |
| **+Y is toward the drain** | Let the ball roll down | Ball `y` **increases** as it falls toward the flippers; `vy > 0` while falling |
| **Units / Z** | Ball at rest | `z ≈ 25` (radius); speeds are small numbers (VPU/tick, ×100 = VPU/s) |
| **Left flipper** | Press **`a`** (toggle up), again to drop | That flipper's `angle` swings and `solenoid=1` while up |
| **Right flipper** | Press **`l`** | Same, on the right |
| **Plunger** | Press **`p`** (pull), again (release/fire) | `plunger pos` rises toward 1.0 then fires; ball launches |
| **Stable ids (multiball)** | If the table can produce >1 ball, get two going | Each `ball #id` keeps the **same id**; ids don't renumber as balls move |
| **Events** | Hit a bumper / wall | An `events this read` line shows `bumper/hit`, `surface/hit`, etc. with the element name |

Console keys: `a`/`l` flippers, `p` plunger, `z`/`x`/`c` nudge left/forward/right,
`t` tilt, `g` dump geometry, `space` release all, `h` help, `q` quit.

> If the dashboard shows garbled box characters, use Windows Terminal (it supports
> the ANSI redraw); the legacy console host may not.

**Pass criteria:** positions track reality, the axes are as in the table above,
flippers/plunger respond, and ball ids are stable. Quit with `q`.

---

## 3. Panic bot keeps a ball alive

Close the console first (one client at a time). Then:

```powershell
python panic_bot.py
```

It connects, waits for telemetry, and prints `running`. It will auto-launch a ball
(it pulses the plunger if nothing's moving) and then panic-flip: whenever a ball
gets near a flipper, it raises that flipper.

**Don't touch the keyboard flippers** during this — you want to see the *program*
keep the ball alive, not your hands.

**Pass criteria:** the bot visibly flips at the ball and keeps it in play
**meaningfully longer than if you did nothing** (let a ball drain untouched a few
times to feel the baseline). It won't aim or play well — that's the point; it's
the floor every smart bot must beat. Stop with **Ctrl-C** (it releases the
flippers on exit).

---

## 4. Round-trip latency under load (the headline number)

Close the panic bot first. Then:

```powershell
python latency_load_test.py
```

It measures the send-flip → motion-in-telemetry round-trip and prints, once per
second, the **observed load** alongside the latency:

```
load:   980 fps  3 ball(s)  64 lamps   612 KiB/s  dropped=  12345   |   RTT median= 4.71ms p95= 5.20ms  (n=37)
```

**Create the load yourself while it runs** (single-client means the bot can't also
be connected, so *you* are the load generator):

1. With the latency tool connected, **play the table with your keyboard** — keep a
   ball (or several, if the table supports multiball) bouncing through bumpers,
   ramps, and any light-show mode. Nudge, trigger modes — make the engine work.
2. Watch the per-second line. Compare **rest** (quiet single ball) to **busy**
   (multiball / heavy animation).

At the end it prints a summary and a verdict:

```
  -> sub-5ms holds: YES
```

**Pass criteria:** the median round-trip stays **under 5 ms** when busy, not just
at rest. If the median climbs well above 5 ms only under load, that's the bridge/
engine buckling (the thing to catch before it shows up as "bots got worse in
multiball"). Stop with Ctrl-C; it prints the final stats.

> Offline reference (already captured, `--mock`): the *client* parse path sustains
> ~14,000 frames/s with ~1-frame freshness lag, so anything above ~5 ms here is the
> bridge/engine side, not Python.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `no telemetry: is a table running…` | Table not in **-Play** mode, plugin not enabled (step 0a), or another client is already connected (close it). |
| No "Bot Bridge v2" notification on launch | Plugin disabled or DLL not deployed. Check the ini section; the DLL should be at `…\Debug_BGFX-x64\plugins\bot-bridge\plugin-bot-bridge64.dll`. |
| Connection refused / hangs at "waiting" | The table window isn't actually running a game yet (still in menu), or port 13501 is taken. |
| Console shows boxes/garbage | Use Windows Terminal for the ANSI dashboard. |
| Flippers don't move from the bot but do from the keyboard | The table maps flippers via script `KeyDown`; very unusual not to work, but confirm with the console (`a`/`l`) first to isolate plugin vs. table. |
| Latency fine at rest, spikes under load | That's the finding — note the busy median/p95; it indicates engine-side contention, the exact thing this step exists to surface. |

## Quick command reference

```powershell
# launch table (deploy dir)
cd "C:\Users\logan\Development\vpinball\.build\bin\vpx\Debug_BGFX-x64"
.\VPinballX_BGFX64.exe -Play "assets\exampleTable.vpx"

# client tools (client dir) — ONE at a time
cd "C:\Users\logan\Development\vpinball\plugins\bot-bridge\client"
python console.py            # step 2: sanity-check telemetry + actuation
python panic_bot.py          # step 3: bot keeps a ball alive
python latency_load_test.py  # step 4: RTT under load (play to create load)
```
