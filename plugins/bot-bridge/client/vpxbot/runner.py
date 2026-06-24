"""The declarative action model and the bot run loop.

A bot's decide() does **not** send commands. It returns a `DesiredActions` that
describes what it wants the actuators to *be* this instant:

    left flipper up or down, right flipper up or down, plunger pulled or released,
    and an optional momentary nudge impulse.

`Actuators.apply()` diffs that against what the actuators are currently doing and
sends only the press/release that actually changed. Two consequences fall out for
free, with no helper doing it *for* the bot:

  * Cradling / holding / timed releases: keep returning `left=True` and the
    flipper stays up; return `left=False` to drop it. The bot expresses timing by
    *when* it changes its mind, not by managing press/release edges.

  * Momentary nudge: digital nudge is edge-triggered in the engine (one impulse
    per press). So a nudge fires once on the rising edge (none -> a direction).
    To nudge again, return `nudge=None` for a tick, then the direction again.

This module is mechanism, not strategy. It contains no aiming, no ball
prediction, no "trap the ball" — only the plumbing every bot shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .client import BridgeClient
from .state import State, Geometry

# Nudge directions for DesiredActions.nudge.
NUDGE_LEFT = "left"
NUDGE_RIGHT = "right"
NUDGE_FORWARD = "forward"   # up-table; maps to the engine's center nudge
_NUDGE_DIRS = {NUDGE_LEFT, NUDGE_RIGHT, NUDGE_FORWARD}


@dataclass
class DesiredActions:
    """What a bot wants the actuators to be doing *right now*. Declarative: just
    set the booleans; the runner figures out the press/release edges."""

    left: bool = False           # left flipper: True = up (energized), False = down
    right: bool = False          # right flipper: True = up, False = down
    plunger: bool = False        # True = pull/hold the plunger, False = release/fire
    nudge: Optional[str] = None  # one-shot impulse this tick: NUDGE_LEFT/RIGHT/FORWARD or None


# A decide function: given the freshest State, return what to do. Returning None
# is allowed and means "do nothing" (idle: flippers down, plunger released).
DecideFn = Callable[[State], Optional[DesiredActions]]


class Actuators:
    """Tracks current actuator state and sends only the deltas. One per bridge."""

    def __init__(self, bridge: BridgeClient):
        self.bridge = bridge
        self._left = False
        self._right = False
        self._plunger = False
        # Nudge edge machine: `_nudge_armed` is True when a fresh impulse is allowed.
        # It disarms after firing and re-arms when the bot asks for no nudge.
        self._nudge_armed = True
        self._nudge_held: Optional[str] = None  # direction currently pressed, if any

    def apply(self, desired: Optional[DesiredActions]) -> None:
        d = desired or DesiredActions()

        if d.left != self._left:
            self.bridge.flip_left(d.left)
            self._left = d.left
        if d.right != self._right:
            self.bridge.flip_right(d.right)
            self._right = d.right
        if d.plunger != self._plunger:
            self.bridge.launch(d.plunger)
            self._plunger = d.plunger

        self._apply_nudge(d.nudge)

    def _apply_nudge(self, direction: Optional[str]) -> None:
        # Release any nudge key pressed on the previous apply() (one-tick pulse).
        if self._nudge_held is not None:
            self._press_nudge(self._nudge_held, False)
            self._nudge_held = None

        if direction is None:
            self._nudge_armed = True       # re-arm for the next impulse
            return
        if direction not in _NUDGE_DIRS:
            return
        if self._nudge_armed:
            self._press_nudge(direction, True)
            self._nudge_held = direction   # released on the next apply()
            self._nudge_armed = False      # one impulse per rising edge

    def _press_nudge(self, direction: str, press: bool) -> None:
        if direction == NUDGE_LEFT:
            self.bridge.nudge_left(press)
        elif direction == NUDGE_RIGHT:
            self.bridge.nudge_right(press)
        elif direction == NUDGE_FORWARD:
            self.bridge.nudge_center(press)

    def release_all(self) -> None:
        """Drop everything to a safe idle state (used on shutdown)."""
        self.apply(DesiredActions())
        if self._nudge_held is not None:
            self._press_nudge(self._nudge_held, False)
            self._nudge_held = None


# A factory that, given this game's static geometry (or None if not yet
# available), returns the decide function to run. Use this when your bot wants to
# precompute from geometry at startup.
BotFactory = Callable[[Optional[Geometry]], DecideFn]


def run(decide: Optional[DecideFn] = None, *,
        bot_factory: Optional[BotFactory] = None,
        host: str = "127.0.0.1", port: int = 13501,
        hz: float = 250.0,
        on_state: Optional[Callable[[State], None]] = None) -> None:
    """Connect to the bridge and run the policy loop until interrupted.

    The loop is intentionally tiny and is the whole "harness" a bot needs:

        latest freshest state  ->  decide(state)  ->  apply desired actions  ->  repeat

    It always acts on the *freshest* state (stale frames are dropped by the
    client), and paces itself to roughly `hz` so decide() is cheap to write and
    doesn't busy-spin. `hz` only bounds how often you re-decide; you never miss
    the latest state because the client always hands you the newest snapshot.

    Pass either `decide` (a plain function) or `bot_factory` (called once after
    connecting, with the static geometry, returning a decide function — use this
    if your bot precomputes from geometry). `on_state` is an optional observer
    (e.g. for logging) called every iteration.
    """
    if (decide is None) == (bot_factory is None):
        raise ValueError("pass exactly one of `decide` or `bot_factory`")

    import time
    period = 1.0 / hz if hz > 0 else 0.0
    with BridgeClient(host=host, port=port) as bridge:
        print(f"[vpxbot] connecting to {host}:{port} - waiting for a running table...")
        if not bridge.wait_until_ready(timeout=60.0):
            print("[vpxbot] no telemetry: is a table running with the BotBridge plugin enabled?")
            return
        if bot_factory is not None:
            decide = bot_factory(bridge.geometry)
        print("[vpxbot] connected. running. Ctrl-C to stop.")
        actuators = Actuators(bridge)
        try:
            next_t = time.perf_counter()
            while True:
                state = bridge.latest_state()
                if state is not None:
                    if on_state is not None:
                        on_state(state)
                    actuators.apply(decide(state))
                if period > 0.0:
                    next_t += period
                    sleep_for = next_t - time.perf_counter()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    else:
                        next_t = time.perf_counter()  # fell behind; resync
        except KeyboardInterrupt:
            print("\n[vpxbot] stopping, releasing actuators.")
        finally:
            actuators.release_all()
