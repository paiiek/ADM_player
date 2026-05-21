"""Limit OSC position record rate; store real frame indices (no grid snap) for smooth ADM automation."""

from __future__ import annotations

from collections.abc import Callable

from .timeline_store import TimelineStore

RECORD_SAMPLE_RATE = 48_000
# ~480 Hz @ 48 k → ~100 samples between points (~2.1 ms). Higher = smoother Nuendo curves, larger metadata.
OSC_POSITION_RECORD_HZ = 480


def min_samples_between_stores(
    sample_rate: int = RECORD_SAMPLE_RATE,
    hz: int = OSC_POSITION_RECORD_HZ,
) -> int:
    return max(1, int(sample_rate // hz))


def make_position_callback(
    timeline: TimelineStore,
    *,
    sample_rate: int = RECORD_SAMPLE_RATE,
    hz: int = OSC_POSITION_RECORD_HZ,
) -> Callable[[int, int, float, float, float], None]:
    """
    At most one stored point per channel per ``sample_rate // hz`` samples.
    Uses the **actual** audio frame from the callback (not snapped to a tick grid), so automation
    timing follows OSC motion without artificial stair-stepping on the time axis.
    """
    min_gap = min_samples_between_stores(sample_rate, hz)
    last_frame: dict[int, int] = {}

    def on_xyz(ch: int, frame: int, x: float, y: float, z: float) -> None:
        f = max(0, int(frame))
        prev = last_frame.get(ch)
        if prev is None:
            last_frame[ch] = f
            timeline.add_cartesian(ch, f, x, y, z)
            return
        if f - prev >= min_gap:
            last_frame[ch] = f
            timeline.add_cartesian(ch, f, x, y, z)

    return on_xyz
