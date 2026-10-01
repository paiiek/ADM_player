"""TRACK A3 (ADR 0029) — Python PRODUCER for the spatial_engine METADATA sidecar.

`class MetaRingSink` is a second, cross-LANGUAGE implementation of the frozen
C++ wire contract `core/src/audio_io/shm/MetaRingHeader.h` (spatial_engine
repo). It creates a SEPARATE POSIX shared-memory region — never inside the
frozen ADR 0019 PCM ring — writes the read-only header fields, and packs
fixed-64-byte `MetaRecord` PODs into a power-of-two record ring keyed by the
absolute PCM frame index. Every header/record offset/type/endianness below is
transcribed BYTE-FOR-BYTE from `MetaRingHeader.h`; the inline
`MetaRingHeader.h:<line>` comment pins each field to its source line (the same
single-source-of-truth discipline as `ipc_sink.py` ↔ `RingHeader.h`). A single
wrong offset is a silent cross-process corruption.

This module mirrors `ipc_sink.py`'s producer discipline: read-only header, byte
offset table, drop-newest xrun policy, 10 Hz heartbeat + producer_state
lifecycle, and a close() view-drop + gc.collect() ordering.
"""

from __future__ import annotations

import gc
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Final

from .ipc_sink import _fence, _heartbeat_ms, next_pow2

_log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants — transcribed from MetaRingHeader.h:32-53
# ─────────────────────────────────────────────────────────────────────────────

# "SPEMETA1" LE u64 (MetaRingHeader.h:32)
SPE_META_MAGIC: Final[int] = 0x5350454D45544131
META_HEADER_SIZE: Final[int] = 4096  # kMetaRingHeaderSize (MetaRingHeader.h:33)
META_HEADER_VERSION: Final[int] = 2  # kMetaRingVersion (wire v2, P-137; v1 is refused by the engine)
# kMetaLayoutHash: FNV-1a-64 of kMetaContractDescriptor (MetaRingHeader.h). DERIVED
# on the engine side (`python3 scripts/meta_layout_hash.py`); stamped at
# OFF_LAYOUT_HASH so the engine admits the segment (0 = unstamped => refused).
META_LAYOUT_HASH: Final[int] = 0x9F317DE56F623BC6

# MetaProducerState enum (MetaRingHeader.h:38-43)
STATE_IDLE: Final[int] = 0       # MetaRingHeader.h:39
STATE_STREAMING: Final[int] = 1  # MetaRingHeader.h:40
STATE_DRAINING: Final[int] = 2   # MetaRingHeader.h:41
STATE_CLOSED: Final[int] = 3     # MetaRingHeader.h:42

# MetaRecord flag bits (MetaRingHeader.h:47-48)
META_FLAG_ACTIVE: Final[int] = 1 << 0  # bit0 object active (MetaRingHeader.h:47)
META_FLAG_MUTE: Final[int] = 1 << 1    # bit1 object muted  (MetaRingHeader.h:48)

# coord_mode values (MetaRingHeader.h:52-53)
META_COORD_POLAR: Final[int] = 0  # a0=az, a1=el, a2=dist (MetaRingHeader.h:52)
META_COORD_CART: Final[int] = 1   # a0=x,  a1=y,  a2=z    (MetaRingHeader.h:53)

# ─────────────────────────────────────────────────────────────────────────────
# MetaRingHeader field byte offsets (MetaRingHeader.h:129-146 table / :150-166
# struct / static_asserts). All fields little-endian. Wire v2 (P-137): natural
# alignment, explicit zero pads at 0x34 and 0x4C, no pragma pack.
# ─────────────────────────────────────────────────────────────────────────────
OFF_MAGIC: Final[int] = 0x0000            # u64          magic           MetaRingHeader.h:151
OFF_VERSION: Final[int] = 0x0008          # u32          version         MetaRingHeader.h:152
OFF_HEADER_SIZE: Final[int] = 0x000C      # u32          header_size     MetaRingHeader.h:153
OFF_SAMPLE_RATE: Final[int] = 0x0010      # u32          sample_rate     MetaRingHeader.h:154
OFF_SLOT_COUNT: Final[int] = 0x0014       # u32          slot_count      MetaRingHeader.h:155
OFF_RECORD_CAPACITY: Final[int] = 0x0018  # u32          record_capacity MetaRingHeader.h:156
OFF_RECORD_SIZE: Final[int] = 0x001C      # u32          record_size     MetaRingHeader.h:157
OFF_WRITE_IDX: Final[int] = 0x0020        # atomic<u64>  write_idx       MetaRingHeader.h:158
OFF_READ_IDX: Final[int] = 0x0028         # atomic<u64>  read_idx        MetaRingHeader.h:159
OFF_PRODUCER_PID: Final[int] = 0x0030     # u32          producer_pid    MetaRingHeader.h:160
OFF_PAD0: Final[int] = 0x0034             # u32          _pad0 (zero)
OFF_HEARTBEAT_MS: Final[int] = 0x0038     # atomic<u64>  heartbeat_ms
OFF_XRUN_COUNT: Final[int] = 0x0040       # atomic<u64>  xrun_count
OFF_PRODUCER_STATE: Final[int] = 0x0048   # atomic<u32>  producer_state
OFF_PAD1: Final[int] = 0x004C             # u32          _pad1 (zero)
OFF_SEQ: Final[int] = 0x0050              # atomic<u64>  seq
OFF_LAYOUT_HASH: Final[int] = 0x0058      # u64          layout_hash (== META_LAYOUT_HASH)
OFF_RESERVED: Final[int] = 0x0060         # u8[0xFA0]    _reserved

# ─────────────────────────────────────────────────────────────────────────────
# MetaRecord field byte offsets (MetaRingHeader.h:73-86 table / :90-104 struct /
# :108-121 static_asserts). Fixed 64-byte POD.
# ─────────────────────────────────────────────────────────────────────────────
RECORD_SIZE: Final[int] = 64                # sizeof(MetaRecord)   MetaRingHeader.h:121
REC_OFF_FRAME_INDEX: Final[int] = 0x0000    # u64  frame_index     MetaRingHeader.h:91
REC_OFF_OBJ_ID: Final[int] = 0x0008         # u32  obj_id          MetaRingHeader.h:92
REC_OFF_COORD_MODE: Final[int] = 0x000C     # u32  coord_mode      MetaRingHeader.h:93
REC_OFF_A0: Final[int] = 0x0010             # f32  a0              MetaRingHeader.h:94
REC_OFF_A1: Final[int] = 0x0014             # f32  a1              MetaRingHeader.h:95
REC_OFF_A2: Final[int] = 0x0018             # f32  a2              MetaRingHeader.h:96
REC_OFF_GAIN_LIN: Final[int] = 0x001C       # f32  gain_lin        MetaRingHeader.h:97
REC_OFF_WIDTH_RAD: Final[int] = 0x0020      # f32  width_rad       MetaRingHeader.h:98
REC_OFF_FLAGS: Final[int] = 0x0024          # u32  flags           MetaRingHeader.h:99
REC_OFF_REC_SEQ: Final[int] = 0x0028        # u32  rec_seq         MetaRingHeader.h:100
REC_OFF_INTERP_LEN: Final[int] = 0x002C     # f32  interp_len_frames (A4 reserved, 0) MetaRingHeader.h:101
REC_OFF_JUMP_FLAG: Final[int] = 0x0030      # u32  jump_flag         (A4 reserved, 0) MetaRingHeader.h:102
REC_OFF_PAD: Final[int] = 0x0034            # u32[3] _pad           MetaRingHeader.h:103

# Contiguous single-shot record format — field order EXACTLY matches the offset
# table above (frame_index @0x00 … jump_flag @0x30, then 12-byte _pad to 64 B).
# Little-endian, NEVER native (the wire is LE, like RingHeader.h).
_REC_FORMAT: Final[str] = "<QIIfffffIIfI12x"
assert struct.calcsize(_REC_FORMAT) == RECORD_SIZE, "MetaRecord format must pack to 64 bytes"

# Explicit little-endian scalar formats (mirror ipc_sink.py).
_U64: Final[str] = "<Q"
_U32: Final[str] = "<I"


def record_byte_offset(index: int, record_capacity: int) -> int:
    """Byte offset of record slot `index` (mirror MetaRingHeader.h:194-198).

    Records begin immediately after the 4096-byte header; the ring is masked by
    record_capacity (a power of two), so `index & (record_capacity-1)` selects
    the slot.
    """
    slot = index & (record_capacity - 1)
    return META_HEADER_SIZE + slot * RECORD_SIZE


def total_meta_region_bytes(record_capacity: int) -> int:
    """Total shm region size for the config (mirror MetaRingHeader.h:201-204)."""
    return META_HEADER_SIZE + record_capacity * RECORD_SIZE


@dataclass
class MetaRecord:
    """A single fixed-64-byte metadata record (mirror MetaRingHeader.h:90-104).

    ``coord_mode`` selects the meaning of ``a0/a1/a2`` (polar az/el/dist vs
    Cartesian x/y/z). ``rec_seq`` is stamped by the sink at publish (a
    stream-monotonic record counter); the builder leaves it 0. ``frame_index``
    is authoritative on the sink at publish. ``interp_len_frames`` / ``jump_flag``
    are A4-reserved and MUST be 0 in A3.
    """

    frame_index: int
    obj_id: int
    coord_mode: int
    a0: float
    a1: float
    a2: float
    gain_lin: float
    width_rad: float
    flags: int
    rec_seq: int = 0
    interp_len_frames: float = 0.0
    jump_flag: int = 0


class MetaRingSink:
    """Producer side of the ADR 0029 metadata sidecar ring.

    Construct → `.publish(records, frame_index)` per audio block → `.close()`
    (or use as a context manager). Mirrors `IpcRingSink`'s lifecycle discipline
    but carries fixed-size `MetaRecord` PODs instead of planar PCM.
    """

    def __init__(
        self,
        path: str,
        *,
        sample_rate: int,
        slot_count: int,
        record_capacity: int = 1024,
        drain_dwell_s: float = 1.0,
        heartbeat_hz: float = 10.0,
    ) -> None:
        if sample_rate < 1:
            raise ValueError(f"sample_rate must be >= 1, got {sample_rate}")
        if slot_count < 1 or slot_count > 128:
            raise ValueError(f"slot_count must be in [1, 128], got {slot_count}")
        if record_capacity < 1:
            raise ValueError(f"record_capacity must be >= 1, got {record_capacity}")

        self._name = path
        self._sample_rate = int(sample_rate)
        self._slot_count = int(slot_count)
        self._drain_dwell_s = float(drain_dwell_s)
        self._heartbeat_hz = float(heartbeat_hz)

        # Producer pads record_capacity → next pow2 (the consumer masks by
        # capacity-1, so a non-pow2 ring would corrupt) and logs ONCE only when
        # padding actually occurred (mirror ipc_sink D5 p1).
        capacity = next_pow2(int(record_capacity))
        if capacity != int(record_capacity):
            _log.info(
                "record_capacity %d not a power of two; padded to %d",
                int(record_capacity),
                capacity,
            )
        self._capacity = capacity

        size = total_meta_region_bytes(self._capacity)
        # NOTE: never store self._shm.buf as an attribute — a retained memoryview
        # is an exporter that would make SharedMemory.close() raise BufferError.
        # Access self._shm.buf inline each time (as ipc_sink.py does).
        from multiprocessing import shared_memory  # noqa: PLC0415

        self._shm = shared_memory.SharedMemory(create=True, name=path, size=size)
        # SharedMemory create zero-fills, but re-zero explicitly so the header +
        # every record slot start clean regardless of platform quirk.
        self._shm.buf[:size] = b"\x00" * size

        # Read-only header fields (written once at construction).
        buf = self._shm.buf
        struct.pack_into(_U64, buf, OFF_MAGIC, SPE_META_MAGIC)
        struct.pack_into(_U32, buf, OFF_VERSION, META_HEADER_VERSION)
        struct.pack_into(_U32, buf, OFF_HEADER_SIZE, META_HEADER_SIZE)
        struct.pack_into(_U32, buf, OFF_SAMPLE_RATE, self._sample_rate)
        struct.pack_into(_U32, buf, OFF_SLOT_COUNT, self._slot_count)
        struct.pack_into(_U32, buf, OFF_RECORD_CAPACITY, self._capacity)
        struct.pack_into(_U32, buf, OFF_RECORD_SIZE, RECORD_SIZE)
        struct.pack_into(_U64, buf, OFF_WRITE_IDX, 0)
        struct.pack_into(_U64, buf, OFF_READ_IDX, 0)
        struct.pack_into(_U32, buf, OFF_PRODUCER_PID, os.getpid())
        struct.pack_into(_U64, buf, OFF_XRUN_COUNT, 0)
        struct.pack_into(_U32, buf, OFF_PRODUCER_STATE, STATE_IDLE)
        struct.pack_into(_U64, buf, OFF_SEQ, 0)
        struct.pack_into(_U64, buf, OFF_LAYOUT_HASH, META_LAYOUT_HASH)
        # First heartbeat stamped at construction (unix-epoch ms).
        struct.pack_into(_U64, buf, OFF_HEARTBEAT_MS, _heartbeat_ms())
        del buf  # drop the local memoryview before any close() can run.

        # Software shadows of the wire cursors (the wire is source of truth, but
        # we keep ints to avoid a round-trip read on every publish).
        self._write_idx = 0
        self._seq = 0
        self._xrun = 0
        self._rec_seq = 0  # stream-monotonic per-record counter (record.rec_seq)
        self._state = STATE_IDLE

        self._closed = False
        # Set when close() enters Draining(2) — lets a test observe Draining
        # without wall-clock polling (mirror ipc_sink AC-7b).
        self._draining_event = threading.Event()

        # 10 Hz daemon heartbeat timer (mirror ipc_sink): covers gaps so a live
        # producer is not falsely flagged stale. Stopped+joined FIRST in close().
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="MetaRingSink-heartbeat", daemon=True
        )
        self._hb_thread.start()

    # ── heartbeat ────────────────────────────────────────────────────────────

    def _stamp_heartbeat(self) -> None:
        struct.pack_into(_U64, self._shm.buf, OFF_HEARTBEAT_MS, _heartbeat_ms())

    def _heartbeat_loop(self) -> None:
        period = 1.0 / self._heartbeat_hz if self._heartbeat_hz > 0 else 0.1
        while not self._hb_stop.wait(period):
            if self._closed:
                break
            self._stamp_heartbeat()

    # ── publish path ───────────────────────────────────────────────────────────

    def publish(self, records: list[MetaRecord], frame_index: int) -> None:
        """Publish a batch of `records`, all bound to absolute PCM `frame_index`.

        Packs each record into its ring slot, then publishes `write_idx` LAST in
        strict program order (mirror ipc_sink's write_idx-last discipline).
        `frame_index` is stamped authoritatively into every record (the sink is
        the source of truth for frame binding). Applies the drop-newest xrun
        policy at BATCH granularity: if the ring cannot hold the whole batch it
        is dropped and `xrun_count` bumped — NEVER blocks (ADR §4.2).
        """
        if self._closed:
            raise RuntimeError("publish() on a closed MetaRingSink")

        n = len(records)
        if n == 0:
            return

        buf = self._shm.buf
        # Free-space / xrun check FIRST. read_idx is the consumer's progress;
        # free = capacity - (write_idx - read_idx) record slots.
        read_idx = struct.unpack_from(_U64, buf, OFF_READ_IDX)[0]
        free = self._capacity - (self._write_idx - read_idx)
        if free < n:
            # drop-newest: discard the INCOMING batch, bump xrun_count, and do
            # NOT advance write_idx (nothing already-published is overwritten).
            self._xrun += 1
            struct.pack_into(_U64, buf, OFF_XRUN_COUNT, self._xrun)
            return

        frame_index = int(frame_index)
        wi = self._write_idx
        for rec in records:
            base = record_byte_offset(wi, self._capacity)
            packed = struct.pack(
                _REC_FORMAT,
                frame_index,            # frame_index (authoritative)  0x00
                int(rec.obj_id),        # obj_id                       0x08
                int(rec.coord_mode),    # coord_mode                   0x0C
                float(rec.a0),          # a0                           0x10
                float(rec.a1),          # a1                           0x14
                float(rec.a2),          # a2                           0x18
                float(rec.gain_lin),    # gain_lin                     0x1C
                float(rec.width_rad),   # width_rad                    0x20
                int(rec.flags),         # flags (builder single-source) 0x24
                self._rec_seq,          # rec_seq (sink-stamped)       0x28
                0.0,                    # interp_len_frames (A4 reserved) 0x2C
                0,                      # jump_flag (A4 reserved)      0x30
            )
            buf[base : base + RECORD_SIZE] = packed  # overwrites the whole slot
            self._rec_seq += 1
            wi += 1

        # RELEASE half (A-14, mirror ipc_sink). Every record store above must be
        # globally visible BEFORE the write_idx store below — the consumer
        # acquire-loads write_idx (MetaRingConsumer.cpp:171/252/254/285) and then
        # reads those very records. This used to be `os.sched_yield()`, which is
        # a scheduler hint and NOT a barrier: correct only by accident on
        # x86-64 TSO, wrong on ARM64 (P-33). See `ipc_sink._resolve_fence`.
        _fence()

        # Publish write_idx LAST in strict program order.
        self._write_idx = wi
        struct.pack_into(_U64, buf, OFF_WRITE_IDX, self._write_idx)

        # Bump seq (published-batch counter) and beat the heartbeat.
        self._seq += 1
        struct.pack_into(_U64, buf, OFF_SEQ, self._seq)
        self._stamp_heartbeat()

        # Idle → Streaming on the first successful publish.
        if self._state == STATE_IDLE:
            self._set_state(STATE_STREAMING)

    # ── state ────────────────────────────────────────────────────────────────

    def _set_state(self, state: int) -> None:
        self._state = state
        # RELEASE fence: the consumer acquire-loads producer_state
        # (MetaRingConsumer.cpp:322) and treats Closed(3) as "everything the
        # producer ever wrote is now final" (mirror ipc_sink._set_state, P-33).
        _fence()
        struct.pack_into(_U32, self._shm.buf, OFF_PRODUCER_STATE, state)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Tear down the ring (idempotent), mirroring ipc_sink close() ordering.

        Order: stop+join the heartbeat timer → Draining(2) dwell → Closed(3) +
        final heartbeat → gc.collect() to release any transient exporters →
        SharedMemory.close() + .unlink(). This sink retains NO persistent buffer
        view (records are packed via transient struct.pack + slice assignment),
        so close() cannot raise BufferError; the gc.collect() keeps the same
        defensive discipline as ipc_sink.py (P4 / C2).
        """
        if self._closed:
            return
        self._closed = True

        # 1) Stop+join the heartbeat timer FIRST so no late beat overwrites the
        #    terminal state/heartbeat or races the unlink.
        self._hb_stop.set()
        if self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)

        # 2) Streaming/Idle → Draining(2), dwell >= one engine-tick → Closed(3).
        self._set_state(STATE_DRAINING)
        self._draining_event.set()
        if self._drain_dwell_s > 0:
            time.sleep(self._drain_dwell_s)
        self._set_state(STATE_CLOSED)
        self._stamp_heartbeat()

        # 3) Drop any transient exporters before SharedMemory.close() (C2).
        gc.collect()

        # 4) Producer owns the lifecycle: close THIS handle + unlink the region
        #    (the consumer NEVER unlinks).
        try:
            self._shm.close()
        finally:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> MetaRingSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
