#!/usr/bin/env python3
"""VPX Bot Bridge — Milestone 1 round-trip latency test.

Measures the end-to-end control latency that caps how well any bot can play:

    send "flip left"  (timestamp T0)
      -> plugin applies SetActionState on the next physics tick
      -> the left flipper starts rotating in-engine
      -> the new angle appears in a telemetry line we receive (timestamp T1)
    latency = T1 - T0

Connects to the bot-bridge plugin's loopback TCP server (NDJSON), watches the
flipper angles, and reports min / median / mean over several trials.

Usage:  python latency_test.py [--host 127.0.0.1] [--port 13501] [--trials 20]
"""
import argparse
import json
import socket
import statistics
import time


def read_lines(sockfile):
    """Yield (recv_perf_time, parsed_json) for each telemetry line."""
    for raw in sockfile:
        t = time.perf_counter()
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield t, json.loads(raw)
        except json.JSONDecodeError:
            continue


def max_flipper_angle(frame):
    fl = frame.get("flippers", [])
    if not fl:
        return None
    return max(f["angle"] for f in fl)


def angles(frame):
    return [f["angle"] for f in frame.get("flippers", [])]


def wait_for_movement(gen, baseline, threshold, deadline):
    """Return recv time of first frame whose any flipper deviates > threshold."""
    for t, frame in gen:
        a = angles(frame)
        if a and any(abs(a[i] - baseline[i]) > threshold for i in range(min(len(a), len(baseline)))):
            return t, frame
        if time.perf_counter() > deadline:
            return None, None
    return None, None


def settle(gen, target_count):
    """Drain frames briefly; return the latest frame's angles as a baseline."""
    last = None
    end = time.perf_counter() + 0.3
    for t, frame in gen:
        last = frame
        if time.perf_counter() > end:
            break
    return angles(last) if last else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13501)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--threshold", type=float, default=2.0, help="degrees of movement that count as 'flipper moved'")
    args = ap.parse_args()

    s = socket.create_connection((args.host, args.port), timeout=5.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sockfile = s.makefile("r", buffering=1, encoding="utf-8", newline="\n")
    gen = read_lines(sockfile)

    # Wait until we actually see flippers (i.e. a game is running).
    print("Waiting for telemetry with flippers ...")
    t0 = time.perf_counter()
    base = []
    while time.perf_counter() - t0 < 15.0:
        try:
            _, frame = next(gen)
        except StopIteration:
            print("Telemetry stream ended."); return
        if frame.get("flippers"):
            base = angles(frame)
            print(f"Connected. schema v{frame.get('v')}, "
                  f"{len(frame.get('balls', []))} ball(s), {len(base)} flipper(s).")
            break
    if not base:
        print("No flippers seen in telemetry — is a table running with the plugin enabled?")
        return

    latencies_ms = []
    for trial in range(args.trials):
        # Re-baseline from a settled resting state.
        baseline = settle(gen, 0)
        if not baseline:
            continue

        # Send flip-left, timestamp the send.
        t_send = time.perf_counter()
        s.sendall(b'{"cmd":"flip_left","press":1}\n')

        t_move, frame = wait_for_movement(gen, baseline, args.threshold, t_send + 1.0)
        # Release.
        s.sendall(b'{"cmd":"flip_left","press":0}\n')

        if t_move is None:
            print(f"trial {trial+1:2d}: no movement observed (timeout)")
        else:
            ms = (t_move - t_send) * 1000.0
            latencies_ms.append(ms)
            print(f"trial {trial+1:2d}: {ms:6.2f} ms")

        # Let the flipper fall back to rest before the next trial.
        time.sleep(0.25)

    s.close()

    if latencies_ms:
        print("\n=== round-trip latency (send flip -> observe flipper move in telemetry) ===")
        print(f"  samples : {len(latencies_ms)}")
        print(f"  min     : {min(latencies_ms):6.2f} ms")
        print(f"  median  : {statistics.median(latencies_ms):6.2f} ms")
        print(f"  mean    : {statistics.mean(latencies_ms):6.2f} ms")
        print(f"  max     : {max(latencies_ms):6.2f} ms")
    else:
        print("No latency samples collected.")


if __name__ == "__main__":
    main()
