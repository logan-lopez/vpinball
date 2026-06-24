"""End-to-end check: the reference panic bot actually closes decide -> action.

Runs panic_bot.py as a real subprocess against the mock bridge, places a ball in
the lower-left, and confirms the bot raises the left flipper. This exercises the
whole stack (runner loop + Actuators diffing + client send) the way a live table
would, minus the physics.

Run:  python tests/test_bots.py   (from plugins/bot-bridge/client/)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.dirname(HERE)
sys.path.insert(0, CLIENT_DIR)
sys.path.insert(0, HERE)

from mock_bridge import MockBridge  # noqa: E402

_failures = 0


def check(cond: bool, msg: str) -> None:
    global _failures
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures += 1


def test_panic_bot_flips_for_low_ball():
    print("test_panic_bot_flips_for_low_ball")
    with MockBridge(rate_hz=200.0) as mock:
        # A ball low and on the left side (mock's main flippers sit at y=1803,
        # split_x=450), so the panic bot should raise the LEFT flipper.
        mock.set_balls([MockBridge.make_ball(0, x=120.0, y=1700.0, vy=3.0)])
        proc = subprocess.Popen(
            [sys.executable, os.path.join(CLIENT_DIR, "panic_bot.py"),
             "--port", str(mock.port), "--hz", "200"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(2.0)  # let it connect, receive geometry + state, and react
            cmds = mock.commands()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()

        flip_left_press = [c for c in cmds if c.get("cmd") == "flip_left" and c.get("press") == 1]
        flip_right_press = [c for c in cmds if c.get("cmd") == "flip_right" and c.get("press") == 1]
        check(len(flip_left_press) >= 1, f"panic bot raised LEFT flipper for a low-left ball ({len(flip_left_press)}x)")
        check(len(flip_right_press) == 0, "panic bot did NOT raise the right flipper (ball is on the left)")


def main():
    test_panic_bot_flips_for_low_ball()
    print("=" * 60)
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
