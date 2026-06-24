#!/usr/bin/env python3
"""Verify the discrete hit/switch event stream: jostle the ball with nudges, then
tally the 'events' arrays from the state frames."""
import json, socket, time
from collections import Counter

ITEM = {0: "surface", 1: "flipper", 3: "plunger", 5: "bumper", 6: "trigger",
        7: "light", 8: "kicker", 10: "gate", 11: "spinner", 12: "ramp",
        19: "primitive", 21: "rubber", 22: "hittarget"}
KIND = {0: "hit", 1: "unhit", 2: "slingshot", 3: "spin", 4: "eos", 5: "bos", 6: "flipperCollide"}

s = socket.create_connection(("127.0.0.1", 13501), timeout=10.0)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
f = s.makefile("rw", buffering=1, encoding="utf-8", newline="\n")


def send(o):
    f.write(json.dumps(o) + "\n"); f.flush()


events = []
by = Counter()
samples = []
t0 = time.perf_counter()
nudge_seq = ["nudge_left", "nudge_right", "nudge_center"]
ni = 0
last_nudge = 0.0

# Jostle the ball for ~6s and collect events.
while time.perf_counter() - t0 < 6.0:
    now = time.perf_counter()
    if now - last_nudge > 0.25:
        cmd = nudge_seq[ni % 3]; ni += 1
        send({"cmd": cmd, "press": 1}); send({"cmd": cmd, "press": 0})
        # occasional flips too
        send({"cmd": "flip_left", "press": 1}); send({"cmd": "flip_left", "press": 0})
        last_nudge = now
    line = f.readline()
    if not line:
        break
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        continue
    if o.get("type") != "state":
        continue
    for e in o.get("events", []):
        events.append(e)
        by[(ITEM.get(e["type"], e["type"]), KIND.get(e["kind"], e["kind"]))] += 1
        if len(samples) < 12:
            samples.append(e)

s.close()
print(f"=== collected {len(events)} hit/switch events in ~6s ===")
print("by (partType, kind):")
for k, n in by.most_common():
    print(f"  {k[0]:>10} / {k[1]:<10} : {n}")
print("samples:")
for e in samples:
    print(f"  t={e['t']:.3f} {ITEM.get(e['type'],e['type'])}/{KIND.get(e['kind'],e['kind'])} "
          f"name='{e['name']}' scalar={e['scalar']:.2f}")
print("=== done ===" if events else "=== NO EVENTS (ball may not have hit anything) ===")
