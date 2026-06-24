#!/usr/bin/env python3
"""Latency & throughput under load — re-confirm sub-5ms when it's busy.

Latency that is clean at rest but degrades under multiball + a busy light show is
a classic way for "the bots got worse in multiball" to actually be the bridge (or
the client) buckling. This tool measures it two ways:

LIVE mode (default) — against a running table + the BotBridge plugin:
    Repeatedly send a flip and time how long until the flipper's motion shows up
    in telemetry (the same round-trip a bot's reflexes ride on), while reporting
    the live telemetry load it observes: frame rate, balls, dropped frames,
    bytes/s. Induce the load yourself — start multiball and a light show (or let
    panic_bot.py keep balls going) — and watch whether the round-trip holds.

        python latency_load_test.py                 # measure on the real bridge
        python latency_load_test.py --duration 60

MOCK mode — no table required, validates the *client* path can sustain the load:
    Streams a synthetic 1 kHz multiball + full-lamp frame set and checks the
    client ingests it without backing up, how stale the freshest frame is when a
    policy reads it, and the per-read decode cost.

        python latency_load_test.py --mock --rate 1000 --balls 6 --lamps 200
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vpxbot  # noqa: E402


# --------------------------------------------------------------------------- #
# LIVE: round-trip latency while reporting observed load
# --------------------------------------------------------------------------- #
def run_live(args):
    with vpxbot.BridgeClient(host=args.host, port=args.port) as bridge:
        print(f"[load] connecting to {args.host}:{args.port} - waiting for a running table...")
        if not bridge.wait_until_ready(timeout=60.0):
            print("[load] no telemetry: is a table running with the BotBridge plugin enabled?")
            return
        print("[load] connected. Induce load now (multiball + light show, or play the table).")
        print("[load] measuring round-trip latency; Ctrl-C to stop.\n")

        # Two metrics per flip:
        #   applied = send -> the coil energizes in telemetry (solenoid=1). This is
        #             the TRUE bridge round-trip: transport + tick + telemetry, with
        #             no flipper mechanics. This is the number that should be < 5 ms.
        #   motion  = send -> the flipper has rotated > threshold degrees. This adds
        #             the solenoid coil ramp (mechanical), so it is always larger and
        #             is dominated by physics, not the bridge.
        applied: list[float] = []
        motion: list[float] = []
        last_report = time.perf_counter()
        last_frames = bridge.stats["frames_received"]
        last_bytes = bridge.stats["bytes_received"]
        start = time.perf_counter()

        try:
            while time.perf_counter() - start < args.duration:
                a, m = _measure_one_rtt(bridge, args.threshold)
                if a is not None:
                    applied.append(a)
                if m is not None:
                    motion.append(m)

                now = time.perf_counter()
                if now - last_report >= 1.0:
                    st = bridge.stats
                    s = bridge.latest_state()
                    dt = now - last_report
                    fps = (st["frames_received"] - last_frames) / dt
                    kbps = (st["bytes_received"] - last_bytes) / dt / 1024.0
                    last_report, last_frames, last_bytes = now, st["frames_received"], st["bytes_received"]
                    nballs = len(s.balls) if s else 0
                    nlamps = len(s.lamps) if s else 0
                    med_a = statistics.median(applied[-25:]) if applied else float("nan")
                    med_m = statistics.median(motion[-25:]) if motion else float("nan")
                    print(f"  load: {fps:6.0f} fps  {nballs} ball(s)  {nlamps} lamps  "
                          f"{kbps:6.0f} KiB/s  dropped={st['frames_dropped']:>7}   |   "
                          f"applied={med_a:5.2f}ms  motion={med_m:5.2f}ms  (n={len(applied)})")

                time.sleep(args.gap)
        except KeyboardInterrupt:
            pass
        finally:
            bridge.flip_left(False)

        _report_rtt("applied (send -> coil energized; the bridge round-trip)", applied, gate=5.0)
        _report_rtt("motion  (send -> flipper rotated; includes coil ramp)", motion, gate=None)


def _measure_one_rtt(bridge: vpxbot.BridgeClient, threshold: float):
    """Send a flip and time two things: when the coil energizes (solenoid=1, the
    bridge round-trip) and when the flipper has rotated > threshold degrees (adds
    the mechanical coil ramp). Returns (applied_ms, motion_ms), either may be None."""
    base = bridge.latest_state()
    if base is None or not base.flippers:
        time.sleep(0.05)
        return None, None
    # Need a clean resting baseline: no coil already energized.
    if any(f.solenoid for f in base.flippers):
        time.sleep(0.1)
        return None, None
    baseline = [f.angle for f in base.flippers]

    t0 = time.perf_counter()
    bridge.flip_left(True)
    applied_at = None
    motion_at = None
    deadline = t0 + 1.0
    while time.perf_counter() < deadline and (applied_at is None or motion_at is None):
        s = bridge.wait_for_state(timeout=1.0)
        if s is None:
            break
        now = time.perf_counter()
        if applied_at is None and any(f.solenoid for f in s.flippers):
            applied_at = now
        if motion_at is None and any(abs(f.angle - baseline[i]) > threshold
                                     for i, f in enumerate(s.flippers) if i < len(baseline)):
            motion_at = now
    bridge.flip_left(False)
    time.sleep(0.20)  # let it fall back before the next trial
    return ((applied_at - t0) * 1000.0 if applied_at is not None else None,
            (motion_at - t0) * 1000.0 if motion_at is not None else None)


def _report_rtt(label: str, rtts: list[float], gate):
    print(f"\n=== {label} ===")
    if not rtts:
        print("  no samples")
        return
    rtts_sorted = sorted(rtts)
    p95 = rtts_sorted[max(0, int(len(rtts_sorted) * 0.95) - 1)]
    print(f"  samples : {len(rtts)}")
    print(f"  min     : {min(rtts):6.2f} ms")
    print(f"  median  : {statistics.median(rtts):6.2f} ms")
    print(f"  mean    : {statistics.mean(rtts):6.2f} ms")
    print(f"  p95     : {p95:6.2f} ms")
    print(f"  max     : {max(rtts):6.2f} ms")
    if gate is not None:
        print(f"  -> sub-{gate:.0f}ms holds: {'YES' if statistics.median(rtts) < gate else 'NO'}")


# --------------------------------------------------------------------------- #
# MOCK: can the client sustain a 1 kHz multiball + light-show load?
# --------------------------------------------------------------------------- #
def run_mock(args):
    import json
    from tests.mock_bridge import MockBridge

    # (1) Parse-capacity micro-benchmark: the reader thread does one json.loads per
    # frame. This is the client's true ingest ceiling, independent of how fast any
    # producer can push. (The Python mock below cannot itself produce 1 kHz of 10 KB
    # frames; the real C++ bridge can, so this is the number that matters.)
    sample = _sample_state_frame(args.balls, args.lamps)
    sample_bytes = sample.encode("utf-8")
    iters = 20000
    t0 = time.perf_counter()
    for _ in range(iters):
        json.loads(sample_bytes)
    dt = time.perf_counter() - t0
    parse_us = dt / iters * 1e6
    parse_fps = 1.0 / (dt / iters)
    print("=== client parse capacity (reader hot path) ===")
    print(f"  frame size          : {len(sample_bytes)} B  ({args.balls} balls, {args.lamps} lamps)")
    print(f"  json.loads cost     : {parse_us:6.1f} us/frame")
    print(f"  parse ceiling       : {parse_fps:8.0f} frames/s  "
          f"({'OK for 1 kHz' if parse_fps > 1000 else 'BELOW 1 kHz'})")
    print()

    # (2) Live socket run through the mock (limited by the Python producer's speed),
    # to confirm freshness behaviour end to end over a real loopback socket.
    balls = [MockBridge.make_ball(i, x=100.0 + 60 * i, y=900.0 + 30 * i, vx=1.0, vy=2.0)
             for i in range(args.balls)]
    with MockBridge(port=0, rate_hz=args.rate, n_lamps=args.lamps) as mock:
        mock.set_balls(balls)
        mock.set_flood(True)  # push as fast as the producer can
        with vpxbot.BridgeClient(host="127.0.0.1", port=mock.port, auto_reconnect=False) as c:
            if not c.wait_until_ready(timeout=5.0):
                print("[mock] client never got a frame"); return
            print(f"[mock] flooding {args.balls} balls + {args.lamps} lamps over loopback "
                  f"for {args.duration:.0f}s; policy reads at {args.hz:.0f} Hz\n")

            decode_times: list[float] = []
            stale_ticks: list[int] = []
            period = 1.0 / args.hz
            start = time.perf_counter()
            next_t = start
            while time.perf_counter() - start < args.duration:
                t0 = time.perf_counter()
                s = c.latest_state()
                decode_times.append((time.perf_counter() - t0) * 1e6)
                if s is not None:
                    stale_ticks.append(mock._tick - s.tick)
                next_t += period
                d = next_t - time.perf_counter()
                if d > 0:
                    time.sleep(d)

            st = c.stats
            elapsed = time.perf_counter() - start
            produced = mock._tick
            recv_fps = st["frames_received"] / elapsed
            print("=== client ingest over loopback socket ===")
            print(f"  producer reached    : {produced / elapsed:6.0f} fps "
                  f"(Python mock ceiling, NOT the C++ bridge)")
            print(f"  client received     : {st['frames_received']} ({recv_fps:6.0f} fps)")
            print(f"  ingest completeness : {100.0 * st['frames_received'] / max(produced, 1):5.1f}% "
                  f"(client kept up with the producer)")
            print(f"  dropped as stale    : {st['frames_dropped']} (expected: latest-wins, slow policy)")
            print(f"  bytes/frame         : ~{st['bytes_received'] / max(st['frames_received'],1):6.0f} B")
            if decode_times:
                print(f"  latest_state() cost : median {statistics.median(decode_times):6.1f} us  "
                      f"max {max(decode_times):6.1f} us")
            if stale_ticks:
                lag = statistics.median(stale_ticks)
                print(f"  freshness lag       : median {lag:4.0f} frames behind the newest received")
            completeness = st["frames_received"] / max(produced, 1)
            print(f"  -> client keeps up with the producer: "
                  f"{'YES' if completeness > 0.98 else 'NO (reader backing up)'}")


def _sample_state_frame(n_balls: int, n_lamps: int) -> str:
    import json
    from tests.mock_bridge import MockBridge
    m = MockBridge(n_lamps=n_lamps)
    m.set_balls([MockBridge.make_ball(i) for i in range(n_balls)])
    return m._state_frame()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=vpxbot.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=vpxbot.DEFAULT_PORT)
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to run")
    ap.add_argument("--threshold", type=float, default=2.0, help="deg of motion that counts as 'flipped' (live)")
    ap.add_argument("--gap", type=float, default=0.25, help="seconds between RTT trials (live)")
    ap.add_argument("--mock", action="store_true", help="offline client-throughput test (no table)")
    ap.add_argument("--rate", type=float, default=1000.0, help="mock stream rate, Hz")
    ap.add_argument("--balls", type=int, default=6, help="mock ball count (multiball)")
    ap.add_argument("--lamps", type=int, default=200, help="mock lamp count (light show)")
    ap.add_argument("--hz", type=float, default=250.0, help="mock policy rate, Hz")
    args = ap.parse_args()

    if args.mock:
        run_mock(args)
    else:
        run_live(args)


if __name__ == "__main__":
    main()
