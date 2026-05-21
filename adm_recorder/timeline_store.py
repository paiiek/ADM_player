from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class PositionEvent:
    """Sample-aligned position (1-based channel = OSC object id)."""

    channel_1based: int
    frame: int
    x: float
    y: float
    z: float


class TimelineStore:
    """Thread-safe OSC→frame log for axml blocks after recording."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[PositionEvent] = []

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

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

    def snapshot(self) -> list[PositionEvent]:
        with self._lock:
            return list(self._events)

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
