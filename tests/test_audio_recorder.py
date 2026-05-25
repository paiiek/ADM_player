"""AudioCapture._callback must never let an overflow or write failure pass silently."""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from adm_recorder.audio_recorder import AudioCapture


class _FakeSF:
    """Stand-in for soundfile.SoundFile so the callback can run without a stream."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.writes = 0
        self._raise = raise_exc

    def write(self, arr: np.ndarray) -> None:
        if self._raise is not None:
            raise self._raise
        self.writes += 1


def _make_capture() -> AudioCapture:
    # 2-in → 2-out identity route; device=None and no start() means no real I/O.
    return AudioCapture(
        device=None,
        samplerate=48000,
        device_channels=2,
        out_channels=2,
        out_wav=Path("/tmp/_audio_recorder_unused.wav"),
        route=np.eye(2, dtype=np.float32),
    )


class TestAudioCaptureCallback(unittest.TestCase):
    def setUp(self) -> None:
        self.indata = np.zeros((128, 2), dtype=np.float32)

    def test_normal_write_advances_frames(self) -> None:
        cap = _make_capture()
        cap._sf = _FakeSF()
        cap._callback(self.indata, 128, None, 0)  # status falsy = healthy
        self.assertEqual(cap.current_frame(), 128)
        self.assertIsNone(cap.last_error)
        self.assertEqual(cap.overflow_count, 0)

    def test_status_flag_counts_overflow(self) -> None:
        cap = _make_capture()
        cap._sf = _FakeSF()
        cap._callback(self.indata, 128, None, "input overflow")  # truthy status
        self.assertEqual(cap.overflow_count, 1)
        # Overflow is reported, not fatal — the frames we did get are still written.
        self.assertEqual(cap.current_frame(), 128)
        self.assertIsNone(cap.last_error)

    def test_write_error_is_latched_not_raised(self) -> None:
        cap = _make_capture()
        cap._sf = _FakeSF(raise_exc=OSError("No space left on device"))
        # Must NOT raise: a raise here would tear down the PortAudio stream
        # silently and truncate the recording.
        cap._callback(self.indata, 128, None, 0)
        self.assertIsNotNone(cap.last_error)
        self.assertIn("OSError", cap.last_error or "")
        # A failed write must not advance the frame counter.
        self.assertEqual(cap.current_frame(), 0)


if __name__ == "__main__":
    unittest.main()
