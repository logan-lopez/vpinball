"""The bridge client: the single connect / decode / send spine.

Everything in this project that talks to the VPX bot bridge goes through
`BridgeClient`. There is no other transport code anywhere — the bot template,
the reference bot, and the debug console all import this.

Transport (must match the bridge plugin exactly; see plugins/bot-bridge/README.md):
  * loopback TCP, default 127.0.0.1:13501, TCP_NODELAY
  * NDJSON: one JSON object per '\\n'-terminated line
  * the bridge serves ONE client at a time (run either a bot or the console, not both)
  * on connect (once a game is running) the bridge sends a "static" frame, then
    "state" frames every physics update (~1 kHz best-effort); a fresh "static"
    frame is re-sent when a new game starts

Freshness model (the important part):
  A background reader thread drains the socket continuously and keeps only the
  *latest* state — slow consumers never back up a queue, and stale continuous
  frames are dropped on purpose. Discrete `events`, however, are coalesced: every
  event since your previous read is preserved, so dropping stale frames never
  loses a hit/switch event.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Optional

from . import state as st
from .state import (
    Ball, Flipper, Plunger, Lamp, Nudge, HitEvent, State,
    Geometry, Part, LampDesc,
    FlipperGeom, RampGeom, KickerGeom, TriggerGeom, BumperGeom,
    GateGeom, SpinnerGeom, TargetGeom, SurfaceGeom, PrimitiveGeom,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 13501


# --------------------------------------------------------------------------- #
# Decode: raw JSON -> typed state objects
# --------------------------------------------------------------------------- #
def decode_event(o: dict) -> HitEvent:
    return HitEvent(
        t=o.get("t", 0.0), type=o.get("type", -1), kind=o.get("kind", -1),
        scalar=o.get("scalar", 0.0), name=o.get("name", ""),
    )


def decode_state(o: dict, events: Optional[list[HitEvent]] = None) -> State:
    """Decode a "state" frame. `events` overrides the frame's own event list with
    the coalesced list (events accumulated across any dropped frames)."""
    balls = [
        Ball(id=b["id"], x=b["x"], y=b["y"], z=b["z"],
             vx=b["vx"], vy=b["vy"], vz=b["vz"],
             avx=b["avx"], avy=b["avy"], avz=b["avz"], radius=b["r"])
        for b in o.get("balls", [])
    ]
    flippers = [
        Flipper(i=f["i"], angle=f["angle"], angle_speed=f["angleSpeed"],
                solenoid=bool(f["solenoid"]), end_of_stroke=bool(f["eos"]))
        for f in o.get("flippers", [])
    ]
    plungers = [
        Plunger(i=p["i"], pos=p["pos"], pos_vpu=p["posVPU"],
                speed=p["speed"], rest=p["rest"])
        for p in o.get("plungers", [])
    ]
    lamps = [
        Lamp(i=l["i"], mode=l["mode"], lit=bool(l["lit"]), intensity=l["in"])
        for l in o.get("lamps", [])
    ]
    n = o.get("nudge", {}) or {}
    nudge = Nudge(
        ax=n.get("ax", 0.0), ay=n.get("ay", 0.0),
        vx=n.get("vx", 0.0), vy=n.get("vy", 0.0),
        dx=n.get("dx", 0.0), dy=n.get("dy", 0.0),
        tilt=bool(n.get("tilt", 0)), slam=bool(n.get("slam", 0)),
        plumb_simulated=bool(n.get("plumbSim", 0)), plumb_count=n.get("plumbCount", 0),
    )
    if events is None:
        events = [decode_event(e) for e in o.get("events", [])]
    return State(
        tick=o.get("tick", 0), time=o.get("time", 0.0),
        balls=balls, flippers=flippers, plungers=plungers, lamps=lamps,
        nudge=nudge, events=events,
    )


def decode_geometry(o: dict) -> Geometry:
    """Decode a "static" frame into raw parts + named typed views + lamp directory."""
    g = Geometry()
    for p in o.get("geometry", []):
        part = Part(
            type=p["type"], name=p.get("name", ""),
            x=p["x"], y=p["y"], z=p["z"],
            a=p["a"], b=p["b"], c=p["c"], d=p["d"],
            ex=p["ex"], ey=p["ey"], ez=p["ez"],
        )
        g.parts.append(part)
        _classify_part(g, part)
    for l in o.get("lamps", []):
        g.lamps.append(LampDesc(index=l["i"], name=l.get("name", ""), mode=l.get("mode", 0)))
    return g


def _classify_part(g: Geometry, p: Part) -> None:
    """Unpack the generic (a,b,c,d,ex,ey,ez) scalars into named, typed views by
    part type. The mapping mirrors the schema table in the bridge README."""
    t = p.type
    if t == 1:  # flipper
        g.flippers.append(FlipperGeom(
            name=p.name, pivot_x=p.x, pivot_y=p.y, height=p.z,
            length_max=p.a, base_radius=p.b, end_radius=p.c,
            start_angle=p.d, end_angle=p.ex))
    elif t == 12:  # ramp
        g.ramps.append(RampGeom(
            name=p.name, entrance_x=p.x, entrance_y=p.y, entrance_z=p.z,
            exit_x=p.ex, exit_y=p.ey, exit_z=p.ez,
            width_bottom=p.a, width_top=p.b, ramp_type=p.c))
    elif t == 8:  # kicker (drains/saucers/scoops)
        g.kickers.append(KickerGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            radius=p.a, kicker_type=p.b, orientation=p.c))
    elif t == 6:  # trigger
        g.triggers.append(TriggerGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            radius=p.a, shape=p.b, rotation=p.c))
    elif t == 5:  # bumper
        g.bumpers.append(BumperGeom(name=p.name, x=p.x, y=p.y, z=p.z, radius=p.a))
    elif t == 10:  # gate
        g.gates.append(GateGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            length=p.a, rotation=p.b, angle_min=p.c, angle_max=p.d))
    elif t == 11:  # spinner
        g.spinners.append(SpinnerGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            length=p.a, rotation=p.b, angle_min=p.c, angle_max=p.d))
    elif t == 22:  # hittarget
        g.targets.append(TargetGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            rot_z=p.a, target_type=p.b, dropped=bool(p.c),
            size_x=p.ex, size_y=p.ey, size_z=p.ez))
    elif t == 0:  # surface (wall) -> bbox
        g.surfaces.append(SurfaceGeom(
            name=p.name, min_x=p.x, min_y=p.y, max_x=p.ex, max_y=p.ey,
            height_bottom=p.a, height_top=p.b))
    elif t == 19:  # primitive
        g.primitives.append(PrimitiveGeom(
            name=p.name, x=p.x, y=p.y, z=p.z,
            size_x=p.ex, size_y=p.ey, size_z=p.ez))


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class BridgeClient:
    """Connect to the bridge, expose the freshest decoded state, and send commands.

    Typical use:
        with BridgeClient() as bridge:
            bridge.wait_until_ready()
            state = bridge.latest_state()
            bridge.flip_left(True)

    All command methods are safe to call from your policy thread. `latest_state()`
    and `wait_for_state()` give you the freshest snapshot with coalesced events.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 auto_reconnect: bool = True, reconnect_delay: float = 1.0):
        self.host = host
        self.port = port
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay

        self._lock = threading.Lock()
        self._new_frame = threading.Condition(self._lock)
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._running = False

        # Freshest state, stored as the raw parsed dict (decoded lazily on read so
        # the 1 kHz reader path stays cheap). Events are decoded eagerly + coalesced.
        self._latest_raw: Optional[dict] = None
        self._unread = False          # the latest frame has not been read yet
        self._pending_events: list[HitEvent] = []
        self._geometry: Optional[Geometry] = None
        self._seq = 0                 # bumps on every state frame
        self._connected = False
        self._frames_received = 0     # lifetime count (for diagnostics)
        self._frames_dropped = 0      # state frames overwritten before a read
        self._bytes_received = 0      # lifetime socket bytes (for load reporting)

    # -- lifecycle ---------------------------------------------------------- #
    def connect(self) -> "BridgeClient":
        if self._running:
            return self
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, name="vpxbot-reader", daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        self._running = False
        with self._lock:
            self._new_frame.notify_all()
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)

    def __enter__(self) -> "BridgeClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def geometry(self) -> Optional[Geometry]:
        """The static geometry + lamp directory for the current game, or None if
        not received yet (no game running). Replaced when a new game starts."""
        with self._lock:
            return self._geometry

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "frames_received": self._frames_received,
                "frames_dropped": self._frames_dropped,
                "bytes_received": self._bytes_received,
                "seq": self._seq,
            }

    # -- reading state ------------------------------------------------------ #
    def latest_state(self) -> Optional[State]:
        """The freshest state, with all events since the previous call attached.
        Returns None if no state frame has arrived yet. Non-blocking."""
        with self._lock:
            if self._latest_raw is None:
                return None
            raw = self._latest_raw
            events = self._pending_events
            self._pending_events = []
            self._unread = False
        return decode_state(raw, events=events)

    def wait_for_state(self, timeout: Optional[float] = None) -> Optional[State]:
        """Block until a new state frame arrives (or `timeout` s elapses), then
        return the freshest state with coalesced events. Returns None on timeout
        with no frame, or if the client is closing."""
        with self._lock:
            start_seq = self._seq
            if not self._new_frame.wait_for(
                    lambda: self._seq != start_seq or not self._running, timeout):
                return None
            if self._latest_raw is None:
                return None
            raw = self._latest_raw
            events = self._pending_events
            self._pending_events = []
            self._unread = False
        return decode_state(raw, events=events)

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """Block until the first state frame arrives (i.e. a game is running and
        the plugin is streaming). Returns True if ready, False on timeout."""
        return self.wait_for_state(timeout) is not None

    # -- sending commands --------------------------------------------------- #
    # These map 1:1 onto the bridge's command vocabulary. Digital actions take a
    # `press` flag — press and release are distinct, so holding (cradling) is just
    # "keep pressing".
    def send_raw(self, obj: dict) -> bool:
        """Send one command object as an NDJSON line. Returns False if not
        currently connected. Thread-safe."""
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._lock:
            sock = self._sock
            if sock is None or not self._connected:
                return False
        try:
            sock.sendall(data)
            return True
        except OSError:
            self._connected = False
            return False

    def _digital(self, cmd: str, press: bool) -> bool:
        return self.send_raw({"cmd": cmd, "press": 1 if press else 0})

    def flip_left(self, press: bool) -> bool:
        return self._digital("flip_left", press)

    def flip_right(self, press: bool) -> bool:
        return self._digital("flip_right", press)

    def launch(self, press: bool) -> bool:
        """Digital plunger / launch action. press = pull, release = fire
        (table-defined). Same path as a real Launch keypress."""
        return self._digital("launch", press)

    def nudge_left(self, press: bool) -> bool:
        return self._digital("nudge_left", press)

    def nudge_center(self, press: bool) -> bool:
        """Forward (up-table) nudge."""
        return self._digital("nudge_center", press)

    def nudge_right(self, press: bool) -> bool:
        return self._digital("nudge_right", press)

    def tilt(self, press: bool) -> bool:
        return self._digital("tilt", press)

    def nudge_accel(self, x: float, y: float) -> bool:
        """Analog nudge acceleration override (table frame, VPU/VP-tick^2).
        NOTE: once engaged this is a sticky one-way latch in the engine — keyboard/
        hardware nudge is suppressed until table reload. Drive it continuously and
        send (0, 0) for 'no nudge'."""
        return self.send_raw({"cmd": "nudge_accel", "x": x, "y": y})

    def plunger_pos(self, value: float) -> bool:
        """Analog plunger position override, 0..1 (0 forward, 1 fully pulled).
        Only moves the rod on tables with the Mechanical Plunger option enabled;
        also a sticky latch (see nudge_accel)."""
        return self.send_raw({"cmd": "plunger_pos", "value": value})

    # -- background reader -------------------------------------------------- #
    def _reader_loop(self) -> None:
        while self._running:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                if sock is not None:
                    sock.close()
                if not self.auto_reconnect or not self._running:
                    break
                self._sleep(self.reconnect_delay)
                continue

            with self._lock:
                self._sock = sock
                self._connected = True
            self._consume(sock)

            with self._lock:
                self._connected = False
                if self._sock is sock:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            if not self.auto_reconnect or not self._running:
                break
            self._sleep(self.reconnect_delay)

    def _consume(self, sock: socket.socket) -> None:
        """Read NDJSON lines until disconnect, updating latest state + events."""
        buf = b""
        while self._running:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break  # peer closed
            with self._lock:
                self._bytes_received += len(chunk)
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1:]
                if line.strip():
                    self._ingest(line)

    def _ingest(self, line: bytes) -> None:
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        kind = o.get("type")
        if kind == "state":
            evs = [decode_event(e) for e in o.get("events", [])]
            with self._lock:
                if self._unread:
                    self._frames_dropped += 1  # previous frame was overwritten unread
                self._latest_raw = o
                self._unread = True
                if evs:
                    self._pending_events.extend(evs)
                self._seq += 1
                self._frames_received += 1
                self._new_frame.notify_all()
        elif kind == "static":
            geo = decode_geometry(o)
            with self._lock:
                self._geometry = geo

    def _sleep(self, seconds: float) -> None:
        # Interruptible sleep: wake immediately on close().
        with self._lock:
            self._new_frame.wait_for(lambda: not self._running, timeout=seconds)
