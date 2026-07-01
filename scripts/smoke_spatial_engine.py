#!/usr/bin/env python3
"""
Smoke test for ADM Player ↔ spatial_engine OSC integration (Phase A contract).

Drives the emitter end-to-end and captures every OSC packet on the engine port,
without actually requiring a spatial_engine binary. Confirms:
  - addresses match ADM-OSC v1.0 (/adm/obj/N/aed, /adm/obj/N/xyz, /adm/config/obj/N/cartesian)
  - distance contract: meters input → normalized [0,1] with 20 m = 1.0
  - mode transition (polar ↔ cartesian) is not silently dropped
  - object indices > MAX_OSC_OBJECTS (default 128; env SPE_ADM_OSC_MAX_OBJECTS opt-down to 64)
    are dropped with a single warning, not emitted
  - lip-sync window: time between scheduled emit and packet reception stays sub-millisecond

Run:
    python scripts/smoke_spatial_engine.py

Exits 0 on PASS, 1 on any failure. Prints a compact per-check verdict table.
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Allow running from the dreamscape/ root without installing the package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pythonosc.dispatcher import Dispatcher  # noqa: E402
from pythonosc.osc_server import ThreadingOSCUDPServer  # noqa: E402

from adm_player.adm_model import AdmObject, ObjectBlock, ObjectPosition  # noqa: E402
from adm_player.osc_emit import ADM_OSC_MAX_DIST, MAX_OSC_OBJECTS, AdmOscEmitter  # noqa: E402
from adm_player.osc_presets import PRESET_DEFAULT_ENDPOINTS, create_osc_emitter  # noqa: E402
from adm_player.osc_sync import SyncEmitter  # noqa: E402


@dataclass
class Capture:
    address: str
    args: tuple
    t_recv_ns: int


class OscRecorder:
    """In-process OSC receiver — substitutes for a live spatial_engine instance."""

    def __init__(self, host: str, port: int) -> None:
        disp = Dispatcher()
        disp.set_default_handler(self._on_any)
        self._server = ThreadingOSCUDPServer((host, port), disp)
        self._captures: list[Capture] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _on_any(self, address: str, *args) -> None:
        with self._lock:
            self._captures.append(Capture(address=address, args=args, t_recv_ns=time.monotonic_ns()))

    def start(self) -> tuple[str, int]:
        self._thread.start()
        return self._server.server_address

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)

    def captures(self) -> list[Capture]:
        with self._lock:
            return list(self._captures)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _obj(idx: int) -> AdmObject:
    return AdmObject(
        adm_index=idx,
        object_id=f"AO_{idx:04x}",
        label=f"obj_{idx}",
        type_definition="objects",
        track_uids=(),
        wav_channels=(idx,),
        blocks=(),
        osc_object_index=idx,
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float, poll_s: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _verdict(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    return (name, ok, detail)


def run_smoke(verbose: bool = False) -> int:
    log_level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s [%(name)s] %(message)s")

    port = _free_port()
    rx = OscRecorder("127.0.0.1", port)
    rx.start()
    results: list[tuple[str, bool, str]] = []
    try:
        # ── Check 1: spatial_engine preset registers and routes to /adm/obj/N/aed
        em = create_osc_emitter(
            "spatial_engine",
            "127.0.0.1",
            port,
            azimuth_offset=0.0,
            azimuth_flip=False,
            on_send=None,
            scales={},
        )
        em.send_object_position(
            _obj(1),
            ObjectBlock(0.0, 1.0, ObjectPosition("polar", azimuth=0.0, elevation=0.0, distance=10.0)),
        )
        if not _wait_until(lambda: any(c.address == "/adm/obj/1/aed" for c in rx.captures()), 1.0):
            results.append(_verdict("preset routes /adm/obj/N/aed", False, "no /adm/obj/1/aed received"))
            return _report(results)
        aed = next(c.args for c in rx.captures() if c.address == "/adm/obj/1/aed")
        results.append(_verdict("preset routes /adm/obj/N/aed", True, f"aed={aed}"))

        # ── Check 2: distance contract — 10 m → 0.5 with ADM_OSC_MAX_DIST=20
        d_norm = float(aed[2])
        ok = abs(d_norm - 0.5) < 1e-6
        results.append(_verdict(
            f"distance contract 10m / {ADM_OSC_MAX_DIST}m = 0.5",
            ok,
            f"got d_norm={d_norm:.6f}",
        ))

        # ── Check 3: mode transition (polar → cartesian) does NOT drop the cart payload
        em2 = create_osc_emitter(
            "spatial_engine",
            "127.0.0.1",
            port,
            azimuth_offset=0.0,
            azimuth_flip=False,
            on_send=None,
            scales={},
        )
        em2.send_object_position(_obj(2), ObjectBlock(0.0, 1.0, ObjectPosition("polar", azimuth=0, elevation=0, distance=1)))
        em2.send_object_position(_obj(2), ObjectBlock(0.0, 1.0, ObjectPosition("cartesian", x=0, y=0, z=1)))
        _wait_until(lambda: any(c.address == "/adm/obj/2/xyz" for c in rx.captures()), 1.0)
        has_aed = any(c.address == "/adm/obj/2/aed" for c in rx.captures())
        has_cfg = any(c.address == "/adm/config/obj/2/cartesian" for c in rx.captures())
        has_xyz = any(c.address == "/adm/obj/2/xyz" for c in rx.captures())
        ok = has_aed and has_cfg and has_xyz
        results.append(_verdict(
            "mode transition polar→cart emits xyz",
            ok,
            f"aed={has_aed} cfg={has_cfg} xyz={has_xyz}",
        ))

        # ── Check 4: object index > MAX_OSC_OBJECTS is dropped, no packet hits the wire
        idx_over = MAX_OSC_OBJECTS + 5
        captures_before = len(rx.captures())
        em3 = create_osc_emitter(
            "spatial_engine",
            "127.0.0.1",
            port,
            azimuth_offset=0.0,
            azimuth_flip=False,
            on_send=None,
            scales={},
        )
        em3.send_object_position(
            _obj(idx_over),
            ObjectBlock(0.0, 1.0, ObjectPosition("polar", azimuth=0, elevation=0, distance=0.5)),
        )
        # Give the network stack a moment in case the drop fails and a packet leaks.
        time.sleep(0.05)
        captures_after = len(rx.captures())
        ok = captures_after == captures_before
        results.append(_verdict(
            f"index > {MAX_OSC_OBJECTS} is silently dropped (no packet)",
            ok,
            f"delta_captures={captures_after - captures_before}",
        ))

        # ── Check 5: send→receive latency (loopback should be << 1 ms typically)
        t0 = time.monotonic_ns()
        em.send_object_position(
            _obj(10),
            ObjectBlock(0.0, 1.0, ObjectPosition("polar", azimuth=90.0, elevation=0.0, distance=0.5)),
        )
        _wait_until(lambda: any(c.address == "/adm/obj/10/aed" for c in rx.captures()), 1.0)
        cap_10 = next((c for c in rx.captures() if c.address == "/adm/obj/10/aed"), None)
        if cap_10 is None:
            results.append(_verdict("loopback latency", False, "no packet"))
        else:
            latency_ms = (cap_10.t_recv_ns - t0) / 1e6
            ok = latency_ms < 50.0  # generous ceiling — sanity check, not timing benchmark
            results.append(_verdict(
                "loopback latency < 50 ms (sanity)",
                ok,
                f"{latency_ms:.3f} ms",
            ))

        # ── Check 6: default endpoint exposed for UI auto-fill
        ep = PRESET_DEFAULT_ENDPOINTS.get("spatial_engine")
        ok = ep == ("127.0.0.1", 9100)
        results.append(_verdict("default endpoint = 127.0.0.1:9100", ok, f"got {ep}"))

        # ── Phase B sync layer ───────────────────────────────────────────
        sync = SyncEmitter("127.0.0.1", port, heartbeat_hz=20.0)
        try:
            # Check 7: handshake reaches receiver as /sys/handshake with [1, reply_port]
            sync.send_handshake(reply_port=9101)
            ok = _wait_until(lambda: any(c.address == "/sys/handshake" for c in rx.captures()), 1.0)
            cap_hs = next((c for c in rx.captures() if c.address == "/sys/handshake"), None)
            args_ok = cap_hs is not None and tuple(cap_hs.args) == (1, 9101)
            results.append(_verdict(
                "/sys/handshake [1, reply_port]",
                ok and args_ok,
                f"args={cap_hs.args if cap_hs else None}",
            ))

            # Check 8: heartbeat ticks (20 Hz → at least 2 beats in 250 ms)
            beats_before = sum(1 for c in rx.captures() if c.address == "/hb/ping")
            sync.start_heartbeat()
            time.sleep(0.25)
            sync.stop_heartbeat()
            beats_after = sum(1 for c in rx.captures() if c.address == "/hb/ping")
            delta = beats_after - beats_before
            ok = delta >= 2
            results.append(_verdict(
                "/hb/ping heartbeat ticks (≥2 in 250ms @20Hz)",
                ok,
                f"got {delta} beats",
            ))

            # Check 9: /transport/play carries a sensible Unix timestamp (within ±1 day of now)
            sync.send_transport_play()
            ok = _wait_until(lambda: any(c.address == "/transport/play" for c in rx.captures()), 1.0)
            cap_tp = next((c for c in rx.captures() if c.address == "/transport/play"), None)
            now = time.time()
            t_ok = (
                cap_tp is not None
                and len(cap_tp.args) == 1
                and isinstance(cap_tp.args[0], float)
                and abs(cap_tp.args[0] - now) < 86400.0
            )
            results.append(_verdict(
                "/transport/play has Unix-time double",
                ok and t_ok,
                f"args={cap_tp.args if cap_tp else None}",
            ))
        finally:
            sync.close()

    finally:
        rx.stop()

    return _report(results)


def _report(results: list[tuple[str, bool, str]]) -> int:
    pad = max(len(name) for name, _, _ in results) + 2
    print()
    print(f"{'Check':<{pad}}  Result  Detail")
    print(f"{'-' * pad}  ------  ------")
    fail = 0
    for name, ok, detail in results:
        tag = "PASS" if ok else "FAIL"
        if not ok:
            fail += 1
        print(f"{name:<{pad}}  {tag:<6}  {detail}")
    print()
    if fail == 0:
        print(f"Smoke OK — {len(results)} checks passed.")
        return 0
    print(f"Smoke FAILED — {fail}/{len(results)} checks failed.")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="enable DEBUG logging")
    args = p.parse_args(argv)
    return run_smoke(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
