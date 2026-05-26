from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class PositionEvent:
    """Sample-aligned position (1-based channel = OSC object id)."""

    channel_1based: int
    frame: int
    x: float
    y: float
    z: float


@dataclass
class MetaEvent:
    """Sample-aligned non-positional ADM field echoed by the engine (M5.2).

    ``kind`` is one of ``gain``/``mute``/``active``/``width``/``name``; ``value``
    is forwarded raw (float for gain/width, int for mute/active, str for name).
    The axml writer (M5.3) maps these onto ``audioBlockFormat`` — the store only
    records them so no engine-side trajectory is lost.
    """

    channel_1based: int
    frame: int
    kind: str
    value: Any


@dataclass
class ObjectBlock:
    """A position block enriched with the gain/width in effect at its start (M5.3).

    ``gain``/``width`` are ``None`` when no such :class:`MetaEvent` preceded the
    block — the axml writer then omits the element, so position-only output stays
    byte-identical to the pre-M5.3 behaviour. ``mute`` is folded into ``gain``
    (muted → ``0.0``) by :func:`events_to_object_blocks`, so the writer never has
    to reason about mute.
    """

    start_frame: int
    end_frame: int
    x: float
    y: float
    z: float
    gain: float | None = None
    width: float | None = None


class TimelineStore:
    """Thread-safe OSC→frame log for axml blocks after recording."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[PositionEvent] = []
        self._meta: list[MetaEvent] = []

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._meta.clear()

    def add_cartesian(
        self,
        channel_1based: int,
        frame: int,
        x: float,
        y: float,
        z: float,
    ) -> None:
        with self._lock:
            self._events.append(
                PositionEvent(int(channel_1based), int(frame), float(x), float(y), float(z))
            )

    def add_meta(
        self,
        channel_1based: int,
        frame: int,
        kind: str,
        value: Any,
    ) -> None:
        with self._lock:
            self._meta.append(MetaEvent(int(channel_1based), int(frame), str(kind), value))

    def snapshot(self) -> list[PositionEvent]:
        with self._lock:
            return list(self._events)

    def snapshot_meta(self) -> list[MetaEvent]:
        with self._lock:
            return list(self._meta)

def events_to_blocks_per_channel(
    events: list[PositionEvent],
    total_frames: int,
    sample_rate: float,
) -> dict[int, list[tuple[int, int, float, float, float]]]:
    """
    Per channel: (start_frame, end_frame, x, y, z) blocks; end is before next event or total_frames.
    """
    by_ch: dict[int, list[PositionEvent]] = {}
    for e in sorted(events, key=lambda x: (x.channel_1based, x.frame)):
        by_ch.setdefault(e.channel_1based, []).append(e)

    out: dict[int, list[tuple[int, int, float, float, float]]] = {}
    for ch, evs in by_ch.items():
        blocks: list[tuple[int, int, float, float, float]] = []
        for i, ev in enumerate(evs):
            end_f = evs[i + 1].frame if i + 1 < len(evs) else total_frames
            if end_f <= ev.frame:
                continue
            blocks.append((ev.frame, end_f, ev.x, ev.y, ev.z))
        if not blocks and total_frames > 0:
            # No events: caller may supply defaults
            pass
        out[ch] = blocks
    return out


def _meta_state_at(
    ch_metas: list[MetaEvent], frame: int
) -> tuple[float | None, float | None]:
    """Resolve the (gain, width) effective at ``frame`` from a channel's metas.

    ``ch_metas`` must be ordered by frame. The returned gain already reflects
    mute: while muted the effective gain is ``0.0``; an unmute reveals the last
    explicit ``/gain`` again (``None`` if none was ever sent, so the writer omits
    the element). ``active`` is ignored here — it is a block-boundary hint for a
    later milestone, not a rendered field.
    """
    cur_gain: float | None = None
    cur_width: float | None = None
    muted = False
    for m in ch_metas:
        if m.frame > frame:
            break
        if m.kind == "gain":
            cur_gain = float(m.value)
        elif m.kind == "width":
            cur_width = float(m.value)
        elif m.kind == "mute":
            muted = bool(int(m.value))
    return (0.0 if muted else cur_gain), cur_width


def events_to_object_blocks(
    events: list[PositionEvent],
    metas: list[MetaEvent],
    total_frames: int,
    sample_rate: float,
) -> tuple[dict[int, list[ObjectBlock]], dict[int, str]]:
    """Position blocks enriched with the gain/width effective at each block start,
    plus the last-seen object name per channel.

    Blocks are still cut on **position** changes (same boundaries as
    :func:`events_to_blocks_per_channel`); the gain/width step functions are then
    sampled at each block's start frame. ``mute`` becomes gain ``0.0`` for the
    muted span, and ``name`` collapses to its last value. Returns
    ``(blocks_by_channel, names_by_channel)``.
    """
    pos_blocks = events_to_blocks_per_channel(events, total_frames, sample_rate)

    metas_by_ch: dict[int, list[MetaEvent]] = {}
    names_by_ch: dict[int, str] = {}
    for m in sorted(metas, key=lambda e: (e.channel_1based, e.frame)):
        if m.kind == "name":
            names_by_ch[m.channel_1based] = str(m.value)  # last wins
        else:
            metas_by_ch.setdefault(m.channel_1based, []).append(m)

    out: dict[int, list[ObjectBlock]] = {}
    for ch, blocks in pos_blocks.items():
        ch_metas = metas_by_ch.get(ch, [])
        out[ch] = [
            ObjectBlock(s, e, x, y, z, *_meta_state_at(ch_metas, s))
            for (s, e, x, y, z) in blocks
        ]
    return out, names_by_ch


def frames_to_smpte_timecode(frame: int, sample_rate: float) -> str:
    """ADM audioBlockFormat rtime/duration string (HH:MM:SS.ffffff)."""
    if sample_rate <= 0:
        sample_rate = 48000.0
    t = frame / float(sample_rate)
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:012.9f}"
