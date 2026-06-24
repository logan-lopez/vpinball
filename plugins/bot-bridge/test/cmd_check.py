#!/usr/bin/env python3
"""Exercise the M2 command surface and report which paths actually move the engine."""
import json, socket, time

s = socket.create_connection(("127.0.0.1", 13501), timeout=10.0)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
f = s.makefile("rw", buffering=1, encoding="utf-8", newline="\n")


def next_state(timeout=1.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
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


def send(obj):
    f.write(json.dumps(obj) + "\n"); f.flush()


def plunger_pos(st):
    return st["plungers"][0]["pos"] if st and st["plungers"] else None


def nudge(st):
    n = st.get("nudge", {}) if st else {}
    return (n.get("ax", 0.0), n.get("ay", 0.0))


def ball_speed(st):
    b = st["balls"]
    if not b:
        return None
    import math
    return math.hypot(b[0]["vx"], b[0]["vy"])


print("=== digital launch (plunger pull/fire) ===")
base = plunger_pos(next_state())
send({"cmd": "launch", "press": 1})
mx = base or 0.0
for _ in range(30):
    st = next_state(0.05)
    p = plunger_pos(st)
    if p is not None:
        mx = max(mx, p)
send({"cmd": "launch", "press": 0})
print(f"  plunger pos: rest~{base} -> max while held {mx:.3f}  ({'PULLED' if base is not None and mx > base + 0.05 else 'no change'})")

print("=== digital nudge_left ===")
nb = nudge(next_state())
send({"cmd": "nudge_left", "press": 1})
peak = 0.0
for _ in range(20):
    st = next_state(0.05)
    ax, ay = nudge(st)
    peak = max(peak, abs(ax) + abs(ay))
send({"cmd": "nudge_left", "press": 0})
print(f"  nudge accel peak |ax|+|ay|: {peak:.4f}  ({'APPLIED' if peak > 1e-4 else 'no change'})")

print("=== analog nudge_accel override (x=0.8) ===")
send({"cmd": "nudge_accel", "x": 0.8, "y": 0.0})
peak = 0.0
for _ in range(20):
    st = next_state(0.05)
    ax, ay = nudge(st)
    peak = max(peak, abs(ax))
send({"cmd": "nudge_accel", "x": 0.0, "y": 0.0})
print(f"  nudge accel peak |ax|: {peak:.4f}  ({'APPLIED' if peak > 1e-4 else 'no effect (needs mapped sensor)'})")

print("=== analog plunger_pos override (value=0.9) ===")
base = plunger_pos(next_state())
send({"cmd": "plunger_pos", "value": 0.9})
mx = base or 0.0
for _ in range(30):
    st = next_state(0.05)
    p = plunger_pos(st)
    if p is not None:
        mx = max(mx, p)
print(f"  plunger pos: rest~{base} -> max {mx:.3f}  ({'MOVED' if base is not None and mx > base + 0.05 else 'no effect (needs mech plunger)'})")

s.close()
print("=== done ===")
