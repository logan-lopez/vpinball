"""Offline verification of the vpxbot client library against the mock bridge.

Run:  python tests/test_client.py     (from plugins/bot-bridge/client/)

These prove the spine's behaviour without a running table: schema decode,
multiball tracking, freshness (drop stale state, keep all events), command
encoding, and the declarative action diffing. The real-table checks (coordinate
frame sanity, true engine latency) still belong on a live table via the console.
"""

from __future__ import annotations

import os
import sys
import time

# Make `import vpxbot` work when run directly from the client directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vpxbot                                  # noqa: E402
from vpxbot import DesiredActions, Actuators   # noqa: E402
from mock_bridge import MockBridge             # noqa: E402

_failures = 0


def check(cond: bool, msg: str) -> None:
    global _failures
    status = "PASS" if cond else "FAIL"
    if not cond:
        _failures += 1
    print(f"  [{status}] {msg}")


def _connect(mock: MockBridge) -> vpxbot.BridgeClient:
    c = vpxbot.BridgeClient(host="127.0.0.1", port=mock.port, auto_reconnect=False)
    c.connect()
    assert c.wait_until_ready(timeout=5.0), "client never received a state frame"
    return c


def test_geometry_decode():
    print("test_geometry_decode")
    with MockBridge() as mock:
        c = _connect(mock)
        # geometry may arrive a hair before/after first state; give it a moment.
        for _ in range(50):
            if c.geometry is not None:
                break
            time.sleep(0.02)
        g = c.geometry
        check(g is not None, "static geometry received")
        check(len(g.flippers) == 2, f"two flippers decoded (got {len(g.flippers)})")
        check(g.flippers[0].name == "LeftFlipper" and g.flippers[0].pivot_x == 278.0,
              "flipper named view: pivot unpacked")
        check(len(g.ramps) == 1 and g.ramps[0].entrance_z == 30.0 and g.ramps[0].exit_z == 120.0,
              "ramp entrance/exit heights unpacked")
        check(len(g.kickers) == 1 and g.kickers[0].name == "Drain" and g.kickers[0].radius == 25.0,
              "kicker/drain center+radius unpacked")
        check(g.lamp_names[:2] == ["lamp0", "lamp1"], "lamp directory index->name")
        c.close()


def test_multiball_tracking():
    print("test_multiball_tracking")
    with MockBridge() as mock:
        mock.set_balls([
            MockBridge.make_ball(0, x=100.0, y=200.0, vy=5.0),
            MockBridge.make_ball(7, x=300.0, y=400.0, vy=-2.0),
            MockBridge.make_ball(9, x=500.0, y=600.0),
        ])
        c = _connect(mock)
        time.sleep(0.05)
        s = c.latest_state()
        check(s is not None and len(s.balls) == 3, "three live balls decoded")
        ids = sorted(b.id for b in s.balls)
        check(ids == [0, 7, 9], f"stable ball ids preserved (got {ids})")
        b7 = next(b for b in s.balls if b.id == 7)
        check(b7.x == 300.0 and b7.vy == -2.0, "per-ball position/velocity decoded")
        c.close()


def test_freshness_drop():
    print("test_freshness_drop")
    with MockBridge(rate_hz=2000.0) as mock:
        c = _connect(mock)
        mock.set_flood(True)          # blast frames as fast as possible
        time.sleep(0.3)               # consumer deliberately does not read
        mock.set_flood(False)
        time.sleep(0.02)
        s1 = c.latest_state()
        st = c.stats
        check(st["frames_dropped"] > 10,
              f"stale state frames were dropped, not queued (dropped={st['frames_dropped']})")
        # latest_state must be the newest frame, and reading again (no new frames)
        # returns a still-recent tick — never an old backed-up one.
        time.sleep(0.02)
        s2 = c.latest_state()
        check(s2.tick >= s1.tick, "latest_state always returns the freshest frame")
        c.close()


def test_event_coalescing():
    print("test_event_coalescing")
    with MockBridge(rate_hz=500.0) as mock:
        c = _connect(mock)
        c.latest_state()              # drain any startup events
        # Emit several events spread across many frames, without reading.
        for i in range(5):
            mock.push_event(type=8, kind=0, name=f"hit{i}", scalar=float(i))
            time.sleep(0.02)
        s = c.latest_state()          # single read after many frames elapsed
        names = sorted(e.name for e in s.events)
        check(names == ["hit0", "hit1", "hit2", "hit3", "hit4"],
              f"all events across dropped frames coalesced (got {names})")
        check(s.events[0].type_name == "kicker" and s.events[0].kind_name == "hit",
              "event enums resolved to names")
        # A second read with no new events returns an empty event list.
        s2 = c.latest_state()
        check(s2 is not None and len(s2.events) == 0, "events drained once, not repeated")
        c.close()


def test_command_encoding():
    print("test_command_encoding")
    with MockBridge() as mock:
        c = _connect(mock)
        c.flip_left(True)
        c.flip_right(False)
        c.launch(True)
        c.nudge_left(True)
        c.nudge_accel(0.5, -0.25)
        c.plunger_pos(0.9)
        time.sleep(0.1)
        cmds = mock.commands()
        check({"cmd": "flip_left", "press": 1} in cmds, "flip_left press encoded")
        check({"cmd": "flip_right", "press": 0} in cmds, "flip_right release encoded")
        check({"cmd": "launch", "press": 1} in cmds, "launch encoded")
        check({"cmd": "nudge_left", "press": 1} in cmds, "nudge_left encoded")
        check(any(x.get("cmd") == "nudge_accel" and x.get("x") == 0.5 and x.get("y") == -0.25
                  for x in cmds), "nudge_accel x/y encoded")
        check(any(x.get("cmd") == "plunger_pos" and x.get("value") == 0.9 for x in cmds),
              "plunger_pos value encoded")
        c.close()


class _RecordingBridge:
    """Stand-in for BridgeClient that records actuator calls (no socket)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def flip_left(self, p): self.calls.append(("flip_left", p))
    def flip_right(self, p): self.calls.append(("flip_right", p))
    def launch(self, p): self.calls.append(("launch", p))
    def nudge_left(self, p): self.calls.append(("nudge_left", p))
    def nudge_right(self, p): self.calls.append(("nudge_right", p))
    def nudge_center(self, p): self.calls.append(("nudge_center", p))


def test_actuator_diffing():
    print("test_actuator_diffing")
    b = _RecordingBridge()
    a = Actuators(b)
    a.apply(DesiredActions(left=True))         # press left
    a.apply(DesiredActions(left=True))         # hold: no new command
    a.apply(DesiredActions(left=True, right=True))  # add right
    a.apply(DesiredActions())                  # release both
    check(b.calls == [("flip_left", True), ("flip_right", True),
                      ("flip_left", False), ("flip_right", False)],
          f"only edges are sent; holding sends nothing (got {b.calls})")


def test_nudge_edge():
    print("test_nudge_edge")
    b = _RecordingBridge()
    a = Actuators(b)
    a.apply(DesiredActions(nudge=vpxbot.NUDGE_LEFT))   # rising edge -> press
    a.apply(DesiredActions(nudge=vpxbot.NUDGE_LEFT))   # held -> release pulse, no re-fire
    a.apply(DesiredActions(nudge=vpxbot.NUDGE_LEFT))   # still held -> nothing new
    a.apply(DesiredActions(nudge=None))                # re-arm
    a.apply(DesiredActions(nudge=vpxbot.NUDGE_LEFT))   # new edge -> press again
    presses = [c for c in b.calls if c[1] is True]
    check(presses == [("nudge_left", True), ("nudge_left", True)],
          f"one nudge impulse per rising edge (got presses {presses})")
    check(("nudge_left", False) in b.calls, "nudge press is released (one-tick pulse)")


def main():
    tests = [
        test_geometry_decode,
        test_multiball_tracking,
        test_freshness_drop,
        test_event_coalescing,
        test_command_encoding,
        test_actuator_diffing,
        test_nudge_edge,
    ]
    for t in tests:
        t()
    print("=" * 60)
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
