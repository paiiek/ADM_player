"""TRACK A3 (ADR 0029) — U4 tests for the metadata sidecar PRODUCER (`MetaRingSink`).

The header/record byte-layout assertions use LITERAL offsets transcribed
directly from core/src/audio_io/shm/MetaRingHeader.h — NOT the producer's own
constants — so a producer/consumer shared-mis-offset bug still fails. The
round-trip reader mirrors the C++ consumer's masked-index read; no C++ build
dependency.
"""

from __future__ import annotations

import logging
import os
import struct
import time

import pytest

from adm_player.meta_sink import (
    META_COORD_POLAR,
    META_FLAG_ACTIVE,
    MetaRecord,
    MetaRingSink,
    next_pow2,
    record_byte_offset,
)

# ── LITERAL offsets transcribed from MetaRingHeader.h (NOT from meta_sink) ─────
# Header table MetaRingHeader.h:129-146 / struct :150-166 / static_asserts :170-186.
L_MAGIC = 0x0000
L_VERSION = 0x0008
L_HEADER_SIZE = 0x000C
L_SAMPLE_RATE = 0x0010
L_SLOT_COUNT = 0x0014
L_RECORD_CAPACITY = 0x0018
L_RECORD_SIZE = 0x001C
L_WRITE_IDX = 0x0020
L_READ_IDX = 0x0028
L_PRODUCER_PID = 0x0030
L_PAD0 = 0x0034  # wire v2 (P-137): natural alignment, explicit zero pads
L_HEARTBEAT_MS = 0x0038
L_XRUN_COUNT = 0x0040
L_PRODUCER_STATE = 0x0048
L_PAD1 = 0x004C
L_SEQ = 0x0050
L_LAYOUT_HASH = 0x0058
L_RESERVED = 0x0060
L_LAYOUT_HASH_VALUE = 0x9F317DE56F623BC6  # kMetaLayoutHash (MetaRingHeader.h, v2)
L_MAGIC_VALUE = 0x5350454D45544131  # "SPEMETA1" LE u64 (MetaRingHeader.h:32)

# Record table MetaRingHeader.h:73-86 / struct :90-104 / static_asserts :108-121.
LR_FRAME_INDEX = 0x0000
LR_OBJ_ID = 0x0008
LR_COORD_MODE = 0x000C
LR_A0 = 0x0010
LR_A1 = 0x0014
LR_A2 = 0x0018
LR_GAIN_LIN = 0x001C
LR_WIDTH_RAD = 0x0020
LR_FLAGS = 0x0024
LR_REC_SEQ = 0x0028
LR_INTERP_LEN = 0x002C
LR_JUMP_FLAG = 0x0030
LR_RECORD_SIZE = 64

_NAME_SEQ = 0


def _unique_name() -> str:
    global _NAME_SEQ
    _NAME_SEQ += 1
    return f"a3metatest-{os.getpid()}-{_NAME_SEQ}-{int(time.time()*1e6) % 1_000_000}"


@pytest.fixture
def name() -> str:
    n = _unique_name()
    yield n
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


def _f32(buf, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


# ═════════════════════════════════════════════════════════════════════════════
# U4 — header byte-layout, every field at its transcribed offset
# ═════════════════════════════════════════════════════════════════════════════


def test_header_layout_all_fields(name: str) -> None:
    before_ms = int(time.time() * 1000)
    sink = MetaRingSink(name, sample_rate=48000, slot_count=128, record_capacity=1024)
    try:
        buf = sink._shm.buf
        assert _u64(buf, L_MAGIC) == L_MAGIC_VALUE
        assert _u32(buf, L_VERSION) == 2
        assert _u32(buf, L_HEADER_SIZE) == 4096
        assert _u32(buf, L_SAMPLE_RATE) == 48000
        assert _u32(buf, L_SLOT_COUNT) == 128
        assert _u32(buf, L_RECORD_CAPACITY) == 1024
        assert _u32(buf, L_RECORD_SIZE) == 64
        assert _u64(buf, L_WRITE_IDX) == 0
        assert _u64(buf, L_READ_IDX) == 0
        assert _u32(buf, L_PRODUCER_PID) == os.getpid()
        hb = _u64(buf, L_HEARTBEAT_MS)
        after_ms = int(time.time() * 1000)
        assert before_ms - 5000 <= hb <= after_ms + 5000, f"heartbeat not unix-epoch: {hb}"
        assert _u64(buf, L_XRUN_COUNT) == 0
        assert _u32(buf, L_PRODUCER_STATE) == 0  # Idle
        assert _u64(buf, L_SEQ) == 0
        assert _u64(buf, L_LAYOUT_HASH) == L_LAYOUT_HASH_VALUE
        assert _u32(buf, L_PAD0) == 0 and _u32(buf, L_PAD1) == 0
    finally:
        sink.close()


def test_reserved_zero_after_init(name: str) -> None:
    sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=256)
    try:
        # _reserved spans 0x60 → 0x1000 (the rest of the 4096-byte header).
        reserved = bytes(sink._shm.buf[L_RESERVED:0x1000])
        assert reserved == b"\x00" * (0x1000 - L_RESERVED), "_reserved must be all-zero after init"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# U4 — next_pow2 record_capacity padding (logged once)
# ═════════════════════════════════════════════════════════════════════════════


def test_record_capacity_padding_pads_and_logs_once(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="adm_player.meta_sink"):
        sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=1000)
    try:
        assert _u32(sink._shm.buf, L_RECORD_CAPACITY) == 1024
        pad_logs = [r for r in caplog.records if "padded to 1024" in r.getMessage()]
        assert len(pad_logs) == 1, f"expected exactly one pad log, got {len(pad_logs)}"
    finally:
        sink.close()


def test_record_capacity_no_pad_no_log(name: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="adm_player.meta_sink"):
        sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=1024)
    try:
        assert _u32(sink._shm.buf, L_RECORD_CAPACITY) == 1024
        assert [r for r in caplog.records if "padded" in r.getMessage()] == []
    finally:
        sink.close()


def test_next_pow2_helper() -> None:
    assert next_pow2(1) == 1
    assert next_pow2(1000) == 1024
    assert next_pow2(1024) == 1024
    assert next_pow2(1025) == 2048


# ═════════════════════════════════════════════════════════════════════════════
# U4 — record round-trip: pack records, read every field back at the offset
# ═════════════════════════════════════════════════════════════════════════════


def _read_record(sink: MetaRingSink, index: int) -> dict[str, float | int]:
    """Read record slot `index` mirroring record_byte_offset (masked index)."""
    buf = sink._shm.buf
    base = record_byte_offset(index, sink._capacity)
    return {
        "frame_index": _u64(buf, base + LR_FRAME_INDEX),
        "obj_id": _u32(buf, base + LR_OBJ_ID),
        "coord_mode": _u32(buf, base + LR_COORD_MODE),
        "a0": _f32(buf, base + LR_A0),
        "a1": _f32(buf, base + LR_A1),
        "a2": _f32(buf, base + LR_A2),
        "gain_lin": _f32(buf, base + LR_GAIN_LIN),
        "width_rad": _f32(buf, base + LR_WIDTH_RAD),
        "flags": _u32(buf, base + LR_FLAGS),
        "rec_seq": _u32(buf, base + LR_REC_SEQ),
        "interp_len": _f32(buf, base + LR_INTERP_LEN),
        "jump_flag": _u32(buf, base + LR_JUMP_FLAG),
    }


def test_record_round_trip_fields(name: str) -> None:
    sink = MetaRingSink(name, sample_rate=48000, slot_count=128, record_capacity=1024)
    try:
        rec = MetaRecord(
            frame_index=0,  # overwritten by publish's authoritative frame_index
            obj_id=7,
            coord_mode=META_COORD_POLAR,
            a0=-90.0,
            a1=12.5,
            a2=0.5,
            gain_lin=0.75,
            width_rad=0.3,
            flags=META_FLAG_ACTIVE,
        )
        sink.publish([rec], frame_index=24000)
        got = _read_record(sink, 0)
        assert got["frame_index"] == 24000, "publish must stamp the authoritative frame_index"
        assert got["obj_id"] == 7
        assert got["coord_mode"] == META_COORD_POLAR
        assert got["a0"] == pytest.approx(-90.0)
        assert got["a1"] == pytest.approx(12.5)
        assert got["a2"] == pytest.approx(0.5)
        assert got["gain_lin"] == pytest.approx(0.75)
        assert got["width_rad"] == pytest.approx(0.3)
        assert got["flags"] == META_FLAG_ACTIVE
        assert got["rec_seq"] == 0, "first record rec_seq is 0"
        assert got["interp_len"] == 0.0, "interp_len_frames is A4-reserved (0)"
        assert got["jump_flag"] == 0, "jump_flag is A4-reserved (0)"
        # write_idx advanced by exactly one record; seq bumped once.
        assert _u64(sink._shm.buf, L_WRITE_IDX) == 1
        assert _u64(sink._shm.buf, L_SEQ) == 1
    finally:
        sink.close()


def test_rec_seq_is_stream_monotonic(name: str) -> None:
    sink = MetaRingSink(name, sample_rate=48000, slot_count=128, record_capacity=1024)
    try:
        r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
        # Two records in one publish, then one more in a second publish.
        sink.publish([r, r], frame_index=0)
        sink.publish([r], frame_index=512)
        assert _read_record(sink, 0)["rec_seq"] == 0
        assert _read_record(sink, 1)["rec_seq"] == 1
        assert _read_record(sink, 2)["rec_seq"] == 2
        assert _u64(sink._shm.buf, L_SEQ) == 2, "seq counts published batches"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# U4 — drop-newest xrun policy (batch granularity)
# ═════════════════════════════════════════════════════════════════════════════


def test_drop_newest_when_ring_full(name: str) -> None:
    cap = 4
    sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=cap)
    try:
        buf = sink._shm.buf
        r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
        # Fill the ring WITHOUT advancing read_idx (no consumer).
        for _ in range(cap):
            sink.publish([r], frame_index=0)
        assert _u64(buf, L_WRITE_IDX) == cap
        assert _u64(buf, L_XRUN_COUNT) == 0, "no xrun while filling"
        # The next publish must be DROPPED: write_idx unchanged, xrun += 1.
        t0 = time.monotonic()
        sink.publish([r], frame_index=0)
        assert time.monotonic() - t0 < 0.5, "dropping publish must not block/sleep"
        assert _u64(buf, L_WRITE_IDX) == cap, "drop-newest must NOT advance write_idx"
        assert _u64(buf, L_XRUN_COUNT) == 1
        sink.publish([r], frame_index=0)
        assert _u64(buf, L_XRUN_COUNT) == 2
    finally:
        sink.close()


def test_empty_publish_is_noop(name: str) -> None:
    sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=64)
    try:
        buf = sink._shm.buf
        sink.publish([], frame_index=1234)
        assert _u64(buf, L_WRITE_IDX) == 0
        assert _u64(buf, L_SEQ) == 0
        assert _u32(buf, L_PRODUCER_STATE) == 0, "empty publish stays Idle"
    finally:
        sink.close()


def test_state_idle_then_streaming(name: str) -> None:
    sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=64)
    try:
        buf = sink._shm.buf
        assert _u32(buf, L_PRODUCER_STATE) == 0, "Idle after __init__"
        r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
        sink.publish([r], frame_index=0)
        assert _u32(buf, L_PRODUCER_STATE) == 1, "Streaming after first publish"
    finally:
        sink.close()


# ═════════════════════════════════════════════════════════════════════════════
# U4 — close() clean (no BufferError) + unlink; idempotent; context manager
# ═════════════════════════════════════════════════════════════════════════════


def test_close_no_buffererror_and_unlinks(name: str) -> None:
    sink = MetaRingSink(
        name, sample_rate=48000, slot_count=128, record_capacity=64, drain_dwell_s=0.0
    )
    r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
    for _ in range(3):
        sink.publish([r], frame_index=0)
        struct.pack_into("<Q", sink._shm.buf, L_READ_IDX, sink._write_idx)
    assert os.path.exists(f"/dev/shm/{name}")
    sink.close()  # must NOT raise BufferError
    assert not os.path.exists(f"/dev/shm/{name}"), "/dev/shm region must be gone after close"


def test_close_is_idempotent(name: str) -> None:
    sink = MetaRingSink(
        name, sample_rate=48000, slot_count=64, record_capacity=64, drain_dwell_s=0.0
    )
    sink.close()
    sink.close()  # no raise, no FileNotFoundError
    assert not os.path.exists(f"/dev/shm/{name}")


def test_context_manager_unlinks_on_exit(name: str) -> None:
    with MetaRingSink(
        name, sample_rate=48000, slot_count=64, record_capacity=64, drain_dwell_s=0.0
    ) as sink:
        r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
        sink.publish([r], frame_index=0)
        assert os.path.exists(f"/dev/shm/{name}")
    assert not os.path.exists(f"/dev/shm/{name}"), "context manager must unlink on __exit__"


def test_publish_after_close_raises(name: str) -> None:
    sink = MetaRingSink(
        name, sample_rate=48000, slot_count=64, record_capacity=64, drain_dwell_s=0.0
    )
    sink.close()
    r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
    with pytest.raises(RuntimeError):
        sink.publish([r], frame_index=0)


def test_invalid_construction_args(name: str) -> None:
    with pytest.raises(ValueError):
        MetaRingSink(name, sample_rate=0, slot_count=64, record_capacity=64)
    with pytest.raises(ValueError):
        MetaRingSink(name, sample_rate=48000, slot_count=0, record_capacity=64)
    with pytest.raises(ValueError):
        MetaRingSink(name, sample_rate=48000, slot_count=129, record_capacity=64)


# ═════════════════════════════════════════════════════════════════════════════
# P-33 — the producer half of the release/acquire wire contract (A-14, mirror
# test_ipc_sink.py). The C++ consumer (MetaRingConsumer.cpp) is already
# correct:
#   :171/:252/:254/:285  write_idx.load(std::memory_order_acquire)
#   :274/:311            read_idx.store(..., std::memory_order_release)
#   :322                 producer_state.load(std::memory_order_acquire)
# Without a matching Python-side RELEASE fence the ring is ordered only by
# x86-64 TSO luck and tears on ARM64 (the previous `os.sched_yield()` was a
# scheduler hint, not a barrier).
# ═════════════════════════════════════════════════════════════════════════════


def test_publish_order_write_idx_last(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import adm_player.meta_sink as mod

    sink = MetaRingSink(name, sample_rate=48000, slot_count=64, record_capacity=64)
    try:
        events: list[str] = []
        real_pack_into = struct.pack_into

        def recording_pack_into(fmt, buf, offset, *vals):
            if offset == mod.OFF_WRITE_IDX:
                events.append("write_idx")
            return real_pack_into(fmt, buf, offset, *vals)

        real_fence = mod._fence

        def recording_fence():
            events.append("fence")
            return real_fence()

        monkeypatch.setattr(mod.struct, "pack_into", recording_pack_into)
        monkeypatch.setattr(mod, "_fence", recording_fence)

        r = MetaRecord(0, 1, META_COORD_POLAR, 0.0, 0.0, 1.0, 1.0, 0.0, META_FLAG_ACTIVE)
        sink.publish([r], frame_index=0)

        assert "fence" in events and "write_idx" in events, events
        # The release fence must precede the write_idx publish (program order).
        assert events.index("fence") < events.index("write_idx"), (
            f"NO release fence before the write_idx publish: {events}"
        )
    finally:
        sink.close()


def test_set_state_fences_before_producer_state_store(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adm_player.meta_sink as mod

    sink = MetaRingSink(
        name, sample_rate=48000, slot_count=64, record_capacity=64, drain_dwell_s=0.0
    )
    try:
        events: list[str] = []
        real_pack_into = struct.pack_into

        def recording_pack_into(fmt, buf, offset, *vals):
            if offset == mod.OFF_PRODUCER_STATE:
                events.append("producer_state")
            return real_pack_into(fmt, buf, offset, *vals)

        real_fence = mod._fence

        def recording_fence():
            events.append("fence")
            return real_fence()

        monkeypatch.setattr(mod.struct, "pack_into", recording_pack_into)
        monkeypatch.setattr(mod, "_fence", recording_fence)

        mod.MetaRingSink._set_state(sink, 1)

        assert "fence" in events and "producer_state" in events, events
        assert events.index("fence") < events.index("producer_state"), (
            f"NO release fence before the producer_state publish: {events}"
        )
    finally:
        sink.close()


def test_fence_impl_is_a_real_barrier(name: str) -> None:
    """A silent downgrade to a no-op fence must be visible via FENCE_IMPL
    (shared with ipc_sink — same `_resolve_fence` instance)."""
    import adm_player.meta_sink as mod

    assert callable(mod._fence)
    mod._fence()  # must not raise
    impl = mod.FENCE_IMPL if hasattr(mod, "FENCE_IMPL") else None
    if impl is None:
        import adm_player.ipc_sink as ipc_mod

        impl = ipc_mod.FENCE_IMPL
    assert isinstance(impl, str) and impl, "FENCE_IMPL must name the mechanism"
