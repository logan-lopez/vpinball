#!/usr/bin/env python3
"""VPX Bot Bridge — Milestone 2 verification.

Connects to the plugin, validates the v2 schema (static geometry + lamp directory,
and the expanded per-tick state), and exercises a couple of commands.

Usage:  python m2_check.py [--host 127.0.0.1] [--port 13501]
"""
import argparse
import json
import socket
import time

ITEM = {0: "surface", 1: "flipper", 3: "plunger", 5: "bumper", 6: "trigger",
        7: "light", 8: "kicker", 10: "gate", 11: "spinner", 12: "ramp",
        19: "primitive", 20: "flasher", 21: "rubber", 22: "hittarget"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13501)
    args = ap.parse_args()

    s = socket.create_connection((args.host, args.port), timeout=10.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    f = s.makefile("rw", buffering=1, encoding="utf-8", newline="\n")

    static = None
    state = None
    t0 = time.perf_counter()
    while (static is None or state is None) and time.perf_counter() - t0 < 15:
        line = f.readline()
        if not line:
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "static" and static is None:
            static = obj
        elif obj.get("type") == "state" and state is None:
            state = obj

    print("=" * 60)
    if static:
        geom = static.get("geometry", [])
        by_type = {}
        for g in geom:
            by_type[ITEM.get(g["type"], g["type"])] = by_type.get(ITEM.get(g["type"], g["type"]), 0) + 1
        print(f"STATIC frame: {len(geom)} collidable parts, {len(static.get('lamps', []))} lamps")
        print("  parts by type:", dict(sorted(by_type.items(), key=lambda kv: -kv[1])))
        for g in geom:
            if g["type"] == 1:  # flipper
                print(f"  flipper '{g['name']}': pivot=({g['x']:.0f},{g['y']:.0f}) lenMax={g['a']:.0f} "
                      f"baseR={g['b']:.0f} endR={g['c']:.0f} start={g['d']:.1f}deg end={g['ex']:.1f}deg")
        ramps = [g for g in geom if g["type"] == 12]
        for g in ramps[:3]:
            print(f"  ramp '{g['name']}': entrance=({g['x']:.0f},{g['y']:.0f},z{g['z']:.0f}) "
                  f"exit=({g['ex']:.0f},{g['ey']:.0f},z{g['ez']:.0f})")
        kickers = [g for g in geom if g["type"] == 8]
        for g in kickers[:3]:
            print(f"  kicker '{g['name']}': center=({g['x']:.0f},{g['y']:.0f}) r={g['a']:.0f}")
        if static.get("lamps"):
            print("  first lamps:", [l["name"] for l in static["lamps"][:6]])
    else:
        print("NO STATIC FRAME RECEIVED")

    print("-" * 60)
    if state:
        print(f"STATE frame: tick={state['tick']} time={state['time']:.2f}")
        print(f"  balls={len(state['balls'])} flippers={len(state['flippers'])} "
              f"plungers={len(state['plungers'])} lamps={len(state['lamps'])}")
        for fl in state["flippers"]:
            print(f"  flipper[{fl['i']}]: angle={fl['angle']:.2f} angleSpeed={fl['angleSpeed']:.3f} "
                  f"solenoid={fl['solenoid']} eos={fl['eos']}")
        for pl in state["plungers"]:
            print(f"  plunger[{pl['i']}]: pos={pl['pos']:.3f} posVPU={pl['posVPU']:.1f} speed={pl['speed']:.3f} rest={pl['rest']:.3f}")
        n = state.get("nudge", {})
        print(f"  nudge: accel=({n.get('ax'):.4f},{n.get('ay'):.4f}) vel=({n.get('vx'):.4f},{n.get('vy'):.4f}) "
              f"tilt={n.get('tilt')} plumbSim={n.get('plumbSim')}")
        lit = sum(1 for l in state["lamps"] if l["lit"])
        print(f"  lamps lit now: {lit}/{len(state['lamps'])}")
    else:
        print("NO STATE FRAME RECEIVED")

    # --- command test: flip right, observe movement + solenoid ---
    print("-" * 60)
    print("COMMAND TEST: flip_right")

    def read_state(deadline):
        while time.perf_counter() < deadline:
            line = f.readline()
            if not line:
                return None
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "state":
                return o
        return None

    base = read_state(time.perf_counter() + 1.0)
    baseang = [fl["angle"] for fl in base["flippers"]] if base else []
    t_send = time.perf_counter()
    f.write('{"cmd":"flip_right","press":1}\n'); f.flush()
    moved = None
    sol_seen = False
    while time.perf_counter() - t_send < 1.0:
        o = read_state(time.perf_counter() + 1.0)
        if not o:
            break
        if any(fl["solenoid"] for fl in o["flippers"]):
            sol_seen = True
        if baseang and any(abs(o["flippers"][i]["angle"] - baseang[i]) > 2.0
                           for i in range(min(len(baseang), len(o["flippers"])))):
            moved = (time.perf_counter() - t_send) * 1000.0
            break
    f.write('{"cmd":"flip_right","press":0}\n'); f.flush()
    print(f"  solenoid energized observed: {sol_seen}")
    print(f"  flipper moved >2deg after: {moved:.2f} ms" if moved else "  flipper did NOT move (unexpected)")

    s.close()
    print("=" * 60)
    print("done")


if __name__ == "__main__":
    main()
