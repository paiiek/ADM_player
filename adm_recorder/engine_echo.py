"""M5.2 — spatial_engine echo-plane subscription client (recorder side).

The engine (M5.1) re-emits every inbound ADM-OSC object/transport message to
registered "echo subscribers" so the recorder can capture the *whole system*'s
trajectory — player, VST3, WebGUI, scene loads — not just what one player sends.

Wire contract (recorder → engine, verified against
``spatial_engine/core/src/ipc``)::

    /sys/handshake  ,sii  "echo_subscriber=adm_object_stream", schema_version, echo_port
    /hb/ping        ,f    unix_seconds        (every HEARTBEAT_SEC; refreshes TTL)

Note the ``,sii`` ordering (string first) — see :meth:`subscribe` for why the
documented ``,iis`` shape would be mis-decoded by the engine (D-4 quirk).

* The handshake goes to the engine's single inbound OSC socket (default 9100 —
  the same port the player streams ``/adm/obj/N/aed`` to). The engine captures
  our source IP via ``recvfrom`` and sends both its ``/sys/handshake_ok`` reply
  and the echo stream to ``(our_ip, echo_port)``. So the caller must already be
  listening on ``echo_port`` before calling :meth:`subscribe`.
* ``echo_port`` is advertised as the ``reply_port`` int — the engine only
  registers the subscriber when ``reply_port > 0`` *and* the tag matches
  ``EchoPlane::kEchoSubscriberTag``.
* TTL: the engine evicts subscribers after 30 s without a ``/hb/ping``
  (``kEchoSubscriberTtlMs``). We ping every 10 s (3× margin). We send ``,f``,
  not ``,d``: the engine refreshes echo TTL for *any* ``/hb/ping`` from the peer
  (the M5.1 refresh sits outside the ``from_external`` check), while ``,d`` is
  reserved for the external *player* liveness latch (ADR 0018 D-5) — the
  recorder is not the player and must not reset that latch.

This module owns only the *control* side (handshake + heartbeat). The inbound
echo ingest reuses the existing :func:`adm_recorder.osc_ingest.start_osc_server`
with ``OscIngestRouter(preset="adm")``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from pythonosc import udp_client

_log = logging.getLogger(__name__)

# Must byte-match spe::ipc::EchoPlane::kEchoSubscriberTag.
ECHO_SUBSCRIBER_TAG = "echo_subscriber=adm_object_stream"
# Must match spe::ipc::SCHEMA_VERSION (Command.h).
SCHEMA_VERSION = 1
# Engine eviction is 30 s (kEchoSubscriberTtlMs); ping at 10 s for 3× headroom.
DEFAULT_HEARTBEAT_SEC = 10.0


class EngineEchoSubscriber:
    """Subscribes the recorder to the engine echo plane and keeps it alive.

    Use as a context manager (or call :meth:`close`) so the heartbeat thread is
    always joined — otherwise pytest workers leak daemon threads between cases.
    """

    def __init__(
        self,
        engine_host: str,
        engine_port: int,
        echo_port: int,
        *,
        schema_version: int = SCHEMA_VERSION,
        heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
        on_send: Callable[[str, Any], None] | None = None,
    ) -> None:
        if not (0 < echo_port <= 65535):
            raise ValueError(f"echo_port must be in (0, 65535]; got {echo_port}")
        if heartbeat_sec <= 0.0:
            raise ValueError(f"heartbeat_sec must be positive; got {heartbeat_sec!r}")
        self._engine_host = engine_host
        self._engine_port = int(engine_port)
        self._echo_port = int(echo_port)
        self._schema_version = int(schema_version)
        self._heartbeat_sec = float(heartbeat_sec)
        self._on_send = on_send
        self._client = udp_client.SimpleUDPClient(engine_host, int(engine_port))
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self) -> EngineEchoSubscriber:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        """Send the handshake and start the heartbeat thread."""
        self.subscribe()
        self.start_heartbeat()

    def close(self) -> None:
        self.stop_heartbeat()

    # ── primitives ───────────────────────────────────────────────────────────

    def _send(self, address: str, value: Any) -> None:
        if self._on_send is not None:
            self._on_send(address, value)
        self._client.send_message(address, value)

    @staticmethod
    def _now_seconds() -> float:
        return time.time()

    # ── handshake ────────────────────────────────────────────────────────────

    def subscribe(self) -> None:
        """One-shot ``/sys/handshake`` advertising ``echo_port`` as reply_port.

        Sent as ``,sii [tag, schema, echo_port]`` — string first. This dodges the
        engine's D-4 quirk: ``CommandDecoder::buildCommand`` treats *any* message
        whose type tags start ``ii`` as carrying a leading ``seq, id`` pair and
        strips the first two ints, so the documented ``,iis`` ordering would make
        the engine read ``schema`` and ``reply_port`` as 0 — leaving the recorder
        unregistered (``reply_port > 0`` is required at SpatialEngine.cpp echo
        registration) and the handshake version-mismatched. The engine reads
        these fields by *type index* (``ints[0]``=schema, ``ints[1]``=reply_port,
        ``strings[0]``=tag), not absolute position, so leading with the string is
        wire-correct today and stays correct if the engine later carves
        ``/sys/handshake`` out of the seq/id heuristic.
        """
        self._send(
            "/sys/handshake",
            [ECHO_SUBSCRIBER_TAG, self._schema_version, self._echo_port],
        )

    # ── heartbeat ────────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stop_evt.is_set():
            # ,f (python float) — refreshes echo TTL without touching the
            # engine's external-player staleness latch (which keys off ,d).
            self._send("/hb/ping", float(self._now_seconds()))
            if self._stop_evt.wait(self._heartbeat_sec):
                return

    def start_heartbeat(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        t = threading.Thread(
            target=self._heartbeat_loop,
            name=f"adm-recorder-echo-hb-{self._echo_port}",
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
            _log.warning("echo heartbeat thread did not exit within %.1fs", timeout)
        self._thread = None
