"""A mock VPX bot bridge server.

Speaks the *exact* NDJSON wire format of the real bridge plugin (schema v2,
verified field-for-field against plugins/bot-bridge/botbridge.cpp) so the client
library, template, panic bot, and console can be exercised offline — no running
table required. Also used to measure how the client ingests a 1 kHz multiball +
light-show load (see latency_load_test.py --mock).

It is a *test double*, not a simulator: there is no physics here. It emits frames
you configure and records the commands it receives.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional


def _ball(i: int, x=500.0, y=1000.0, z=25.0, vx=0.0, vy=0.0, vz=0.0):
    return {"id": i, "x": x, "y": y, "z": z, "vx": vx, "vy": vy, "vz": vz,
            "avx": 0.0, "avy": 0.0, "avz": 0.0, "r": 25.0}


class MockBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 rate_hz: float = 200.0, n_lamps: int = 8):
        self.host = host
        self.port = port
        self.rate_hz = rate_hz
        self.n_lamps = n_lamps

        self._lock = threading.Lock()
        self._balls = [_ball(0)]
        self._event_queue: list[dict] = []   # attached to the next state frame
        self._commands: list[dict] = []
        self._tick = 0
        self._streaming = True
        self._flood = False                   # ignore rate; send as fast as possible

        self._listen: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> int:
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind((self.host, self.port))
        self._listen.listen(1)
        self.port = self._listen.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._listen is not None:
            try:
                self._listen.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "MockBridge":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- control knobs ------------------------------------------------------ #
    def set_balls(self, balls: list[dict]) -> None:
        with self._lock:
            self._balls = list(balls)

    def push_event(self, type: int, kind: int, name: str, scalar: float = 0.0) -> None:
        with self._lock:
            self._event_queue.append(
                {"t": float(self._tick) * 0.01, "type": type, "kind": kind,
                 "scalar": scalar, "name": name})

    def commands(self) -> list[dict]:
        with self._lock:
            return list(self._commands)

    def set_streaming(self, on: bool) -> None:
        with self._lock:
            self._streaming = on

    def set_flood(self, on: bool) -> None:
        with self._lock:
            self._flood = on

    @staticmethod
    def make_ball(i, **kw):
        return _ball(i, **kw)

    # -- server ------------------------------------------------------------- #
    def _serve(self) -> None:
        while self._running:
            try:
                client, _ = self._listen.accept()
            except OSError:
                break
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            recv_thread = threading.Thread(target=self._recv_loop, args=(client,), daemon=True)
            recv_thread.start()
            self._stream(client)
            try:
                client.close()
            except OSError:
                pass
            recv_thread.join(timeout=1.0)

    def _recv_loop(self, client: socket.socket) -> None:
        buf = b""
        while self._running:
            try:
                chunk = client.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self._lock:
                    self._commands.append(obj)

    def _stream(self, client: socket.socket) -> None:
        # Static frame first (mirrors the real bridge sending it on connect).
        try:
            client.sendall(self._static_frame().encode("utf-8"))
        except OSError:
            return
        next_t = time.perf_counter()
        while self._running:
            with self._lock:
                streaming = self._streaming
                flood = self._flood
            if streaming:
                try:
                    client.sendall(self._state_frame().encode("utf-8"))
                except OSError:
                    return
            if flood:
                continue
            next_t += 1.0 / self.rate_hz
            sleep_for = next_t - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.perf_counter()

    # -- frame builders (exact v2 schema) ----------------------------------- #
    def _static_frame(self) -> str:
        geometry = [
            {"type": 1, "name": "LeftFlipper", "x": 278.0, "y": 1803.0, "z": 60.0,
             "a": 130.0, "b": 21.0, "c": 13.0, "d": 121.0, "ex": 70.0, "ey": 0.0, "ez": 0.0},
            {"type": 1, "name": "RightFlipper", "x": 622.0, "y": 1803.0, "z": 60.0,
             "a": 130.0, "b": 21.0, "c": 13.0, "d": 59.0, "ex": 110.0, "ey": 0.0, "ez": 0.0},
            {"type": 12, "name": "RightRamp", "x": 700.0, "y": 400.0, "z": 30.0,
             "a": 60.0, "b": 40.0, "c": 0.0, "d": 0.0, "ex": 760.0, "ey": 1200.0, "ez": 120.0},
            {"type": 8, "name": "Drain", "x": 450.0, "y": 1950.0, "z": 0.0,
             "a": 25.0, "b": 0.0, "c": 0.0, "d": 0.0, "ex": 0.0, "ey": 0.0, "ez": 0.0},
            {"type": 5, "name": "Bumper1", "x": 450.0, "y": 600.0, "z": 0.0,
             "a": 45.0, "b": 0.0, "c": 0.0, "d": 0.0, "ex": 0.0, "ey": 0.0, "ez": 0.0},
        ]
        lamps = [{"i": i, "name": f"lamp{i}", "mode": 1} for i in range(self.n_lamps)]
        return json.dumps({"type": "static", "v": 2, "geometry": geometry, "lamps": lamps}) + "\n"

    def _state_frame(self) -> str:
        with self._lock:
            self._tick += 1
            tick = self._tick
            balls = list(self._balls)
            events = self._event_queue
            self._event_queue = []
        frame = {
            "type": "state", "v": 2, "tick": tick, "time": tick * 0.01,
            "balls": balls,
            "flippers": [
                {"i": 0, "angle": 121.0, "angleSpeed": 0.0, "solenoid": 0, "eos": 1},
                {"i": 1, "angle": 59.0, "angleSpeed": 0.0, "solenoid": 0, "eos": 1},
            ],
            "plungers": [{"i": 0, "pos": 0.167, "posVPU": 1943.3, "speed": 0.0, "rest": 0.167}],
            "lamps": [{"i": i, "mode": 1, "lit": (tick + i) % 2, "in": 0.85}
                      for i in range(self.n_lamps)],
            "nudge": {"ax": 0.0, "ay": 0.0, "vx": 0.0, "vy": 0.0, "dx": 0.0, "dy": 0.0,
                      "tilt": 0, "slam": 0, "plumbSim": 1, "plumbCount": 0},
            "events": events,
        }
        return json.dumps(frame) + "\n"
