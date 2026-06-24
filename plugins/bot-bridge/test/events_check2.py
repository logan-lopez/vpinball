#!/usr/bin/env python3
"""Drive the ball hard with a sustained rotating analog nudge to generate many hits,
then tally the event stream."""
import json, math, socket, time
from collections import Counter

ITEM = {0: "surface", 1: "flipper", 3: "plunger", 5: "bumper", 6: "trigger", 7: "light",
        8: "kicker", 10: "gate", 11: "spinner", 12: "ramp", 19: "primitive", 21: "rubber", 22: "hittarget"}
KIND = {0: "hit", 1: "unhit", 2: "slingshot", 3: "spin", 4: "eos", 5: "bos", 6: "flipperCollide"}

s = socket.create_connection(("127.0.0.1", 13501), timeout=10.0)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
f = s.makefile("rw", buffering=1, encoding="utf-8", newline="\n")
def send(o): f.write(json.dumps(o) + "\n"); f.flush()

by = Counter(); names = Counter(); total = 0; samples = []
t0 = time.perf_counter(); last = 0.0; ang = 0.0
while time.perf_counter() - t0 < 12.0:
    now = time.perf_counter()
    if now - last > 0.05:
        ang += 0.7
        send({"cmd": "nudge_accel", "x": 8.0 * math.cos(ang), "y": 8.0 * math.sin(ang)})
        last = now
    line = f.readline()
    if not line: break
    try: o = json.loads(line)
    except json.JSONDecodeError: continue
    if o.get("type") != "state": continue
    for e in o.get("events", []):
        total += 1
        by[(ITEM.get(e["type"], e["type"]), KIND.get(e["kind"], e["kind"]))] += 1
        names[e["name"]] += 1
        if len(samples) < 14: samples.append(e)
send({"cmd": "nudge_accel", "x": 0.0, "y": 0.0})
s.close()

print(f"=== {total} hit/switch events in ~12s ===")
print("by (partType, kind):")
for k, n in by.most_common(): print(f"  {k[0]:>10} / {k[1]:<10} : {n}")
print(f"distinct parts hit: {len(names)}  e.g. {list(names)[:10]}")
print("samples:")
for e in samples:
    print(f"  t={e['t']:.2f} {ITEM.get(e['type'],e['type'])}/{KIND.get(e['kind'],e['kind'])} '{e['name']}' s={e['scalar']:.1f}")
