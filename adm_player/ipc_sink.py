"""ADR 0019 PR5 — Python PRODUCER for the spatial_engine shm PCM ring.

`class IpcRingSink` is a second, cross-LANGUAGE implementation of the FROZEN
C++ wire contract `core/src/audio_io/shm/RingHeader.h` (spatial_engine repo).
It creates a POSIX shared-memory region, writes the read-only header fields,
and de-interleaves the player's interleaved-2D float32 blocks into the PLANAR
ring the C++ consumer reads. Every header offset/type/endianness below is
transcribed byte-for-byte from `RingHeader.h` — a single wrong offset is a
silent cross-process corruption (ADR 0019 P1).

This module has NO sounddevice / device dependency by design: the `--sink
ipc://` path must run on a headless host (ADR 0019 PR5 PM10 / AC-11).
"""

from __future__ import annotations

import gc
import logging
import os
import struct
import threading
import time
from types import TracebackType
from typing import Final

import numpy as np
from multiprocessing import shared_memory

_log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# _HEADER — offset table mirroring core/src/audio_io/shm/RingHeader.h
#
# All fields little-endian (struct "<..."). The header is #pragma pack(1) so
# every field sits at exactly its documented offset (NO padding / NO 8-align of
# the unaligned 64-bit atomics). Transcribed directly from RingHeader.h; the
# inline `RingHeader.h:<line>` comment pins each field to its source line.
# ─────────────────────────────────────────────────────────────────────────────

# Constants (RingHeader.h:25-27)
SPE_RING_MAGIC: Final[int] = 0x53504543484D4E47  # "SPECHMNG" LE u64  (RingHeader.h:25)
RING_HEADER_SIZE: Final[int] = 4096              # kRingHeaderSize     (RingHeader.h:26)
RING_HEADER_VERSION: Final[int] = 1              # kRingHeaderVersion  (RingHeader.h:27)

# ProducerState enum (RingHeader.h:31-36)
STATE_IDLE: Final[int] = 0       # RingHeader.h:32
STATE_STREAMING: Final[int] = 1  # RingHeader.h:33
STATE_DRAINING: Final[int] = 2   # RingHeader.h:34
STATE_CLOSED: Final[int] = 3     # RingHeader.h:35

# Field byte offsets — ALL 15 fields (RingHeader.h:54-71 table / :79-94 struct).
OFF_MAGIC: Final[int] = 0x0000                       # u64  magic                       RingHeader.h:79
OFF_VERSION: Final[int] = 0x0008                     # u32  version                     RingHeader.h:80
OFF_HEADER_SIZE: Final[int] = 0x000C                 # u32  header_size                 RingHeader.h:81
OFF_SAMPLE_RATE: Final[int] = 0x0010                 # u32  sample_rate                 RingHeader.h:82
OFF_BLOCK_SIZE: Final[int] = 0x0014                  # u32  block_size                  RingHeader.h:83
OFF_CHANNELS: Final[int] = 0x0018                    # u32  channels                    RingHeader.h:84
OFF_CAPACITY_FRAMES: Final[int] = 0x001C             # u32  capacity_frames             RingHeader.h:85
OFF_WRITE_IDX: Final[int] = 0x0020                   # atomic<u64> write_idx            RingHeader.h:86
OFF_READ_IDX: Final[int] = 0x0028                    # atomic<u64> read_idx             RingHeader.h:87
OFF_PRODUCER_PID: Final[int] = 0x0030                # u32  producer_pid                RingHeader.h:88
# The three UNALIGNED hot-path atomics (4-mod-8 offsets) — do NOT 8-align them.
OFF_PRODUCER_HEARTBEAT_MS: Final[int] = 0x0034       # atomic<u64> producer_heartbeat_ms      RingHeader.h:89
OFF_XRUN_COUNT: Final[int] = 0x003C                  # atomic<u64> xrun_count                 RingHeader.h:90
OFF_PRODUCER_META_BLOCK_PTS_NS: Final[int] = 0x0044  # atomic<u64> producer_meta_block_pts_ns RingHeader.h:91
OFF_PRODUCER_STATE: Final[int] = 0x004C              # atomic<u32> producer_state       RingHeader.h:92
OFF_SEQ: Final[int] = 0x0050                         # atomic<u64> seq                  RingHeader.h:93
OFF_RESERVED: Final[int] = 0x0058                    # u8[0xFA8] _reserved (zero-init)  RingHeader.h:94

# Consumer-attach-lock word, carved from _reserved at 0x0058 (RingHeader.h:131).
# The producer zero-inits the whole _reserved span (→ lock reads 0 = "no
# consumer") and NEVER writes it again; the consumer CAS-locks it.
CONSUMER_LOCK_OFFSET: Final[int] = 0x0058  # kConsumerLockOffset  RingHeader.h:131

# Explicit little-endian struct formats (NEVER native — the wire is LE).
_U64: Final[str] = "<Q"
_U32: Final[str] = "<I"

_BYTES_PER_SAMPLE: Final[int] = 4  # float32, sizeof(float) (RingHeader.h:152)


def channel_byte_offset(channel: int, capacity_frames: int) -> int:
    """Byte offset of channel c's planar ring data (mirror RingHeader.h:150-153)."""
    return RING_HEADER_SIZE + channel * capacity_frames * _BYTES_PER_SAMPLE


def total_region_bytes(channels: int, capacity_frames: int) -> int:
    """Total shm region size for the config (mirror RingHeader.h:156-159)."""
    return RING_HEADER_SIZE + channels * capacity_frames * _BYTES_PER_SAMPLE


def next_pow2(n: int) -> int:
    """Smallest power-of-two >= n (n >= 1)."""
    if n < 1:
        raise ValueError(f"ring_frames must be >= 1, got {n}")
    return 1 << (n - 1).bit_length()


def _heartbeat_ms() -> int:
    """Unix-epoch milliseconds (ADR 0019 PR4-Q1 / D2 h1).

    MUST be wall-clock unix-epoch ms, NOT a steady/monotonic clock: the C++
    consumer's stale detector compares `now_unix_ms - producer_heartbeat_ms`
    (RingHeader/ADR §2.3). A monotonic value would be off by the unix epoch
    (~1.7e12 ms) and trip stale forever.
    """
    return int(time.time() * 1000)


class IpcRingSink:
    """Producer side of the ADR 0019 shm PCM ring.

    Construct → `.write(block)` per audio block → `.close()` (or use as a
    context manager). The interleaved-2D `(frames, channels)` float32 block the
    player already produces is de-interleaved into the planar ring on `write()`.
    """

    def __init__(
        self,
        path: str,
        *,
        sample_rate: int,
        channels: int,
        block_size: int,
        ring_frames: int,
        drain_dwell_s: float = 1.0,
        heartbeat_hz: float = 10.0,
    ) -> None:
        if channels < 1:
            raise ValueError(f"channels must be >= 1, got {channels}")
        if block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {block_size}")
        if sample_rate < 1:
            raise ValueError(f"sample_rate must be >= 1, got {sample_rate}")

        self._name = path
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._block_size = int(block_size)
        self._drain_dwell_s = float(drain_dwell_s)
        self._heartbeat_hz = float(heartbeat_hz)

        # D5 p1: producer pads ring_frames → next pow2 (consumer rejects non-pow2
        # at attach, PR2-Q4) and logs ONCE only when padding actually occurred.
        capacity = next_pow2(int(ring_frames))
        if capacity != int(ring_frames):
            _log.info("ring_frames %d not a power of two; padded to %d", int(ring_frames), capacity)
        self._capacity = capacity

        size = total_region_bytes(self._channels, self._capacity)
        self._shm = shared_memory.SharedMemory(create=True, name=path, size=size)

        # SharedMemory create zero-fills, but re-zero the whole region explicitly
        # so the header + _reserved (incl. the consumer-lock word) start clean
        # regardless of any platform quirk (PM5).
        self._shm.buf[:size] = b"\x00" * size

        # Read-only header fields (written once at construction).
        buf = self._shm.buf
        struct.pack_into(_U64, buf, OFF_MAGIC, SPE_RING_MAGIC)
        struct.pack_into(_U32, buf, OFF_VERSION, RING_HEADER_VERSION)
        struct.pack_into(_U32, buf, OFF_HEADER_SIZE, RING_HEADER_SIZE)
        struct.pack_into(_U32, buf, OFF_SAMPLE_RATE, self._sample_rate)
        struct.pack_into(_U32, buf, OFF_BLOCK_SIZE, self._block_size)
        struct.pack_into(_U32, buf, OFF_CHANNELS, self._channels)
        struct.pack_into(_U32, buf, OFF_CAPACITY_FRAMES, self._capacity)
        struct.pack_into(_U64, buf, OFF_WRITE_IDX, 0)
        struct.pack_into(_U64, buf, OFF_READ_IDX, 0)
        struct.pack_into(_U32, buf, OFF_PRODUCER_PID, os.getpid())
        struct.pack_into(_U64, buf, OFF_XRUN_COUNT, 0)
        struct.pack_into(_U64, buf, OFF_PRODUCER_META_BLOCK_PTS_NS, 0)
        struct.pack_into(_U32, buf, OFF_PRODUCER_STATE, STATE_IDLE)
        struct.pack_into(_U64, buf, OFF_SEQ, 0)
        # First heartbeat stamped at construction (unix-epoch ms).
        struct.pack_into(_U64, buf, OFF_PRODUCER_HEARTBEAT_MS, _heartbeat_ms())

        # Per-channel planar ring views over shm.buf with EXPLICIT little-endian
        # dtype (np.dtype("<f4"), NOT native float32 — the wire is LE, D4). These
        # are persistent buffer EXPORTERS of shm.buf and MUST be dropped +
        # gc.collect()-ed before SharedMemory.close() (P4 / C2 / AC-13).
        self._le_f4 = np.dtype("<f4")
        self._ring: list[np.ndarray] = []
        for c in range(self._channels):
            off = channel_byte_offset(c, self._capacity)
            view = np.frombuffer(buf, dtype=self._le_f4, count=self._capacity, offset=off)
            self._ring.append(view)

        # Software shadow of write_idx/seq/xrun (the wire is the source of truth,
        # but we keep ints to avoid a round-trip read on every write).
        self._write_idx = 0
        self._seq = 0
        self._xrun = 0
        self._state = STATE_IDLE

        self._closed = False
        # Set when close() enters Draining(2) — lets a test deterministically
        # observe the Draining state without wall-clock polling (AC-7b).
        self._draining_event = threading.Event()

        # 10 Hz daemon heartbeat timer: covers gaps (pause / between-track /
        # slow disk) so a live producer is not falsely flagged stale by the
        # consumer's tight 100 ms threshold. Mirrors the engine's 10 Hz
        # HeartbeatPublisher (ADR §6). Stopped+joined FIRST in close().
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="IpcRingSink-heartbeat", daemon=True
        )
        self._hb_thread.start()

    # ── heartbeat ────────────────────────────────────────────────────────────

    def _stamp_heartbeat(self) -> None:
        struct.pack_into(_U64, self._shm.buf, OFF_PRODUCER_HEARTBEAT_MS, _heartbeat_ms())

    def _heartbeat_loop(self) -> None:
        period = 1.0 / self._heartbeat_hz if self._heartbeat_hz > 0 else 0.1
        while not self._hb_stop.wait(period):
            if self._closed:
                break
            self._stamp_heartbeat()

    # ── write path ─────────────────────────────────────────────────────────────

    def write(self, block: np.ndarray) -> None:
        """Publish one interleaved-2D float32 block `(frames, channels)`.

        De-interleaves to the planar ring; applies the drop-newest (drop the
        INCOMING block) xrun policy when the ring is full; publishes `write_idx`
        LAST in strict program order. NEVER blocks the audio loop (ADR §4.2).
        """
        if self._closed:
            raise RuntimeError("write() on a closed IpcRingSink")

        frames = int(block.shape[0])
        if frames == 0:
            return
        if block.ndim != 2 or block.shape[1] != self._channels:
            raise ValueError(
                f"block must be (frames, {self._channels}); got shape {block.shape}"
            )

        buf = self._shm.buf
        # Free-space / xrun check FIRST (ADR §4.2). read_idx is the consumer's
        # progress (load); free = capacity - (write_idx - read_idx).
        read_idx = struct.unpack_from(_U64, buf, OFF_READ_IDX)[0]
        free = self._capacity - (self._write_idx - read_idx)
        if free < frames:
            # drop-newest: discard the INCOMING block, bump xrun_count, and do
            # NOT advance write_idx (so nothing already-published is overwritten).
            # Never block / spin / sleep (P2).
            self._xrun += 1
            struct.pack_into(_U64, buf, OFF_XRUN_COUNT, self._xrun)
            return

        cap = self._capacity
        mask = cap - 1  # capacity is pow2
        base = self._write_idx & mask

        # (1) Copy the channel samples first — de-interleave per channel into the
        # planar ring with a <=2-part wrap-split at the i&(cap-1) boundary (D4 d1/w1).
        first = min(frames, cap - base)
        rest = frames - first
        for c in range(self._channels):
            col = block[:, c]
            ring = self._ring[c]
            ring[base : base + first] = col[:first]
            if rest:
                ring[0:rest] = col[first:]

        # (2) Stamp the per-block presentation timestamp (CLOCK_MONOTONIC ns).
        struct.pack_into(_U64, buf, OFF_PRODUCER_META_BLOCK_PTS_NS, time.monotonic_ns())

        # Best-effort scheduler nudge — NOT a memory fence. On x86-64 (TSO) the
        # prior sample/pts stores are visible-before the write_idx store below;
        # ARM64 weak-memory ordering is PR6's concurrent-soak proof (P3).
        os.sched_yield()

        # (3) Publish write_idx LAST in strict program order (D-publish).
        self._write_idx += frames
        struct.pack_into(_U64, buf, OFF_WRITE_IDX, self._write_idx)

        # Bump seq (block counter) and beat the heartbeat.
        self._seq += 1
        struct.pack_into(_U64, buf, OFF_SEQ, self._seq)
        self._stamp_heartbeat()

        # Idle → Streaming on the first successful write (D2 state machine).
        if self._state == STATE_IDLE:
            self._set_state(STATE_STREAMING)

    # ── state ────────────────────────────────────────────────────────────────

    def _set_state(self, state: int) -> None:
        self._state = state
        struct.pack_into(_U32, self._shm.buf, OFF_PRODUCER_STATE, state)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Tear down the ring (idempotent).

        Order (the CRITICAL C2 fix): stop+join the heartbeat timer → write the
        terminal Closed(3) state + final heartbeat → drop every retained
        shm.buf view + gc.collect() → SharedMemory.close() + .unlink(). Dropping
        the views BEFORE close() is mandatory, else close() raises
        `BufferError: cannot close exported pointers exist` and leaks the shm
        object (reproduced on Python 3.12.x).
        """
        if self._closed:
            return
        self._closed = True

        # 1) Stop+join the heartbeat timer FIRST so no late beat overwrites the
        #    terminal state/heartbeat or races the unlink.
        self._hb_stop.set()
        if self._hb_thread.is_alive():
            self._hb_thread.join(timeout=2.0)

        # 2) Streaming/Idle → Draining(2), dwell >= one engine-tick (drain_dwell_s,
        #    default 1.0 so the engine's 1 Hz telemetry tick can sample Draining,
        #    PR4-Q6) → Closed(3) + final heartbeat.
        self._set_state(STATE_DRAINING)
        self._draining_event.set()
        if self._drain_dwell_s > 0:
            time.sleep(self._drain_dwell_s)
        self._set_state(STATE_CLOSED)
        self._stamp_heartbeat()

        # 3) Drop every retained buffer view + gc.collect() to release the
        #    shm.buf exporters BEFORE SharedMemory.close() (C2 / AC-13).
        self._ring = []
        gc.collect()

        # 4) Producer owns the lifecycle: close THIS handle + unlink the region
        #    (the consumer NEVER unlinks — verified SharedMemoryRegion.cpp:155).
        try:
            self._shm.close()
        finally:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> IpcRingSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
