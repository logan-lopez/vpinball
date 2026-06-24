#!/usr/bin/env python3
"""VPX bot bridge debug console — verify the bridge by hand.

A two-way tool built on the shared vpxbot client: it shows decoded telemetry live
(rate-limited, human-readable) and lets you fire flippers / plunger / nudge from
the keyboard. Use it to sanity-check the bridge before trusting a bot:

  * Nudge the ball and watch x/y move — is the coordinate frame sane? (+X right,
    +Y down toward the drain.)
  * Confirm active-ball position matches where the ball really is.
  * In multiball, confirm each ball keeps a stable id.
  * Press a flipper and watch its angle / solenoid change in telemetry.

Keys (no Enter needed):
    a / l      toggle LEFT / RIGHT flipper up-down (toggle = you can cradle)
    p          toggle plunger pulled; toggle again to release (fire)
    z / x / c  nudge LEFT / FORWARD / RIGHT (one impulse)
    t          tilt (one impulse)
    g          dump full static geometry
    space      release everything (panic release)
    h          show this help
    q / Esc    quit

NOTE: the bridge serves one client at a time — close any running bot before
using the console (and vice-versa).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vpxbot  # noqa: E402


# --------------------------------------------------------------------------- #
# Cross-platform single-key input + ANSI screen control
# --------------------------------------------------------------------------- #
def _enable_vt_on_windows() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


if os.name == "nt":
    import msvcrt

    class KeyReader:
        def __enter__(self): return self
        def __exit__(self, *exc): pass

        def get(self):
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):   # function/arrow key prefix
                    msvcrt.getwch()
                    return None
                return ch
            return None
else:
    import termios
    import tty
    import select

    class KeyReader:
        def __enter__(self):
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            return self

        def __exit__(self, *exc):
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

        def get(self):
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.read(1)
            return None


HOME_CLEAR = "\033[H\033[J"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(bridge: vpxbot.BridgeClient, left: bool, right: bool, plunger: bool) -> str:
    state = bridge.latest_state()
    geo = bridge.geometry
    lamp_names = geo.lamp_names if geo else []
    lines = []
    lines.append("VPX BOT BRIDGE — DEBUG CONSOLE   (h=help  q=quit)")
    lines.append("-" * 64)
    st = bridge.stats
    lines.append(f"link: {'CONNECTED' if st['connected'] else 'waiting...'}   "
                 f"frames={st['frames_received']}  dropped={st['frames_dropped']}")
    lines.append(f"manual actuators:  LEFT={'UP' if left else 'dn'}  "
                 f"RIGHT={'UP' if right else 'dn'}  PLUNGER={'PULLED' if plunger else 'rest'}")
    lines.append("-" * 64)

    if state is None:
        lines.append("no telemetry yet — is a table running with the BotBridge plugin enabled?")
        return "\n".join(lines)

    lines.append(f"tick={state.tick}  time={state.time:7.2f}s   balls={len(state.balls)}")
    if not state.balls:
        lines.append("  (no balls on the playfield)")
    for b in state.balls:
        lines.append(
            f"  ball #{b.id}: pos=({b.x:7.1f},{b.y:7.1f},{b.z:6.1f})  "
            f"vel=({b.vx:7.2f},{b.vy:7.2f})  speed={b.speed_vpt:6.2f} VPU/tick  r={b.radius:.0f}")
    for f in state.flippers:
        lines.append(f"  flipper[{f.i}]: angle={f.angle:7.2f}  speed={f.angle_speed:6.2f}  "
                     f"solenoid={int(f.solenoid)}  eos={int(f.end_of_stroke)}")
    for p in state.plungers:
        lines.append(f"  plunger[{p.i}]: pos={p.pos:.3f}  posVPU={p.pos_vpu:7.1f}  "
                     f"speed={p.speed:6.2f}  rest={p.rest:.3f}")
    n = state.nudge
    lines.append(f"  nudge: accel=({n.ax:6.3f},{n.ay:6.3f})  tableVel=({n.vx:6.3f},{n.vy:6.3f})  "
                 f"tilt={int(n.tilt)}  slam={int(n.slam)}  plumbCount={n.plumb_count}")
    lit = sum(1 for l in state.lamps if l.lit)
    lines.append(f"  lamps: {lit}/{len(state.lamps)} lit")
    if state.events:
        lines.append(f"  events this read ({len(state.events)}):")
        for e in state.events[-6:]:
            name = e.name or "?"
            lines.append(f"    t={e.t:8.2f}  {e.type_name}/{e.kind_name}  '{name}'  s={e.scalar:.2f}")
    return "\n".join(lines)


def dump_geometry(bridge: vpxbot.BridgeClient) -> None:
    geo = bridge.geometry
    print(HOME_CLEAR, end="")
    if geo is None:
        print("no geometry received yet.")
    else:
        print("STATIC GEOMETRY")
        print(f"  {len(geo.parts)} collidable parts, {len(geo.lamps)} lamps")
        for f in geo.flippers:
            print(f"  flipper '{f.name}': pivot=({f.pivot_x:.0f},{f.pivot_y:.0f}) "
                  f"len={f.length_max:.0f} start={f.start_angle:.1f} end={f.end_angle:.1f}")
        for r in geo.ramps:
            print(f"  ramp '{r.name}': entrance=({r.entrance_x:.0f},{r.entrance_y:.0f},z{r.entrance_z:.0f}) "
                  f"exit=({r.exit_x:.0f},{r.exit_y:.0f},z{r.exit_z:.0f})")
        for k in geo.kickers:
            print(f"  kicker '{k.name}': center=({k.x:.0f},{k.y:.0f}) r={k.radius:.0f}")
        for bm in geo.bumpers:
            print(f"  bumper '{bm.name}': center=({bm.x:.0f},{bm.y:.0f}) r={bm.radius:.0f}")
        print(f"  triggers={len(geo.triggers)} gates={len(geo.gates)} spinners={len(geo.spinners)} "
              f"targets={len(geo.targets)} surfaces={len(geo.surfaces)} primitives={len(geo.primitives)}")
    print("\n(press any key to return to the live view)")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="VPX bot bridge debug console")
    ap.add_argument("--host", default=vpxbot.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=vpxbot.DEFAULT_PORT)
    ap.add_argument("--rate", type=float, default=10.0, help="display refresh Hz")
    args = ap.parse_args()

    _enable_vt_on_windows()
    left = right = plunger = False
    nudge_releases: list[tuple[float, str]] = []  # (release_time, "left"|"right"|"center")
    paused_for_geometry = False

    with vpxbot.BridgeClient(host=args.host, port=args.port) as bridge, KeyReader() as keys:
        period = 1.0 / args.rate
        next_render = time.perf_counter()
        try:
            while True:
                now = time.perf_counter()

                # Fire pending nudge releases (one-tick momentary pulse).
                for rel_t, direction in list(nudge_releases):
                    if now >= rel_t:
                        _press_nudge(bridge, direction, False)
                        nudge_releases.remove((rel_t, direction))

                ch = keys.get()
                if ch is not None:
                    if paused_for_geometry:
                        paused_for_geometry = False
                        next_render = 0.0       # force immediate redraw
                    elif ch in ("q", "\x1b"):
                        break
                    elif ch == "a":
                        left = not left; bridge.flip_left(left)
                    elif ch == "l":
                        right = not right; bridge.flip_right(right)
                    elif ch == "p":
                        plunger = not plunger; bridge.launch(plunger)
                    elif ch in ("z", "x", "c"):
                        direction = {"z": "left", "x": "center", "c": "right"}[ch]
                        _press_nudge(bridge, direction, True)
                        nudge_releases.append((now + 0.05, direction))
                    elif ch == "t":
                        bridge.tilt(True)
                        nudge_releases.append((now + 0.05, "tilt"))
                    elif ch == "g":
                        dump_geometry(bridge)
                        paused_for_geometry = True
                    elif ch == "h":
                        _print_help()
                        paused_for_geometry = True

                if not paused_for_geometry and now >= next_render:
                    sys.stdout.write(HOME_CLEAR + render(bridge, left, right, plunger) + "\n")
                    sys.stdout.flush()
                    next_render = now + period

                time.sleep(0.002)
        except KeyboardInterrupt:
            pass
        finally:
            # Panic release on the way out.
            bridge.flip_left(False)
            bridge.flip_right(False)
            bridge.launch(False)
            print("\nconsole closed.")


def _press_nudge(bridge: vpxbot.BridgeClient, direction: str, press: bool) -> None:
    if direction == "left":
        bridge.nudge_left(press)
    elif direction == "right":
        bridge.nudge_right(press)
    elif direction == "center":
        bridge.nudge_center(press)
    elif direction == "tilt":
        bridge.tilt(press)


def _print_help() -> None:
    print(HOME_CLEAR + __doc__.strip() + "\n\n(press any key to return to the live view)")


if __name__ == "__main__":
    main()
