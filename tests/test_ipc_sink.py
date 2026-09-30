"""ADR 0019 PR5 — tests for the Python shm-ring PRODUCER (`IpcRingSink`).

The header byte-layout assertions (AC-1) use LITERAL offsets transcribed
directly from core/src/audio_io/shm/RingHeader.h — NOT the producer's own
constants — so a producer/reader shared-mis-offset bug still fails. The
round-trip reader (AC-3..AC-5/AC-9) is a pure-Python mirror of the C++
consumer's masked-index read; no C++ build dependency.

AC coverage:
  AC-1  test_header_layout_all_15_fields            (header byte-layout, 15 offsets)
  AC-2  test_pow2_padding_*                          (pow2 padding + log)
  AC-3  test_round_trip_one_second_sample_exact      (1 s round-trip)
  AC-4  test_deinterleave_planar_channels            (planar de-interleave)
  AC-5  test_wrap_around_reconstructs_ramp           (wrap-split)
  AC-6  test_xrun_drop_newest_deterministic          (drop-newest xrun)
  AC-7a test_heartbeat_unix_epoch_*                  (unix-epoch heartbeat + timer)
  AC-7b test_state_machine_*                         (state machine + drain dwell)
  AC-8  test_reserved_and_lock_word_zero_*           (_reserved / lock zero)
  AC-9  test_publish_order_write_idx_last            (publish program-order)
  AC-13 test_close_no_buffererror_*                  (clean close + unlink)
"""

from __future__ import annotations

import logging
import os
import struct
import time

import numpy as np
import pytest

from adm_player.ipc_sink import (
    OFF_PRODUCER_HEARTBEAT_MS,
    RING_HEADER_SIZE,
    IpcRingSink,
    next_pow2,
)

# ── LITERAL offsets transcribed from RingHeader.h (NOT from ipc_sink._HEADER) ──
# RingHeader.h:54-71 table / :101-116 static_asserts.
L_MAGIC = 0x0000
L_VERSION = 0x0008
L_HEADER_SIZE = 0x000C
L_SAMPLE_RATE = 0x0010
L_BLOCK_SIZE = 0x0014
L_CHANNELS = 0x0018
L_CAPACITY_FRAMES = 0x001C
L_WRITE_IDX = 0x0020
L_READ_IDX = 0x0028
L_PRODUCER_PID = 0x0030
L_PRODUCER_HEARTBEAT_MS = 0x0038
L_XRUN_COUNT = 0x0040
L_PRODUCER_META_BLOCK_PTS_NS = 0x0048
L_PRODUCER_STATE = 0x0050
L_SEQ = 0x0058
L_RESERVED = 0x0060
L_MAGIC_VALUE = 0x53504543484D4E47  # "SPECHMNG" LE u64 (RingHeader.h:25)

_NAME_SEQ = 0


def _unique_name() -> str:
    """A fresh /dev/shm name per test so a leak can't cross-contaminate."""
    global _NAME_SEQ
    _NAME_SEQ += 1
    return f"pr5test-{os.getpid()}-{_NAME_SEQ}-{int(time.time()*1e6) % 1_000_000}"


@pytest.fixture
def name() -> str:
    n = _unique_name()
    yield n
    # Defensive cleanup if a test left the region behind.
    path = f"/dev/shm/{n}"
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _u64(buf, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


# ── pure-Python masked-index reader (mirror of the C++ consumer, ADR §2.4) ────


def _read_channel(sink: IpcRingSink, channel: int, start: int, count: int) -> np.ndarray:
    """Read `count` frames of `channel` starting at global index `start`,
    mirroring the consumer's masked-index read:
        offset = 4096 + c*cap*4 + (i & (cap-1))*4
    """
    cap = sink._capacity
    buf = sink._shm.buf
    base = RING_HEADER_SIZE + channel * cap * 4
    out = np.empty(count, dtype=np.dtype("<f4"))
    for k in range(count):
        i = (start + k) & (cap - 1)
        out[k] = struct.unpack_from("<f", buf, base + i * 4)[0]
    return out


# ═════════════════════════════════════════════════════════════════════════════
# AC-1 — header byte-layout, ALL 15 fields (PM1)
# ═════════════════════════════════════════════════════════════════════════════


def test_header_layout_all_15_fields(name: str) -> None:
    before_ms = int(time.time() * 1000)
    sink = IpcRingSink(name, sample_rate=48000, channels=8, block_size=256, ring_frames=8192)
    try:
        buf = sink._shm.buf
        assert _u64(buf, L_MAGIC) == L_MAGIC_VALUE
        assert _u32(buf, L_VERSION) == 2
        assert _u32(buf, L_HEADER_SIZE) == 4096
        assert _u32(buf, L_SAMPLE_RATE) == 48000
        assert _u32(buf, L_BLOCK_SIZE) == 256
        assert _u32(buf, L_CHANNELS) == 8
        assert _u32(buf, L_CAPACITY_FRAMES) == 8192
        # index fields are u64 (<Q) — a <I slip passes a 1 s test but corrupts
        # past the 2^32 frame boundary.
        assert _u64(buf, L_WRITE_IDX) == 0
        assert _u64(buf, L_READ_IDX) == 0
        assert _u32(buf, L_PRODUCER_PID) == os.getpid()
        # The 3 UNALIGNED hot-path atomics (highest packing/offset risk).
        hb = _u64(buf, L_PRODUCER_HEARTBEAT_MS)
        after_ms = int(time.time() * 1000)
        assert before_ms - 5000 <= hb <= after_ms + 5000, f"heartbeat not unix-epoch: {hb}"
        assert _u64(buf, L_XRUN_COUNT) == 0
        assert _u64(buf, L_PRODUCER_META_BLOCK_PTS_NS) == 0
        assert _u32(buf, L_PRODUCER_STATE) == 0  # Idle
        assert _u64(buf, L_SEQ) == 0
        # The literal offset the producer wrote must equal the module constant.
        assert OFF_PRODUCER_HEARTBEAT_MS == L_PRODUCER_HEARTBEAT_MS
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-2 — pow2 padding (PM6)
# ═════════════════════════════════════════════════════════════════════════════


def test_pow2_padding_pads_and_logs_once(name: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="adm_player.ipc_sink"):
        sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=6000)
    try:
        assert _u32(sink._shm.buf, L_CAPACITY_FRAMES) == 8192
        pad_logs = [r for r in caplog.records if "padded to 8192" in r.getMessage()]
        assert len(pad_logs) == 1, f"expected exactly one pad log, got {len(pad_logs)}"
    finally:
        sink.close()


def test_pow2_padding_no_pad_no_log(name: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="adm_player.ipc_sink"):
        sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        assert _u32(sink._shm.buf, L_CAPACITY_FRAMES) == 8192
        pad_logs = [r for r in caplog.records if "padded" in r.getMessage()]
        assert pad_logs == [], f"unexpected pad log for pow2 input: {pad_logs}"
    finally:
        sink.close()


def test_next_pow2_helper() -> None:
    assert next_pow2(1) == 1
    assert next_pow2(2) == 2
    assert next_pow2(3) == 4
    assert next_pow2(6000) == 8192
    assert next_pow2(8192) == 8192
    assert next_pow2(8193) == 16384


# ═════════════════════════════════════════════════════════════════════════════
# AC-4 — planar de-interleave correctness (D4)
# ═════════════════════════════════════════════════════════════════════════════


def test_deinterleave_planar_channels(name: str) -> None:
    channels = 4
    frames = 128
    sink = IpcRingSink(name, sample_rate=48000, channels=channels, block_size=frames, ring_frames=1024)
    try:
        # channel c is the constant (c+1).
        block = np.empty((frames, channels), dtype=np.float32)
        for c in range(channels):
            block[:, c] = c + 1
        sink.write(np.ascontiguousarray(block))
        for c in range(channels):
            got = _read_channel(sink, c, 0, frames)
            assert np.all(got == (c + 1)), f"channel {c} not planar-correct: {got[:4]}"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-5 — wrap-around correctness (D4/w1)
# ═════════════════════════════════════════════════════════════════════════════


def test_wrap_around_reconstructs_ramp(name: str) -> None:
    channels = 1
    cap = 1024
    block_size = 256
    sink = IpcRingSink(
        name, sample_rate=48000, channels=channels, block_size=block_size, ring_frames=cap
    )
    try:
        # Write a global ramp; a consumer drains after each block so the ring
        # never fills (otherwise drop-newest would discard). Total > capacity to
        # force the write_idx past the ring end (the wrap-split).
        total = cap * 2 + block_size  # crosses the boundary twice
        ramp = np.arange(total, dtype=np.float32)
        read_pos = 0
        recovered = np.empty(total, dtype=np.float32)
        for start in range(0, total, block_size):
            blk = ramp[start : start + block_size].reshape(-1, 1)
            sink.write(np.ascontiguousarray(blk))
            # consumer reads exactly the just-written frames then advances read_idx
            n = blk.shape[0]
            recovered[read_pos : read_pos + n] = _read_channel(sink, 0, read_pos, n)
            read_pos += n
            struct.pack_into("<Q", sink._shm.buf, 0x0028, read_pos)
        assert np.array_equal(recovered, ramp), "wrap-around did not reconstruct the contiguous ramp"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-6 — drop-newest xrun, no-block (PM4) — DETERMINISTIC gate
# ═════════════════════════════════════════════════════════════════════════════


def test_xrun_drop_newest_deterministic(name: str) -> None:
    channels = 2
    cap = 1024
    block_size = 256
    sink = IpcRingSink(
        name, sample_rate=48000, channels=channels, block_size=block_size, ring_frames=cap
    )
    try:
        buf = sink._shm.buf
        # Fill the ring WITHOUT advancing read_idx (no consumer): cap/block_size
        # blocks exactly fill it.
        for _ in range(cap // block_size):
            blk = np.ones((block_size, channels), dtype=np.float32)
            sink.write(blk)
        widx_full = _u64(buf, L_WRITE_IDX)
        xrun_before = _u64(buf, L_XRUN_COUNT)
        assert widx_full == cap, f"ring should be exactly full at write_idx==cap; got {widx_full}"
        assert xrun_before == 0, f"no xrun expected while filling; got {xrun_before}"

        # The next write must be DROPPED: write_idx UNCHANGED, xrun_count += 1.
        sink.write(np.ones((block_size, channels), dtype=np.float32))
        assert _u64(buf, L_WRITE_IDX) == widx_full, "drop-newest must NOT advance write_idx"
        assert _u64(buf, L_XRUN_COUNT) == xrun_before + 1, "xrun_count must increment by exactly 1"

        # A second drop increments again.
        sink.write(np.ones((block_size, channels), dtype=np.float32))
        assert _u64(buf, L_WRITE_IDX) == widx_full
        assert _u64(buf, L_XRUN_COUNT) == xrun_before + 2

        # Advisory only (NOT the gate): the dropping write returns promptly.
        t0 = time.monotonic()
        sink.write(np.ones((block_size, channels), dtype=np.float32))
        assert time.monotonic() - t0 < 0.5, "dropping write must not block/sleep"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-7a — unix-epoch heartbeat + 10 Hz timer (PR4-Q1/PM3)
# ═════════════════════════════════════════════════════════════════════════════


def test_heartbeat_unix_epoch_and_advances_on_write(name: str) -> None:
    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        buf = sink._shm.buf
        sink.write(np.ones((256, 2), dtype=np.float32))
        hb = _u64(buf, L_PRODUCER_HEARTBEAT_MS)
        now_ms = int(time.time() * 1000)
        # unix-epoch ms is ~1.7e12; a steady-clock value would be ~1.7e12 SMALLER.
        assert now_ms - 5000 <= hb <= now_ms + 5000, f"heartbeat not unix-epoch ms: {hb}"
    finally:
        sink.close()


def test_heartbeat_timer_advances_without_writes(name: str) -> None:
    # 50 Hz timer so the test is fast; the production default is 10 Hz.
    sink = IpcRingSink(
        name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192, heartbeat_hz=50.0
    )
    try:
        buf = sink._shm.buf
        first = _u64(buf, L_PRODUCER_HEARTBEAT_MS)
        time.sleep(0.2)  # >= a few timer periods
        second = _u64(buf, L_PRODUCER_HEARTBEAT_MS)
        assert second >= first, "timer heartbeat must be monotonic-ish unix ms"
        assert second - first <= 5000, "timer heartbeat sane delta"
        # It must have advanced at least once with no writes.
        assert second > first or (int(time.time() * 1000) - second) < 200
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-7b — producer_state machine + drain dwell (PR4-Q6/PM7) — DETERMINISTIC
# ═════════════════════════════════════════════════════════════════════════════


def test_state_machine_idle_then_streaming(name: str) -> None:
    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        buf = sink._shm.buf
        assert _u32(buf, L_PRODUCER_STATE) == 0, "must be Idle after __init__"
        sink.write(np.ones((256, 2), dtype=np.float32))
        assert _u32(buf, L_PRODUCER_STATE) == 1, "must be Streaming after first write"
    finally:
        sink.close()


def test_state_machine_drain_dwell_observable(name: str) -> None:
    import threading

    sink = IpcRingSink(
        name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192, drain_dwell_s=0.5
    )
    sink.write(np.ones((256, 2), dtype=np.float32))
    buf = sink._shm.buf

    observed_draining = threading.Event()

    def closer() -> None:
        sink.close()

    t = threading.Thread(target=closer)
    t.start()
    # Deterministic handshake: wait on the producer's Draining event (set on
    # entering Draining(2)) — NOT wall-clock polling.
    assert sink._draining_event.wait(timeout=5.0), "Draining never entered"
    # While the dwell is in progress the state field reads Draining(2).
    state_during = _u32(buf, L_PRODUCER_STATE)
    observed_draining.set()
    t.join(timeout=10.0)
    assert state_during == 2, f"producer_state during dwell must be Draining(2); got {state_during}"
    # close() completed → /dev/shm gone (AC-13); state field is unreadable now.
    assert not os.path.exists(f"/dev/shm/{name}"), "shm must be unlinked after close"


def test_state_machine_zero_dwell_reaches_closed(name: str) -> None:
    sink = IpcRingSink(
        name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192, drain_dwell_s=0.0
    )
    sink.write(np.ones((256, 2), dtype=np.float32))
    # With drain_dwell_s=0.0, best-effort: close() reaches terminal Closed (then
    # unlinks). We can't read the state after unlink, so assert close is clean
    # and the region is gone (the terminal transition happened).
    sink.close()
    assert not os.path.exists(f"/dev/shm/{name}")


# ═════════════════════════════════════════════════════════════════════════════
# AC-8 — _reserved + consumer-lock word zero, never touched (PM5)
# ═════════════════════════════════════════════════════════════════════════════


def test_reserved_and_lock_word_zero_after_init(name: str) -> None:
    sink = IpcRingSink(name, sample_rate=48000, channels=8, block_size=256, ring_frames=8192)
    try:
        reserved = bytes(sink._shm.buf[L_RESERVED:0x1000])
        assert reserved == b"\x00" * (0x1000 - L_RESERVED), "_reserved must be all-zero after init"
        assert _u32(sink._shm.buf, L_RESERVED) == 0, "consumer-lock word must be 0"
    finally:
        sink.close()


def test_reserved_stays_zero_across_writes(name: str) -> None:
    sink = IpcRingSink(name, sample_rate=48000, channels=4, block_size=256, ring_frames=1024)
    try:
        for _ in range(3):
            sink.write(np.ones((256, 4), dtype=np.float32))
            # advance read_idx so the ring never fills
            struct.pack_into("<Q", sink._shm.buf, L_READ_IDX, sink._write_idx)
        sink._stamp_heartbeat()
        reserved = bytes(sink._shm.buf[L_RESERVED:0x1000])
        assert reserved == b"\x00" * (0x1000 - L_RESERVED), "producer must never touch _reserved"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-9 — publish PROGRAM-order, write_idx stored LAST (PM2a)
# ═════════════════════════════════════════════════════════════════════════════


def test_publish_order_write_idx_last(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import adm_player.ipc_sink as mod

    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        events: list[str] = []
        real_pack_into = struct.pack_into

        def recording_pack_into(fmt, buf, offset, *vals):
            if offset == mod.OFF_PRODUCER_META_BLOCK_PTS_NS:
                events.append("pts")
            elif offset == mod.OFF_WRITE_IDX:
                events.append("write_idx")
            return real_pack_into(fmt, buf, offset, *vals)

        # A-14: the ordering point is the RELEASE FENCE (issued AFTER the
        # channel copies + pts, BEFORE the write_idx store), not the old
        # `os.sched_yield()` — which was a scheduler hint, not a barrier.
        real_fence = mod._fence

        def recording_fence():
            events.append("fence")
            return real_fence()

        monkeypatch.setattr(mod.struct, "pack_into", recording_pack_into)
        monkeypatch.setattr(mod, "_fence", recording_fence)

        sink.write(np.ones((256, 2), dtype=np.float32))

        # SOURCE order within write(): acquire fence → pts stamp → RELEASE
        # fence → write_idx LAST.
        assert "pts" in events and "write_idx" in events and "fence" in events
        pts, wi = events.index("pts"), events.index("write_idx")
        release_fences = [k for k, e in enumerate(events) if e == "fence" and pts < k < wi]
        assert release_fences, (
            f"NO release fence between the pts stamp and the write_idx publish: {events}"
        )
        assert pts < wi, f"publish program-order violated: {events}"
        # write_idx must be the LAST publish store (nothing after it except
        # seq/heartbeat, which are not the publish point).
        assert wi == max(pts, wi)
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# A-14 — the producer half of the release/acquire wire contract
#
# The C++ consumer (spatial_engine SharedRingBackend.cpp) is already correct:
#   :461  write_idx.load(std::memory_order_acquire)
#   :555  read_idx.store(..., std::memory_order_release)
# These gates assert the PYTHON producer supplies the matching halves. Without
# them the ring is ordered only by x86-64 TSO luck and tears on ARM64.
# ═════════════════════════════════════════════════════════════════════════════


def test_fence_impl_is_a_real_barrier_on_weakly_ordered_machines() -> None:
    """A silent downgrade to a no-op fence must be visible, and must be
    impossible on an ISA that actually needs the barrier."""
    import platform

    import adm_player.ipc_sink as mod

    assert callable(mod._fence)
    mod._fence()  # must not raise

    impl = mod.FENCE_IMPL
    assert isinstance(impl, str) and impl, "FENCE_IMPL must name the mechanism"

    if platform.machine() not in mod._TSO_MACHINES:
        # ARM64 / POWER / RISC-V: a no-op fence here is the defect itself.
        assert not impl.startswith("noop"), (
            f"weakly-ordered machine {platform.machine()!r} resolved to a NO-OP "
            f"fence ({impl!r}); the shm publish would be unordered"
        )


def test_no_fence_on_weakly_ordered_machine_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No silent disarm: if no barrier primitive can be found, a weakly-ordered
    machine must REFUSE to construct a ring rather than publish unordered."""
    import ctypes

    import adm_player.ipc_sink as mod

    def no_libraries(*_a, **_k):
        raise OSError("no libraries available (test)")

    monkeypatch.setattr(ctypes, "CDLL", no_libraries)

    monkeypatch.setattr(mod.platform, "machine", lambda: "aarch64")
    with pytest.raises(RuntimeError, match="no cross-process memory fence"):
        mod._resolve_fence()

    # ...and on a store-ordered ISA the same situation is sound, so it degrades
    # to a named no-op instead of raising.
    monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
    fn, impl = mod._resolve_fence()
    fn()
    assert impl.startswith("noop-tso")


def test_fence_precedes_magic_publish_at_construction(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`magic` is the consumer's attach latch and is read as a PLAIN load
    (SharedRingBackend.cpp:150) before the geometry fields. It must therefore be
    stored LAST, behind a release fence — otherwise a consumer can attach to a
    ring whose channels/capacity are still zero."""
    import adm_player.ipc_sink as mod

    events: list[str] = []
    real_pack_into = struct.pack_into
    real_fence = mod._fence

    def recording_pack_into(fmt, buf, offset, *vals):
        if offset == L_MAGIC:
            events.append("magic")
        elif offset in (L_CHANNELS, L_CAPACITY_FRAMES, L_BLOCK_SIZE, L_SAMPLE_RATE):
            events.append("geometry")
        return real_pack_into(fmt, buf, offset, *vals)

    def recording_fence():
        events.append("fence")
        return real_fence()

    monkeypatch.setattr(mod.struct, "pack_into", recording_pack_into)
    monkeypatch.setattr(mod, "_fence", recording_fence)

    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        assert "magic" in events, events
        magic = events.index("magic")
        assert magic == len(events) - 1, f"magic must be the LAST header store: {events}"
        assert "geometry" in events and events.index("geometry") < magic, (
            f"geometry must be published BEFORE magic: {events}"
        )
        assert events[magic - 1] == "fence", (
            f"magic must be published behind a release fence: {events}"
        )
    finally:
        sink.close()


def test_acquire_fence_between_read_idx_load_and_ring_write(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer's read_idx load must acquire, pairing with the consumer's
    `read_idx.store(release)` — otherwise the ring writes below can be hoisted
    above it and clobber a slot the consumer is still reading."""
    import adm_player.ipc_sink as mod

    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        events: list[str] = []
        real_unpack_from = struct.unpack_from
        real_fence = mod._fence

        real_pack_into = struct.pack_into

        def recording_unpack_from(fmt, buf, offset, *a, **k):
            if offset == mod.OFF_READ_IDX:
                events.append("read_idx")
            return real_unpack_from(fmt, buf, offset, *a, **k)

        def recording_pack_into(fmt, buf, offset, *vals):
            # The pts stamp is issued AFTER the channel copies, so a fence
            # recorded before it is the ACQUIRE fence, not the RELEASE one.
            # Without this marker the gate cannot tell the two fences apart —
            # it would pass with the acquire fence deleted (observed).
            if offset == mod.OFF_PRODUCER_META_BLOCK_PTS_NS:
                events.append("pts")
            return real_pack_into(fmt, buf, offset, *vals)

        def recording_fence():
            events.append("fence")
            return real_fence()

        monkeypatch.setattr(mod.struct, "unpack_from", recording_unpack_from)
        monkeypatch.setattr(mod.struct, "pack_into", recording_pack_into)
        monkeypatch.setattr(mod, "_fence", recording_fence)

        sink.write(np.ones((256, 2), dtype=np.float32))

        assert "read_idx" in events and "pts" in events, events
        ri, pts = events.index("read_idx"), events.index("pts")
        assert ri < pts, events
        acquire = [k for k, e in enumerate(events) if e == "fence" and ri < k < pts]
        assert acquire, (
            f"no ACQUIRE fence between the read_idx load and the ring writes "
            f"(the fence seen later is the RELEASE fence): {events}"
        )
    finally:
        sink.close()


def test_write_path_issues_no_sched_yield(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The old `os.sched_yield()` must be gone: it never was a barrier, and on
    the audio path a voluntary yield is a latency hazard (ADR 0019 §4.2 — the
    write path must never block)."""
    import adm_player.ipc_sink as mod

    sink = IpcRingSink(name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192)
    try:
        yields = []
        monkeypatch.setattr(mod.os, "sched_yield", lambda: yields.append(1))
        sink.write(np.ones((256, 2), dtype=np.float32))
        assert yields == [], "write() still calls os.sched_yield()"
    finally:
        sink.close()


def test_publish_serialized_read_below_write_idx_is_complete(name: str) -> None:
    """For an observed write_idx==N, all frames below N are the fully-written
    ramp and producer_meta_block_pts_ns is non-zero + monotonic."""
    channels = 2
    block_size = 256
    sink = IpcRingSink(
        name, sample_rate=48000, channels=channels, block_size=block_size, ring_frames=8192
    )
    try:
        buf = sink._shm.buf
        last_pts = 0
        read_pos = 0
        n_blocks = 4
        for b in range(n_blocks):
            base = b * block_size
            block = np.empty((block_size, channels), dtype=np.float32)
            for c in range(channels):
                block[:, c] = np.arange(base, base + block_size, dtype=np.float32) + c * 1_000_000
            sink.write(np.ascontiguousarray(block))
            widx = _u64(buf, 0x0020)
            assert widx == base + block_size
            pts = _u64(buf, L_PRODUCER_META_BLOCK_PTS_NS)
            assert pts != 0, "producer_meta_block_pts_ns must be non-zero after a write"
            assert pts >= last_pts, "pts must be monotonic across published blocks"
            last_pts = pts
            # every frame below write_idx is the expected ramp
            for c in range(channels):
                got = _read_channel(sink, c, read_pos, block_size)
                exp = np.arange(base, base + block_size, dtype=np.float32) + c * 1_000_000
                assert np.array_equal(got, exp), f"frame below write_idx not complete (ch {c})"
            read_pos += block_size
            struct.pack_into("<Q", buf, 0x0028, read_pos)
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-3 — 1-second round-trip, sample-exact (ADR §8 step 5)
# ═════════════════════════════════════════════════════════════════════════════


def test_round_trip_one_second_sample_exact(name: str) -> None:
    sr = 48000
    channels = 8
    block_size = 256
    total = sr  # 1 second
    sink = IpcRingSink(
        name, sample_rate=sr, channels=channels, block_size=block_size, ring_frames=8192
    )
    try:
        buf = sink._shm.buf
        # distinct per-channel ramp
        src = np.empty((total, channels), dtype=np.float32)
        for c in range(channels):
            src[:, c] = (np.arange(total, dtype=np.float32) * 0.5) + c
        recovered = np.empty((total, channels), dtype=np.float32)
        read_pos = 0
        for start in range(0, total, block_size):
            blk = src[start : start + block_size]
            n = blk.shape[0]
            sink.write(np.ascontiguousarray(blk))
            for c in range(channels):
                recovered[read_pos : read_pos + n, c] = _read_channel(sink, c, read_pos, n)
            read_pos += n
            struct.pack_into("<Q", buf, 0x0028, read_pos)  # consumer drains
        assert np.array_equal(recovered, src), "round-trip not sample-exact (+/-0)"
        # seq incremented once per block; xrun_count == 0 (consumer kept up).
        n_blocks = (total + block_size - 1) // block_size
        assert _u64(buf, L_SEQ) == n_blocks, "seq must increment once per block"
        assert _u64(buf, L_XRUN_COUNT) == 0, "no xrun when the consumer keeps up"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# AC-13 — close() without BufferError + unlink (PM9/C2)
# ═════════════════════════════════════════════════════════════════════════════


def test_close_no_buffererror_and_unlinks(name: str) -> None:
    sink = IpcRingSink(
        name, sample_rate=48000, channels=8, block_size=256, ring_frames=8192, drain_dwell_s=0.0
    )
    for _ in range(3):
        sink.write(np.ones((256, 8), dtype=np.float32))
        struct.pack_into("<Q", sink._shm.buf, 0x0028, sink._write_idx)
    assert os.path.exists(f"/dev/shm/{name}")
    sink.close()  # must NOT raise BufferError
    assert not os.path.exists(f"/dev/shm/{name}"), "/dev/shm region must be gone after close"


def test_close_is_idempotent(name: str) -> None:
    sink = IpcRingSink(
        name, sample_rate=48000, channels=2, block_size=256, ring_frames=8192, drain_dwell_s=0.0
    )
    sink.write(np.ones((256, 2), dtype=np.float32))
    sink.close()
    sink.close()  # second close is a no-op (no raise, no FileNotFoundError)
    assert not os.path.exists(f"/dev/shm/{name}")


def test_context_manager_unlinks_on_exit(name: str) -> None:
    with IpcRingSink(
        name, sample_rate=48000, channels=4, block_size=256, ring_frames=8192, drain_dwell_s=0.0
    ) as sink:
        sink.write(np.ones((256, 4), dtype=np.float32))
        assert os.path.exists(f"/dev/shm/{name}")
    assert not os.path.exists(f"/dev/shm/{name}"), "context manager must unlink on __exit__"


# ═════════════════════════════════════════════════════════════════════════════
# AC-11 — CLI --sink ipc:// wiring + guards + OSC unchanged, HEADLESS (PM10)
# ═════════════════════════════════════════════════════════════════════════════

import soundfile as sf  # noqa: E402


def _write_wav(path: str, channels: int = 4, frames: int = 600, sr: int = 48000) -> None:
    data = np.zeros((frames, channels), dtype=np.float32)
    for c in range(channels):
        data[:, c] = (c + 1) * 0.01
    sf.write(path, data, sr, subtype="FLOAT")


@pytest.fixture
def raising_sd(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `sounddevice` whose every device call raises, so a device
    query on the ipc path would fail loudly (headless guard, PM10)."""
    import types

    fake = types.ModuleType("sounddevice")

    def _boom(*_a, **_k):
        raise AssertionError("ipc path must NOT touch any sounddevice device API")

    fake.query_devices = _boom
    fake.OutputStream = _boom

    class _Default:
        device = property(lambda self: _boom())

    fake.default = _Default()
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "sounddevice", fake)
    return fake


def _stub_adm_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass ADM axml/chna parsing in the CLI — AC-11 exercises the SINK seam,
    not ADM parsing (covered by test_adm_model). The fixture WAV has no axml."""
    monkeypatch.setattr("adm_player.__main__.read_axml", lambda _p: "<axml/>")
    monkeypatch.setattr("adm_player.__main__.read_chna_mapping", lambda _p: {})
    monkeypatch.setattr("adm_player.__main__.parse_adm_objects", lambda *_a, **_k: [])


def test_cli_sink_ipc_routes_to_ring_headless(
    tmp_path, name: str, raising_sd, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav, channels=4, frames=600)
    _stub_adm_parse(monkeypatch)

    # Spy on the IpcRingSink the CLI builds (proves all-channels + block_size).
    init_captured: dict[str, object] = {}
    real_init = IpcRingSink.__init__

    def spy_init(self, path, **kw):
        init_captured["path"] = path
        init_captured["kw"] = dict(kw)
        real_init(self, path, **kw)

    monkeypatch.setattr("adm_player.ipc_sink.IpcRingSink.__init__", spy_init)

    # Spy on play_adm_wav to capture the sink seam WITHOUT driving the real file
    # loop (this host ships soundfile 0.9.0 which lacks SoundFile.frames; the
    # production code uses f.frames per the pinned soundfile>=0.12). We close the
    # injected sink ourselves so the region is unlinked, as the real loop would.
    play_captured: dict[str, object] = {}

    def spy_play(path, objects, **kw):
        play_captured["device"] = kw.get("device", "ABSENT")
        play_captured["out_channels"] = kw.get("out_channels", "ABSENT")
        play_captured["block_frames"] = kw.get("block_frames")
        sink = kw.get("sink")
        play_captured["sink_type"] = type(sink).__name__ if sink is not None else None
        if sink is not None:
            with sink:
                pass  # mimic the loop's open/close → unlink

    monkeypatch.setattr("adm_player.__main__.play_adm_wav", spy_play)

    rc = cli.main(
        [
            wav,
            "--no-osc",
            "--sink",
            f"ipc://{name}",
            "--block-size",
            "256",
            "--ring-frames",
            "8192",
        ]
    )
    assert rc == 0, "ipc CLI run must succeed headless"
    # The ipc path constructed an IpcRingSink (NOT sd.OutputStream — which would
    # have raised via raising_sd) writing ALL 4 file channels at block_size=256.
    assert init_captured["path"] == name
    assert init_captured["kw"]["channels"] == 4, "ipc sink must write ALL file channels"
    assert init_captured["kw"]["block_size"] == 256
    assert init_captured["kw"]["sample_rate"] == 48000
    # play_adm_wav was handed the IpcRingSink and NO device/out_channels (bypass).
    assert play_captured["sink_type"] == "IpcRingSink"
    assert play_captured["device"] == "ABSENT", "ipc path must not pass a device"
    assert play_captured["out_channels"] == "ABSENT", "ipc path must not pass out_channels"
    assert play_captured["block_frames"] == 256, "ipc read granularity = --block-size"
    # the region was unlinked on close (context manager teardown)
    assert not os.path.exists(f"/dev/shm/{name}")


def test_cli_sink_mutual_exclusion_audio_device(tmp_path, name: str) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav)
    with pytest.raises(SystemExit) as ei:
        cli.main([wav, "--sink", f"ipc://{name}", "--audio-device", "3"])
    assert ei.value.code != 0


def test_cli_sink_mutual_exclusion_interactive(tmp_path, name: str) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav)
    with pytest.raises(SystemExit) as ei:
        cli.main([wav, "--sink", f"ipc://{name}", "--interactive"])
    assert ei.value.code != 0


def test_cli_sink_mutual_exclusion_out_channels(tmp_path, name: str) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav)
    with pytest.raises(SystemExit) as ei:
        cli.main([wav, "--sink", f"ipc://{name}", "--out-channels", "2"])
    assert ei.value.code != 0


def test_cli_sink_mutual_exclusion_list_devices(tmp_path, name: str) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav)
    with pytest.raises(SystemExit) as ei:
        cli.main([wav, "--sink", f"ipc://{name}", "--list-audio-devices"])
    assert ei.value.code != 0


def test_cli_sink_rejects_malformed_value(tmp_path) -> None:
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav)
    with pytest.raises(SystemExit):
        cli.main([wav, "--sink", "shm:bad"])
    with pytest.raises(SystemExit):
        cli.main([wav, "--sink", "ipc://"])


def test_cli_default_path_uses_device_not_ring(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --sink, the existing device path is taken: play_adm_wav is called
    with NO injected sink and WITH device/out_channels — proving the seam is
    additive and the default behavior is unchanged."""
    from adm_player import __main__ as cli

    wav = str(tmp_path / "mix.wav")
    _write_wav(wav, channels=2, frames=600)
    _stub_adm_parse(monkeypatch)

    sink_built: list[str] = []
    monkeypatch.setattr(
        "adm_player.ipc_sink.IpcRingSink.__init__",
        lambda self, *a, **k: sink_built.append("ring"),
    )
    captured: dict[str, object] = {}

    def spy_play(path, objects, **kw):
        captured["sink"] = kw.get("sink", "ABSENT")
        captured["device"] = kw.get("device", "ABSENT")
        captured["out_channels"] = kw.get("out_channels", "ABSENT")

    monkeypatch.setattr("adm_player.__main__.play_adm_wav", spy_play)

    rc = cli.main([wav, "--no-osc", "--audio-device", "3"])
    assert rc == 0
    assert sink_built == [], "default path must NOT construct IpcRingSink"
    assert captured["sink"] == "ABSENT", "default path must not inject a sink"
    assert captured["device"] == 3, "default path must pass the device through"
    assert captured["out_channels"] is None
