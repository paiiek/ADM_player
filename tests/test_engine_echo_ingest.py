"""M5.2 — recorder ⇄ engine echo-plane round-trip.

A fake engine listens on an inbound port, and on ``/sys/handshake`` echoes a
burst of ``/adm/obj/N/{aed,gain,mute,name}`` back to the ``reply_port`` the
recorder advertised — exactly the M5.1 echo contract. We assert the recorder's
ingest path turns that into timeline position + meta events, and that the
heartbeat keeps reaching the engine.
"""
from __future__ import annotations

import time
import unittest
from typing import Any, Callable
from unittest import mock

from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher

from adm_recorder.engine_echo import (
    ECHO_SUBSCRIBER_TAG,
    SCHEMA_VERSION,
    EngineEchoSubscriber,
    start_engine_echo_ingest,
)
from adm_recorder.osc_ingest import OscIngestRouter, aed_deg_to_xyz, start_osc_server
from adm_recorder.osc_udp_server import start_threading_osc_udp
from adm_recorder.timeline_store import TimelineStore


def _wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class TestEngineEchoIngest(unittest.TestCase):
    def test_handshake_payload_matches_engine_contract(self) -> None:
        """subscribe() must emit ,sii [tag, schema, echo_port] (D-4: string first)."""
        sent: list[tuple[str, Any]] = []
        sub = EngineEchoSubscriber(
            "127.0.0.1", 9100, 9102, on_send=lambda a, v: sent.append((a, v))
        )
        self.addCleanup(sub.close)
        sub.subscribe()
        self.assertEqual(sent, [("/sys/handshake", [ECHO_SUBSCRIBER_TAG, SCHEMA_VERSION, 9102])])

    def test_echo_round_trip_records_position_and_meta(self) -> None:
        handshakes: list[int] = []  # reply_port seen per handshake
        ping_count = {"n": 0}

        # ── recorder ingest side (bound first so echoes have a destination) ──
        timeline = TimelineStore()
        frame = {"f": 100}
        router = OscIngestRouter(
            "adm",
            get_frame=lambda: frame["f"],
            on_xyz=timeline.add_cartesian,
            on_meta=timeline.add_meta,
        )
        ingest_srv, _ingest_th = start_osc_server("127.0.0.1", 0, router)
        self.addCleanup(ingest_srv.shutdown)
        listen_port = ingest_srv.server_address[1]

        # ── fake engine: echo a burst back to reply_port on handshake ──
        def on_handshake(_addr: str, *args: Any) -> None:
            # Read by *type index*, exactly as the engine CommandDecoder does
            # (ints[0]=schema, ints[1]=reply_port, strings[0]=tag) — this is what
            # makes the ,sii ordering wire-correct regardless of arg position.
            ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
            strs = [a for a in args if isinstance(a, str)]
            self.assertEqual(ints[0], SCHEMA_VERSION)
            self.assertEqual(strs[0], ECHO_SUBSCRIBER_TAG)
            reply_port = int(ints[1])
            handshakes.append(reply_port)
            echo = udp_client.SimpleUDPClient("127.0.0.1", reply_port)
            echo.send_message("/adm/obj/3/aed", [30.0, 10.0, 0.5])
            echo.send_message("/adm/obj/3/gain", 0.5)
            echo.send_message("/adm/obj/3/mute", 1)
            echo.send_message("/adm/obj/3/name", "dog")

        def on_ping(_addr: str, *_args: Any) -> None:
            ping_count["n"] += 1

        d = Dispatcher()
        d.map("/sys/handshake", on_handshake)
        d.map("/hb/ping", on_ping)
        engine_srv, _engine_th = start_threading_osc_udp(("127.0.0.1", 0), d)
        self.addCleanup(engine_srv.shutdown)
        engine_port = engine_srv.server_address[1]

        # ── subscribe (advertises listen_port as reply_port) + heartbeat ──
        sub = EngineEchoSubscriber(
            "127.0.0.1", engine_port, listen_port, heartbeat_sec=0.05
        )
        self.addCleanup(sub.close)
        sub.start()

        self.assertTrue(
            _wait_until(lambda: timeline.snapshot() and len(timeline.snapshot_meta()) >= 3),
            f"echo not ingested: pos={timeline.snapshot()} meta={timeline.snapshot_meta()}",
        )

        # position: aed → cartesian, frame stamped from get_frame()
        positions = timeline.snapshot()
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p.channel_1based, 3)
        self.assertEqual(p.frame, 100)
        ex, ey, ez = aed_deg_to_xyz(30.0, 10.0, 0.5)
        self.assertAlmostEqual(p.x, ex, places=5)
        self.assertAlmostEqual(p.y, ey, places=5)
        self.assertAlmostEqual(p.z, ez, places=5)

        # meta: gain/mute/name forwarded raw, channel + frame preserved
        meta = {m.kind: m for m in timeline.snapshot_meta()}
        self.assertEqual(set(meta), {"gain", "mute", "name"})
        self.assertAlmostEqual(meta["gain"].value, 0.5, places=5)
        self.assertEqual(meta["mute"].value, 1)
        self.assertEqual(meta["name"].value, "dog")
        self.assertEqual(meta["gain"].channel_1based, 3)
        self.assertEqual(meta["gain"].frame, 100)

        # heartbeat reaches the engine (keeps the subscriber TTL alive)
        self.assertTrue(_wait_until(lambda: ping_count["n"] >= 1))
        self.assertGreaterEqual(len(handshakes), 1)
        self.assertEqual(handshakes[0], listen_port)


class TestEngineEchoSession(unittest.TestCase):
    """``start_engine_echo_ingest`` — the one-call echo pipeline the GUI/CLI use."""

    def _start_fake_engine(self, on_handshake: Callable[..., None]) -> tuple[int, dict]:
        """Bind a fake engine: route ``/sys/handshake`` and count ``/hb/ping``."""
        ping_count = {"n": 0}
        d = Dispatcher()
        d.map("/sys/handshake", on_handshake)
        d.map("/hb/ping", lambda *_a: ping_count.__setitem__("n", ping_count["n"] + 1))
        srv, _th = start_threading_osc_udp(("127.0.0.1", 0), d)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1], ping_count

    def test_ephemeral_listen_port_resolved_and_advertised(self) -> None:
        timeline = TimelineStore()
        handshakes: list[int] = []

        def on_handshake(_addr: str, *args: Any) -> None:
            # read reply_port by type index, like the engine CommandDecoder
            ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
            reply_port = int(ints[1])
            handshakes.append(reply_port)
            echo = udp_client.SimpleUDPClient("127.0.0.1", reply_port)
            echo.send_message("/adm/obj/7/aed", [-45.0, 0.0, 1.0])
            echo.send_message("/adm/obj/7/gain", 0.25)
            echo.send_message("/adm/obj/7/width", 30.0)

        engine_port, ping_count = self._start_fake_engine(on_handshake)

        session = start_engine_echo_ingest(
            bind_host="127.0.0.1",
            listen_port=0,  # ephemeral — the OS picks the port
            engine_host="127.0.0.1",
            engine_port=engine_port,
            get_frame=lambda: 42,
            on_xyz=timeline.add_cartesian,
            on_meta=timeline.add_meta,
            heartbeat_sec=0.05,
        )
        self.addCleanup(session.close)

        # the ephemeral port was resolved (non-zero) before subscribing
        self.assertGreater(session.listen_port, 0)

        self.assertTrue(
            _wait_until(
                lambda: timeline.snapshot() and len(timeline.snapshot_meta()) >= 2
            ),
            f"echo not ingested: pos={timeline.snapshot()} meta={timeline.snapshot_meta()}",
        )
        # …and that exact resolved port is what the engine was told to reply to
        self.assertEqual(handshakes[0], session.listen_port)

        pos = timeline.snapshot()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].channel_1based, 7)
        self.assertEqual(pos[0].frame, 42)
        ex, ey, ez = aed_deg_to_xyz(-45.0, 0.0, 1.0)
        self.assertAlmostEqual(pos[0].x, ex, places=5)
        self.assertAlmostEqual(pos[0].y, ey, places=5)
        self.assertAlmostEqual(pos[0].z, ez, places=5)

        meta = {m.kind: m.value for m in timeline.snapshot_meta()}
        self.assertAlmostEqual(meta["gain"], 0.25, places=5)
        self.assertAlmostEqual(meta["width"], 30.0, places=5)

        self.assertTrue(_wait_until(lambda: ping_count["n"] >= 1))

        # close() tears down the server and is idempotent
        session.close()
        self.assertIsNone(session.server)
        session.close()

    def test_subscribe_failure_tears_down_ingest_socket(self) -> None:
        """If the handshake send fails, the just-bound ingest socket is closed."""
        closed = {"shutdown": 0, "server_close": 0}

        class _FakeServer:
            server_address = ("127.0.0.1", 5555)

            def shutdown(self) -> None:
                closed["shutdown"] += 1

            def server_close(self) -> None:
                closed["server_close"] += 1

        def boom(_addr: str, _val: Any) -> None:
            raise RuntimeError("handshake send blew up")

        with mock.patch(
            "adm_recorder.engine_echo.start_osc_server",
            return_value=(_FakeServer(), None),
        ):
            with self.assertRaises(RuntimeError):
                start_engine_echo_ingest(
                    bind_host="127.0.0.1",
                    listen_port=5555,
                    engine_host="127.0.0.1",
                    engine_port=9100,
                    get_frame=lambda: 0,
                    on_xyz=lambda *_a: None,
                    on_send=boom,
                )

        # the half-open ingest socket must be reclaimed, not leaked
        self.assertEqual(closed["shutdown"], 1)
        self.assertEqual(closed["server_close"], 1)


if __name__ == "__main__":
    unittest.main()
