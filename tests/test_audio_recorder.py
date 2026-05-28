"""AudioCapture._callback must never let an overflow or write failure pass silently."""
from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import numpy as np

from adm_recorder.audio_recorder import AudioCapture


class _FakeSF:
    """Stand-in for soundfile.SoundFile so the callback can run without a stream."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.writes = 0
        self.flushes = 0
        self.closed = False
        self._raise = raise_exc

    def write(self, arr: np.ndarray) -> None:
        if self._raise is not None:
            raise self._raise
        self.writes += 1

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed = True


def _make_capture(
    *,
    on_levels: Callable[[list[float]], None] | None = None,
    on_capture_error: Callable[[str], None] | None = None,
    out_wav: Path | None = None,
) -> AudioCapture:
    # 2-in → 2-out identity route; device=None and no start() means no real I/O.
    return AudioCapture(
        device=None,
        samplerate=48000,
        device_channels=2,
        out_channels=2,
        out_wav=out_wav if out_wav is not None else Path("/tmp/_audio_recorder_unused.wav"),
        route=np.eye(2, dtype=np.float32),
        on_levels=on_levels,
        on_capture_error=on_capture_error,
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

    def test_per_block_flush_runs(self) -> None:
        """Per-block flush bounds silent loss on crash to ~1 block."""
        cap = _make_capture()
        fake = _FakeSF()
        cap._sf = fake
        cap._callback(self.indata, 128, None, 0)
        cap._callback(self.indata, 128, None, 0)
        self.assertEqual(fake.writes, 2)
        # write() must be paired with flush() so libsndfile pushes the block to
        # the OS page cache immediately; otherwise SIGINT loses N internal blocks.
        self.assertEqual(fake.flushes, 2)

    def test_on_levels_exception_is_logged_not_swallowed(self) -> None:
        """A broken levels sink (Qt slot raised, widget closed) must NOT silently
        stop levels delivery — the warning is logged, capture continues, frame
        counter still advances."""
        calls: list[list[float]] = []

        def boom(peaks: list[float]) -> None:
            calls.append(peaks)
            raise RuntimeError("levels widget closed")

        cap = _make_capture(on_levels=boom)
        cap._sf = _FakeSF()
        # If on_levels raised through, this would propagate and tear down the
        # PortAudio thread. It must not.
        cap._callback(self.indata, 128, None, 0)
        self.assertEqual(len(calls), 1)
        # Capture path must still have advanced.
        self.assertEqual(cap.current_frame(), 128)

    def test_on_capture_error_fires_on_write_failure(self) -> None:
        errors: list[str] = []
        cap = _make_capture(on_capture_error=errors.append)
        cap._sf = _FakeSF(raise_exc=OSError("disk full"))
        cap._callback(self.indata, 128, None, 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("OSError", errors[0])
        # And last_error stays latched so polling callers also see it.
        self.assertIn("OSError", cap.last_error or "")

    def test_on_capture_error_failure_does_not_break_capture(self) -> None:
        """If the GUI's error sink itself raises, capture must still survive."""
        def cb(_: str) -> None:
            raise RuntimeError("GUI closed mid-error")

        cap = _make_capture(on_capture_error=cb)
        cap._sf = _FakeSF(raise_exc=OSError("disk full"))
        # No raise out of the callback.
        cap._callback(self.indata, 128, None, 0)
        self.assertIn("OSError", cap.last_error or "")

    def test_stop_flushes_closes_and_fsyncs_without_raise(self) -> None:
        """stop() must flush, close, and fsync (best-effort) without raising —
        even if the fake SoundFile didn't write anything real."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "rec.wav"
            # Touch the file so fsync has something to open. Empty file is fine.
            out.write_bytes(b"")
            cap = _make_capture(out_wav=out)
            fake = _FakeSF()
            cap._sf = fake
            cap.stop()
            self.assertTrue(fake.closed)
            self.assertGreaterEqual(fake.flushes, 1)
            # Idempotent: second stop is a no-op.
            cap.stop()


if __name__ == "__main__":
    unittest.main()
