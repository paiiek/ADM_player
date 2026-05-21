"""Phase B sync layer: handshake + 1 Hz heartbeat + transport timetag.

Wire format (player → spatial_engine, ADM-OSC v1.0 + project extension):

    /sys/handshake     ,ii    1, reply_port
    /hb/ping           ,d     unix_time_seconds      (every 1 s while session alive)
    /transport/play    ,d     unix_time_seconds      (absolute start time)
    /transport/pause   (no args)
    /transport/stop    (no args)

Design notes
------------
* Sync is opt-in: callers must explicitly construct `SyncEmitter`. The default CLI
  flow leaves it off so non-spatial-engine targets are unaffected.
* Heartbeat runs in a daemon thread with a `threading.Event` so the main loop can
  stop it deterministically (no jitter on shutdown).
* `on_send(address, value_tuple)` is captured for tests; it never touches the
  socket, so unit tests run without a network port.
* We pass absolute time as an OSC `d` (double, seconds since Unix epoch) instead
  of a native `t` timetag. Rationale: pythonosc's `t` builder packs into the NTP
  era (1900-01-01) which has caused interop bugs in other ADM-OSC bridges;
  doubles round-trip cleanly through every receiver we've tested.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from pythonosc import udp_client

_log = logging.getLogger(__name__)

# Heartbeat cadence (Hz). 1 Hz matches the project plan and keeps OS UDP queue
# pressure trivial while still letting the engine detect player crashes within
# ~3 s (3 missed beats).
DEFAULT_HEARTBEAT_HZ: float = 1.0


class SyncEmitter:
    """Emits the Phase B sync messages and manages the heartbeat thread.

    Use as a context manager (or call `close()` on shutdown) to guarantee the
    heartbeat thread exits — important under tests so pytest workers don't leak
    background threads between cases.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        heartbeat_hz: float = DEFAULT_HEARTBEAT_HZ,
        on_send: Callable[[str, Any], None] | None = None,
    ) -> None:
        if heartbeat_hz <= 0.0:
            raise ValueError(f"heartbeat_hz must be positive; got {heartbeat_hz!r}")
        self._host = host
        self._port = port
        self._client = udp_client.SimpleUDPClient(host, port)
        self._on_send = on_send
        self._heartbeat_period = 1.0 / heartbeat_hz
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> SyncEmitter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.stop_heartbeat()

    # ── primitives ─────────────────────────────────────────────────────────

    def _send(self, address: str, value: Any) -> None:
        if self._on_send is not None:
            self._on_send(address, value)
        self._client.send_message(address, value)

    @staticmethod
    def _now_seconds() -> float:
        return time.time()

    # ── handshake ──────────────────────────────────────────────────────────

    def send_handshake(self, reply_port: int) -> None:
        """One-shot handshake. `reply_port` is the UDP port we'll listen on for
        the engine's optional acknowledgement (engine ack is out of scope here)."""
        if reply_port < 0 or reply_port > 65535:
            raise ValueError(f"reply_port must be in [0, 65535]; got {reply_port}")
        self._send("/sys/handshake", [1, int(reply_port)])

    # ── heartbeat ──────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Daemon thread that ticks every `_heartbeat_period` seconds.
        Uses Event.wait() instead of time.sleep so close() returns promptly."""
        while not self._stop_evt.is_set():
            self._send("/hb/ping", float(self._now_seconds()))
            # If wait() returns True the event was set → exit immediately.
            if self._stop_evt.wait(self._heartbeat_period):
                return

    def start_heartbeat(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        t = threading.Thread(
            target=self._heartbeat_loop,
            name=f"adm-player-heartbeat-{self._port}",
            daemon=True,
        )
        self._thread = t
        t.start()

    def stop_heartbeat(self, timeout: float = 2.0) -> None:
        t = self._thread
        if t is None:
            return
        self._stop_evt.set()
        t.join(timeout=timeout)
        if t.is_alive():
            _log.warning("heartbeat thread did not exit within %.1fs", timeout)
        self._thread = None

    # ── transport ──────────────────────────────────────────────────────────

    def send_transport_play(self, start_time: float | None = None) -> None:
        """Mirror player play → engine. `start_time` defaults to time.time()."""
        t = self._now_seconds() if start_time is None else float(start_time)
        self._send("/transport/play", t)

    def send_transport_pause(self) -> None:
        # Pause carries no timetag (engine resumes from its own playhead).
        self._send("/transport/pause", [])

    def send_transport_stop(self) -> None:
        self._send("/transport/stop", [])
