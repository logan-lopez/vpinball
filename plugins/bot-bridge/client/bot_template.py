#!/usr/bin/env python3
"""VPX bot template — the scaffold you fill in.

Copy this file, rename it, and write your logic inside decide(). Everything else
(connecting, decoding telemetry, diffing your desired actions into press/release
edges, the run loop) is handled by the shared `vpxbot` library — you do not
touch it.

--------------------------------------------------------------------------------
THE ONE HOOK
--------------------------------------------------------------------------------
    decide(state) -> DesiredActions

You get the freshest snapshot of the table and return what you want the actuators
to *be* right now. You do not send commands and you do not manage press/release —
just describe the desired state and the runner sends only what changed.

    DesiredActions(
        left    = bool,          # left flipper up (True) or down (False)
        right   = bool,          # right flipper up or down
        plunger = bool,          # pull/hold the plunger (True) or release/fire (False)
        nudge   = None | "left" | "right" | "forward",   # a one-shot nudge impulse
    )

Because actions are declarative, useful behaviours fall out for free:
  * Cradle / hold a flipper: keep returning left=True. It stays up until you stop.
  * Timed release: return left=True for a while, then left=False — the timing is
    just *when you change your mind*.
  * Nudge: return nudge="left" to fire one impulse. To nudge again, return None
    for a tick, then "left" again (nudge is edge-triggered, like a real button).

--------------------------------------------------------------------------------
THE STATE SURFACE  (what you can observe)
--------------------------------------------------------------------------------
Read vpxbot/state.py for the authoritative, fully-documented definitions and the
coordinate frame. In short, every call to decide() gives you a `state` with:

  state.tick                     frame counter; state.time = game time (seconds)

  state.balls   -> list of Ball  EVERY live ball (multiball-safe). Each has:
                                   .id  stable per-ball id (not a list index)
                                   .x .y .z         position, VPU
                                   .vx .vy .vz      velocity, VPU/VP-tick (x100 -> VPU/s)
                                   .avx .avy .avz   spin, rad/VP-tick
                                   .radius          VPU
                 COORDINATE FRAME: origin = playfield TOP-LEFT, +X right,
                 +Y DOWN-table toward the drain, +Z up (surface z=0). So a ball
                 falling toward the flippers has vy > 0 and a large y.

  state.flippers -> list of Flipper   .i .angle(deg) .angle_speed(deg/VP-tick)
                                       .solenoid(energized) .end_of_stroke
  state.plungers -> list of Plunger   .pos(0..1) .pos_vpu .speed .rest
  state.lamps    -> list of Lamp      .i .mode .lit .intensity
                    (lamp i's name is in the static geometry lamp directory)
  state.nudge    -> Nudge             applied nudge accel, table velocity, tilt flags
  state.events   -> list of HitEvent  discrete physical hits since your last call:
                    .t .type .kind .scalar .name  (e.g. a bumper hit, a spinner spin,
                    a trigger entered). Coalesced, so you never miss one.

Static geometry (loaded once, constant during a game) is on the bridge object,
passed to your bot as `geometry` below. It includes (all VPU, table frame):
    geometry.flippers   pivot (x,y), length, base/end radius, start/end angle
    geometry.ramps      entrance (x,y,z) and exit (x,y,z), widths
    geometry.kickers    saucers/scoops AND drains (center, radius)
    geometry.bumpers / .triggers / .gates / .spinners / .targets / .surfaces
    geometry.lamp_names index -> lamp name
(See vpxbot/state.py for every field.)

--------------------------------------------------------------------------------
WHAT THIS TEMPLATE DOES NOT GIVE YOU
--------------------------------------------------------------------------------
There are no strategy helpers — no aim_at(), no trap_ball(), no trajectory
predictor. That is deliberate: the template hands you access and documentation,
not tactics. Build timing, prediction, cradling, shot selection yourself from the
state above. Your bot may keep its own state across decide() calls (that is what
the class below is for) — use it for memory, prediction, or timing if you work
out how.
"""

from __future__ import annotations

import argparse

import vpxbot
from vpxbot import State, DesiredActions, Geometry


class Bot:
    """Your bot. The instance persists for the whole game, so use `self` to keep
    any memory you want across ticks (previous ball position, a timer, a mode)."""

    def __init__(self, geometry: Geometry | None):
        # The static geometry for this table (may be None very briefly at start).
        # Stash whatever you want to precompute from it here.
        self.geometry = geometry
        # --- your persistent state goes here ---

    def decide(self, state: State) -> DesiredActions:
        # ====================================================================
        # YOUR LOGIC GOES HERE.
        #
        # Inspect `state` (and self.geometry / your own memory), then return a
        # DesiredActions describing what the actuators should be right now.
        #
        # The default below does nothing: flippers down, plunger released, no
        # nudge. Replace it.
        # ====================================================================
        return DesiredActions(left=False, right=False, plunger=False, nudge=None)


def main():
    ap = argparse.ArgumentParser(description="VPX bot (template)")
    ap.add_argument("--host", default=vpxbot.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=vpxbot.DEFAULT_PORT)
    ap.add_argument("--hz", type=float, default=250.0,
                    help="policy rate; you always act on the freshest state regardless")
    args = ap.parse_args()

    # The run loop (in vpxbot/runner.py) does, every iteration:
    #     freshest state -> decide -> apply only the changed press/release -> repeat
    # It is intentionally NOT duplicated here. `bot_factory` is called once with
    # this game's static geometry so the Bot can precompute from it.
    vpxbot.run(
        bot_factory=lambda geometry: Bot(geometry).decide,
        host=args.host, port=args.port, hz=args.hz,
    )


if __name__ == "__main__":
    main()
