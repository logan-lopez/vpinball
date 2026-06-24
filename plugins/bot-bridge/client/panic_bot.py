#!/usr/bin/env python3
"""Reference panic-flip bot — the league's floor.

This is the simplest viable policy, on purpose: if a ball is near a flipper, raise
that flipper. No aiming, no shot selection, no modes, no cleverness. Its two jobs:

  1. Prove that a *program* (not your hands) can close the decide -> action loop on
     a live table.
  2. Be the permanent control every AI-written bot must beat. If a bot can't keep a
     ball alive better than panic-flipping, it isn't trying.

It is built on exactly the same scaffold as bot_template.py and the shared vpxbot
library — it is what the template looks like once decide() is filled in with the
dumbest thing that works.

The only non-flip mechanic is a crude auto-launch (pulse the plunger if nothing
has moved for a while) so the bot is autonomous; that is plumbing to get a ball
into play, not strategy.
"""

from __future__ import annotations

import argparse

import vpxbot
from vpxbot import State, DesiredActions, Geometry


# --- tunables (deliberately blunt) ----------------------------------------- #
REACT_ZONE_VPU = 400.0     # start flipping when a ball is within this far ABOVE the flipper line
MOTION_EPS_VPT = 0.5       # planar speed (VPU/VP-tick) considered "the ball is moving"
IDLE_LAUNCH_SECS = 2.0     # if nothing moves this long, try to launch
LAUNCH_HOLD_SECS = 0.5     # how long to hold the plunger before releasing (fire)


class PanicBot:
    def __init__(self, geometry: Geometry | None):
        self.split_x: float | None = None      # x dividing "left side" from "right side"
        self.flipper_line: float | None = None  # y of the main flippers
        if geometry and len(geometry.flippers) >= 2:
            # The two flippers closest to the player (largest y) are the main pair.
            main = sorted(geometry.flippers, key=lambda f: f.pivot_y, reverse=True)[:2]
            self.split_x = sum(f.pivot_x for f in main) / 2.0
            self.flipper_line = min(f.pivot_y for f in main)
        self._last_motion_time = 0.0
        self._launch_until = 0.0

    def decide(self, state: State) -> DesiredActions:
        left = right = False

        # Track motion so we know when the table has gone quiet (ball stuck/waiting).
        if any(b.speed_vpt > MOTION_EPS_VPT for b in state.balls):
            self._last_motion_time = state.time

        # Panic flip: if a ball is low (near the flipper line), raise the flipper on
        # the side the ball is on. With known geometry we split by the midpoint
        # between the two main flippers; without it we just flip when a ball descends.
        if self.flipper_line is not None and self.split_x is not None:
            for b in state.balls:
                if b.y > self.flipper_line - REACT_ZONE_VPU:
                    if b.x < self.split_x:
                        left = True
                    else:
                        right = True
        else:
            for b in state.balls:
                if b.vy > MOTION_EPS_VPT:   # descending toward the drain
                    left = right = True

        # Crude auto-launch so the bot is self-starting.
        plunger = False
        if state.time < self._launch_until:
            plunger = True                                   # holding the plunger pulled
        elif state.time - self._last_motion_time > IDLE_LAUNCH_SECS:
            self._launch_until = state.time + LAUNCH_HOLD_SECS
            plunger = True

        return DesiredActions(left=left, right=right, plunger=plunger)


def main():
    ap = argparse.ArgumentParser(description="VPX reference panic-flip bot")
    ap.add_argument("--host", default=vpxbot.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=vpxbot.DEFAULT_PORT)
    ap.add_argument("--hz", type=float, default=250.0)
    args = ap.parse_args()
    vpxbot.run(
        bot_factory=lambda geometry: PanicBot(geometry).decide,
        host=args.host, port=args.port, hz=args.hz,
    )


if __name__ == "__main__":
    main()
