"""Decoded telemetry surface for the VPX bot bridge.

This module is the *data dictionary* for a bot. Every field a bot can observe
lives here as a typed, documented attribute. If you are writing a bot, read this
file top to bottom: it is the complete sensor surface and nothing is hidden.

It contains **no strategy and no tactics** — only the meaning, units, and
coordinate frame of each value the bridge reports.

--------------------------------------------------------------------------------
COORDINATE FRAME & UNITS  (identical on every table)
--------------------------------------------------------------------------------
Lengths are in **VPU** ("VP length units"):
    50 VPU = 1.0625 in = 26.9875 mm   ->   1 VPU = 0.53975 mm
Use the converters at the bottom of this file (vpu_to_mm, mm_to_vpu, ...).

The playfield is a top-down plane:
    origin (0, 0) = playfield TOP-LEFT corner
    +X  = toward the right of the playfield
    +Y  = DOWN-table, toward the player / the drain
    +Z  = UP, out of the playfield surface (surface plane is z = 0)
The play area spans [0 .. table_width] x [0 .. table_height] VPU. A resting
ball's center sits at z = radius (default radius 25 VPU). The glass is ~210 VPU up.

**Linear velocity** is in **VPU per VP-tick** (1 VP-tick = 0.01 s).
    multiply by VPT_PER_SEC (=100) to get VPU/s.
**Angular velocity** (ball spin) is in **radians per VP-tick**.
**Angles** are in **degrees**. **Angle speeds** are in **degrees per VP-tick**.

A note on intuition for +Y: because +Y points down-table toward the drain, a
ball that is "falling toward the flippers" has vy > 0, and a larger y means
closer to the bottom of the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Unit constants & converters (lengths in VPU, time in VP-ticks of 0.01 s)
# --------------------------------------------------------------------------- #
MM_PER_VPU = 0.53975          # 1 VPU in millimetres
VPU_PER_MM = 1.0 / MM_PER_VPU
INCH_PER_VPU = MM_PER_VPU / 25.4
VPU_PER_INCH = 1.0 / INCH_PER_VPU
VPT_PER_SEC = 100.0           # VP-ticks per second (1 VP-tick = 0.01 s)


def vpu_to_mm(vpu: float) -> float:
    return vpu * MM_PER_VPU


def mm_to_vpu(mm: float) -> float:
    return mm * VPU_PER_MM


def vpu_to_inch(vpu: float) -> float:
    return vpu * INCH_PER_VPU


def vel_to_vpu_per_sec(vel_vpt: float) -> float:
    """Convert a linear-velocity component (VPU/VP-tick) to VPU/second."""
    return vel_vpt * VPT_PER_SEC


# --------------------------------------------------------------------------- #
# Enumerations carried on the wire (kept human-readable here)
# --------------------------------------------------------------------------- #
# VPX ItemTypeEnum (src/core/iselect.h) — the "type" field on geometry parts and
# the "type" of a hit event. Only the collidable/relevant subset is named.
ITEM_TYPE = {
    0: "surface",      # wall (closed dragpoint polygon)
    1: "flipper",
    2: "timer",
    3: "plunger",
    4: "textbox",
    5: "bumper",
    6: "trigger",      # rollover / region sensor
    7: "light",        # a lamp; lamp telemetry comes via lamps[], not geometry
    8: "kicker",       # saucers, scoops, and (usually) drains
    9: "decal",
    10: "gate",
    11: "spinner",
    12: "ramp",
    13: "table",
    19: "primitive",   # arbitrary 3D mesh (may be collidable)
    20: "flasher",
    21: "rubber",
    22: "hittarget",   # stand-up / drop target
    23: "ball",
}

# VPXHitEvent.eventKind — what kind of discrete physical event fired.
EVENT_KIND = {
    0: "hit",            # ball contacted the part
    1: "unhit",          # ball left a region part (trigger/kicker exit)
    2: "slingshot",      # a slingshot fired
    3: "spin",           # a spinner completed a rotation (scalar = spin speed)
    4: "eos",            # end-of-stroke limit (reserved)
    5: "bos",            # begin-of-stroke limit (reserved)
    6: "flipperCollide", # ball collided with a flipper (reserved)
}

# Lamp configured mode (VPXLampState.mode / VPXLampDesc.mode).
LAMP_MODE = {0: "off", 1: "on", 2: "blinking"}


def item_type_name(type_id: int) -> str:
    return ITEM_TYPE.get(type_id, f"type{type_id}")


def event_kind_name(kind_id: int) -> str:
    return EVENT_KIND.get(kind_id, f"kind{kind_id}")


# --------------------------------------------------------------------------- #
# Per-tick dynamic state  (the "state" frame)
# --------------------------------------------------------------------------- #
@dataclass
class Ball:
    """One live ball. Every ball on the table appears here (multiball-safe)."""

    id: int            # stable unique id for this ball's lifetime (NOT a list index)
    x: float           # position X, VPU (+X right)
    y: float           # position Y, VPU (+Y down-table toward the drain)
    z: float           # position Z, VPU (+Z up; surface = 0, center rests at radius)
    vx: float          # linear velocity X, VPU/VP-tick (x100 -> VPU/s)
    vy: float          # linear velocity Y, VPU/VP-tick (>0 = moving toward drain)
    vz: float          # linear velocity Z, VPU/VP-tick
    avx: float         # angular velocity (spin) X, rad/VP-tick
    avy: float         # angular velocity (spin) Y, rad/VP-tick
    avz: float         # angular velocity (spin) Z, rad/VP-tick
    radius: float      # ball radius, VPU (default 25)

    @property
    def speed_vpt(self) -> float:
        """Planar (X/Y) speed magnitude, VPU/VP-tick. Derived, not from the wire."""
        return (self.vx * self.vx + self.vy * self.vy) ** 0.5


@dataclass
class Flipper:
    """One flipper's live kinematics, indexed by `i` (matches the geometry order)."""

    i: int             # flipper index; correlate with Geometry.flippers[i]
    angle: float       # current flipper angle, degrees
    angle_speed: float # angular velocity, degrees/VP-tick (x100 -> deg/s)
    solenoid: bool     # coil energized == the flip button is held
    end_of_stroke: bool  # resting against the end stop (fully up or fully down)


@dataclass
class Plunger:
    """One plunger's live state, indexed by `i`."""

    i: int
    pos: float         # normalized position 0..1 (0 = forward/released, 1 = fully pulled)
    pos_vpu: float     # raw rod-tip position, VPU
    speed: float       # rod speed, VPU/VP-tick (>0 retracting, <0 firing forward)
    rest: float        # park fraction 0..1 (the "at rest" value is `rest`, NOT 0)


@dataclass
class Lamp:
    """One lamp's live state, by index. Correlate the index with the lamp
    directory in the static frame (Geometry.lamp_names[i]) to learn its name."""

    i: int
    mode: int          # configured tri-state: 0 off, 1 on, 2 blinking (see LAMP_MODE)
    lit: bool          # instantaneous on/off, blink phase + dimming already resolved
    intensity: float   # live faded emission, relative (can exceed 1.0)

    @property
    def mode_name(self) -> str:
        return LAMP_MODE.get(self.mode, f"mode{self.mode}")


@dataclass
class Nudge:
    """Global table dynamics: applied nudge force + tilt status."""

    ax: float          # applied nudge acceleration X, VPU/VP-tick^2 (table frame)
    ay: float          # applied nudge acceleration Y, VPU/VP-tick^2
    vx: float          # spring-model table velocity X, VPU/VP-tick
    vy: float          # spring-model table velocity Y, VPU/VP-tick
    dx: float          # visual table-shake displacement X, VPU (screen frame, y flipped)
    dy: float          # visual table-shake displacement Y, VPU
    tilt: bool         # a tilt input is currently active
    slam: bool         # a slam-tilt input is currently active
    plumb_simulated: bool  # the mechanical plumb-bob tilt is being simulated
    plumb_count: int   # monotonic count of plumb-tilt events since game start


@dataclass
class HitEvent:
    """One discrete *physical* hit/switch event. These are emitted as deltas
    each physics tick; the client coalesces them across dropped frames so a slow
    bot still sees every event since its last read.

    These are physical contacts (ball hit this wall/target/bumper, this spinner
    spun, this trigger was entered/left), NOT ROM logical switch numbers — mapping
    a physical hit to a game rule is table-specific and out of the bridge's scope.
    """

    t: float           # engine game time of the event, seconds
    type: int          # ItemTypeEnum of the part hit (see ITEM_TYPE)
    kind: int          # what happened (see EVENT_KIND)
    scalar: float      # event payload, e.g. spinner spin speed (deg/s); 0 if none
    name: str          # element name of the part (correlate with the geometry)

    @property
    def type_name(self) -> str:
        return item_type_name(self.type)

    @property
    def kind_name(self) -> str:
        return event_kind_name(self.kind)


@dataclass
class State:
    """A complete freshest-state snapshot handed to a bot's decide().

    `balls`/`flippers`/`plungers`/`lamps`/`nudge` are the *latest* values (stale
    intermediate frames are dropped on purpose). `events` is the coalesced list
    of every discrete event since the previous time the bot read state, so the
    discrete event stream is never lost even when continuous frames are dropped.
    """

    tick: int                      # bridge frame counter (monotonic per game)
    time: float                    # engine game time, seconds
    balls: list[Ball]
    flippers: list[Flipper]
    plungers: list[Plunger]
    lamps: list[Lamp]
    nudge: Nudge
    events: list[HitEvent] = field(default_factory=list)

    def lamp(self, index: int) -> Optional[Lamp]:
        """Lamp by its directory index, or None if out of range."""
        return self.lamps[index] if 0 <= index < len(self.lamps) else None


# --------------------------------------------------------------------------- #
# Static geometry  (the "static" frame) — sent once per game, constant in play
# --------------------------------------------------------------------------- #
# The bridge packs every collidable part into a generic record (type, name, and
# the scalars x,y,z,a,b,c,d,ex,ey,ez). The meaning of a,b,c,d,ex,ey,ez depends on
# the part type. Rather than make a bot memorize that table, Geometry below
# unpacks the common part types into named, documented views.
@dataclass
class Part:
    """A raw geometry record exactly as it arrives on the wire. Use the typed
    views on `Geometry` (flippers, ramps, kickers, ...) for named fields; this
    raw form is here for part types without a dedicated view."""

    type: int
    name: str
    x: float
    y: float
    z: float
    a: float
    b: float
    c: float
    d: float
    ex: float
    ey: float
    ez: float

    @property
    def type_name(self) -> str:
        return item_type_name(self.type)


@dataclass
class FlipperGeom:
    name: str
    pivot_x: float     # pivot point X, VPU
    pivot_y: float     # pivot point Y, VPU
    height: float      # mount height Z, VPU
    length_max: float  # nominal flipper length (pivot->tip), VPU
    base_radius: float # radius of the base (pivot) cap, VPU
    end_radius: float  # radius of the tip cap, VPU
    start_angle: float # resting (down) angle, degrees
    end_angle: float   # fully-flipped (up) angle, degrees


@dataclass
class RampGeom:
    name: str
    entrance_x: float  # one end of the ramp centre-line, VPU
    entrance_y: float
    entrance_z: float  # height at that end, VPU
    exit_x: float      # the other end, VPU
    exit_y: float
    exit_z: float      # height at that end, VPU
    width_bottom: float
    width_top: float
    ramp_type: float   # VPX ramp-type code
    # NOTE: which end is the true "entrance" is table-author-dependent; the bridge
    # reports the two centre-line endpoints. Treat entrance/exit as "the two ends".


@dataclass
class KickerGeom:
    """Kickers cover saucers, scoops, and (most commonly) the drain holes.
    VPX has no dedicated 'drain' part: a drain is just a kicker at the bottom."""

    name: str
    x: float           # centre X, VPU
    y: float           # centre Y, VPU
    z: float           # hit height Z, VPU
    radius: float      # VPU
    kicker_type: float # VPX kicker-type code
    orientation: float # degrees


@dataclass
class TriggerGeom:
    name: str
    x: float
    y: float
    z: float
    radius: float
    shape: float       # VPX trigger-shape code
    rotation: float    # degrees


@dataclass
class BumperGeom:
    name: str
    x: float
    y: float
    z: float
    radius: float


@dataclass
class GateGeom:
    name: str
    x: float
    y: float
    z: float
    length: float
    rotation: float    # degrees
    angle_min: float   # degrees
    angle_max: float   # degrees


@dataclass
class SpinnerGeom:
    name: str
    x: float
    y: float
    z: float
    length: float
    rotation: float    # degrees
    angle_min: float   # degrees
    angle_max: float   # degrees


@dataclass
class TargetGeom:
    name: str
    x: float
    y: float
    z: float
    rot_z: float       # degrees
    target_type: float
    dropped: bool      # drop targets: currently dropped (down)
    size_x: float
    size_y: float
    size_z: float


@dataclass
class SurfaceGeom:
    """A wall, reduced to its axis-aligned bounding box + heights."""

    name: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    height_bottom: float
    height_top: float


@dataclass
class PrimitiveGeom:
    name: str
    x: float
    y: float
    z: float
    size_x: float
    size_y: float
    size_z: float


@dataclass
class LampDesc:
    """Lamp directory entry: maps a per-tick lamp index to a name."""

    index: int
    name: str
    mode: int          # configured mode at load (see LAMP_MODE)


@dataclass
class Geometry:
    """The table's static geometry + lamp directory, decoded once per game.

    `parts` is every raw record. The typed lists below are the convenient,
    named views of the common part types. `lamps` is the index->name directory
    that the per-tick Lamp list lines up with.
    """

    parts: list[Part] = field(default_factory=list)
    lamps: list[LampDesc] = field(default_factory=list)

    flippers: list[FlipperGeom] = field(default_factory=list)
    ramps: list[RampGeom] = field(default_factory=list)
    kickers: list[KickerGeom] = field(default_factory=list)  # includes drains/saucers
    triggers: list[TriggerGeom] = field(default_factory=list)
    bumpers: list[BumperGeom] = field(default_factory=list)
    gates: list[GateGeom] = field(default_factory=list)
    spinners: list[SpinnerGeom] = field(default_factory=list)
    targets: list[TargetGeom] = field(default_factory=list)
    surfaces: list[SurfaceGeom] = field(default_factory=list)
    primitives: list[PrimitiveGeom] = field(default_factory=list)

    @property
    def lamp_names(self) -> list[str]:
        """Lamp names ordered by directory index (lines up with State.lamps)."""
        out: list[str] = []
        for d in self.lamps:
            if d.index >= len(out):
                out.extend([""] * (d.index - len(out) + 1))
            out[d.index] = d.name
        return out
