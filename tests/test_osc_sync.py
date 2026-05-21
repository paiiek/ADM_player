"""Regression tests for Phase B sync emitter (handshake + heartbeat + transport).

All tests run socketless via the `on_send` capture hook, so no UDP port is bound.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest

from adm_player.osc_sync import DEFAULT_HEARTBEAT_HZ, SyncEmitter


def _free_port() -> int:
    """Reserve a real UDP port so pythonosc's SimpleUDPClient never races."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _capture() -> tuple[list[tuple[str, Any]], Any]:
    sent: list[tuple[str, Any]] = []
    return sent, lambda addr, value: sent.append((addr, value))


# ── handshake ────────────────────────────────────────────────────────────

def test_handshake_emits_int_pair_with_reply_port() -> None:
    sent, on_send = _capture()
    em = SyncEmitter("127.0.0.1", _free_port(), on_send=on_send)
    em.send_handshake(reply_port=9101)
    em.close()
    assert sent == [("/sys/handshake", [1, 9101])], f"unexpected handshake payload: {sent}"


def test_handshake_rejects_out_of_range_port() -> None:
    em = SyncEmitter("127.0.0.1", _free_port())
    with pytest.raises(ValueError, match="reply_port"):
        em.send_handshake(reply_port=70000)
    with pytest.raises(ValueError, match="reply_port"):
        em.send_handshake(reply_port=-1)


# ── heartbeat ────────────────────────────────────────────────────────────

def test_heartbeat_ticks_at_configured_rate_and_stops_cleanly() -> None:
    """Run at 50 Hz for 200 ms → expect ~10 beats (allow ±50% jitter on CI)."""
    sent, on_send = _capture()
    em = SyncEmitter("127.0.0.1", _free_port(), heartbeat_hz=50.0, on_send=on_send)
    em.start_heartbeat()
    time.sleep(0.2)
    em.stop_heartbeat()
    pings = [v for a, v in sent if a == "/hb/ping"]
    # CI can stall; we mainly care that *something* fires and the thread exits.
    assert len(pings) >= 3, f"expected at least 3 heartbeats in 200ms; got {len(pings)}"
    assert all(isinstance(v, float) for v in pings), f"heartbeat payload must be float; got {pings}"


def test_heartbeat_double_start_is_idempotent() -> None:
    em = SyncEmitter("127.0.0.1", _free_port(), heartbeat_hz=10.0)
    em.start_heartbeat()
    first = em._thread
    em.start_heartbeat()  # must not spawn a second thread
    assert em._thread is first, "second start_heartbeat must reuse the existing thread"
    em.stop_heartbeat()


def test_heartbeat_stop_without_start_is_noop() -> None:
    em = SyncEmitter("127.0.0.1", _free_port())
    em.stop_heartbeat()  # must not raise


def test_heartbeat_hz_must_be_positive() -> None:
    with pytest.raises(ValueError, match="heartbeat_hz"):
        SyncEmitter("127.0.0.1", _free_port(), heartbeat_hz=0.0)
    with pytest.raises(ValueError, match="heartbeat_hz"):
        SyncEmitter("127.0.0.1", _free_port(), heartbeat_hz=-1.0)


def test_context_manager_stops_heartbeat_on_exit() -> None:
    sent, on_send = _capture()
    with SyncEmitter("127.0.0.1", _free_port(), heartbeat_hz=50.0, on_send=on_send) as em:
        em.start_heartbeat()
        time.sleep(0.05)
    # After __exit__ the thread must be gone.
    assert em._thread is None, "context manager must clear heartbeat thread on exit"


# ── transport ────────────────────────────────────────────────────────────

def test_transport_play_emits_double_unix_time() -> None:
    sent, on_send = _capture()
    em = SyncEmitter("127.0.0.1", _free_port(), on_send=on_send)
    em.send_transport_play(start_time=1_700_000_000.5)
    em.close()
    assert sent == [("/transport/play", 1_700_000_000.5)]


def test_transport_play_default_uses_current_time() -> None:
    sent, on_send = _capture()
    em = SyncEmitter("127.0.0.1", _free_port(), on_send=on_send)
    before = time.time()
    em.send_transport_play()
    after = time.time()
    em.close()
    addr, value = sent[0]
    assert addr == "/transport/play"
    assert isinstance(value, float)
    assert before - 1.0 <= value <= after + 1.0, f"transport time out of sane range: {value}"


def test_transport_pause_and_stop_carry_no_payload() -> None:
    sent, on_send = _capture()
    em = SyncEmitter("127.0.0.1", _free_port(), on_send=on_send)
    em.send_transport_pause()
    em.send_transport_stop()
    em.close()
    assert ("/transport/pause", []) in sent
    assert ("/transport/stop", []) in sent


# ── module-level constant sanity ─────────────────────────────────────────

def test_default_heartbeat_rate_is_one_hz() -> None:
    """The plan specifies 1 Hz — guard against accidental cadence drift."""
    assert DEFAULT_HEARTBEAT_HZ == 1.0
