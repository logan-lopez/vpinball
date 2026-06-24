"""vpxbot — the shared client library for VPX bot bridge bots.

This is the single spine that the bot template, the reference bot, and the debug
console all import. It does three things and nothing else:

  * connect to the bridge          (BridgeClient)
  * decode telemetry               (State and the types in `state`)
  * send commands                  (BridgeClient.flip_left, ...)

plus a small declarative action model + run loop (DesiredActions, run) so a bot
only has to write decide().

Quick start for a bot author:

    import vpxbot

    def decide(state: vpxbot.State) -> vpxbot.DesiredActions:
        # your logic here — read state, return what you want the actuators to be
        return vpxbot.DesiredActions(left=True)

    vpxbot.run(decide)

Read `vpxbot/state.py` for the complete sensor surface (every field, its units,
and the coordinate frame) and `vpxbot/runner.py` for the action model.
"""

from .client import (
    BridgeClient, DEFAULT_HOST, DEFAULT_PORT,
    decode_state, decode_geometry, decode_event,
)
from .runner import (
    DesiredActions, Actuators, run,
    NUDGE_LEFT, NUDGE_RIGHT, NUDGE_FORWARD,
)
from .state import (
    # per-tick dynamic state
    State, Ball, Flipper, Plunger, Lamp, Nudge, HitEvent,
    # static geometry
    Geometry, Part, LampDesc,
    FlipperGeom, RampGeom, KickerGeom, TriggerGeom, BumperGeom,
    GateGeom, SpinnerGeom, TargetGeom, SurfaceGeom, PrimitiveGeom,
    # enums / lookups
    ITEM_TYPE, EVENT_KIND, LAMP_MODE,
    item_type_name, event_kind_name,
    # units / converters
    MM_PER_VPU, VPU_PER_MM, VPT_PER_SEC,
    vpu_to_mm, mm_to_vpu, vpu_to_inch, vel_to_vpu_per_sec,
)

__all__ = [
    "BridgeClient", "DEFAULT_HOST", "DEFAULT_PORT",
    "decode_state", "decode_geometry", "decode_event",
    "DesiredActions", "Actuators", "run",
    "NUDGE_LEFT", "NUDGE_RIGHT", "NUDGE_FORWARD",
    "State", "Ball", "Flipper", "Plunger", "Lamp", "Nudge", "HitEvent",
    "Geometry", "Part", "LampDesc",
    "FlipperGeom", "RampGeom", "KickerGeom", "TriggerGeom", "BumperGeom",
    "GateGeom", "SpinnerGeom", "TargetGeom", "SurfaceGeom", "PrimitiveGeom",
    "ITEM_TYPE", "EVENT_KIND", "LAMP_MODE", "item_type_name", "event_kind_name",
    "MM_PER_VPU", "VPU_PER_MM", "VPT_PER_SEC",
    "vpu_to_mm", "mm_to_vpu", "vpu_to_inch", "vel_to_vpu_per_sec",
]
