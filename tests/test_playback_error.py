"""_run_playback_loop must NOT raise when the audio sink dies mid-stream.

Output device unplug (USB pull, JACK quit) surfaces in sounddevice as a
RuntimeError out of OutputStream.write. We swallow it, surface via on_error,
and let the outer `with` close the stream cleanly so the worker thread can
reach its finalizer.

We test the loop directly with a FakeSoundFile so the test is independent of
the installed `soundfile` version's exposure of `SoundFile.frames`.
"""
from __future__ import annotations

import unittest

import numpy as np

from adm_player.playback import _run_playback_loop


class _FakeSoundFile:
    """Minimal SoundFile stand-in: stream zeros for N blocks, then EOF."""

    def __init__(self, blocks: int, frames_per_block: int, channels: int) -> None:
        self._remaining_blocks = int(blocks)
        self._fpb = int(frames_per_block)
        self._ch = int(channels)

    def read(self, block_frames: int, dtype: str = "float32", always_2d: bool = True):
        if self._remaining_blocks <= 0:
            return np.zeros((0, self._ch), dtype=np.float32)
        self._remaining_blocks -= 1
        return np.zeros((min(block_frames, self._fpb), self._ch), dtype=np.float32)

    def seek(self, frame: int) -> None:  # noqa: D401 — not exercised here
        pass


class _ExplodingSink:
    """Raises on the N-th write to simulate device unplug."""

    def __init__(self, explode_on: int = 2, exc: BaseException | None = None) -> None:
        self.writes = 0
        self.entered = False
        self.exited = False
        self._explode_on = explode_on
        self._exc = exc if exc is not None else RuntimeError("PortAudio: Device unavailable")

    def __enter__(self):  # noqa: D401
        self.entered = True
        return self

    def __exit__(self, *exc):  # noqa: D401
        self.exited = True
        return False

    def write(self, data: np.ndarray) -> None:
        self.writes += 1
        if self.writes == self._explode_on:
            raise self._exc


def _run(sink: _ExplodingSink, *, on_error=None, blocks: int = 8) -> None:
    fake = _FakeSoundFile(blocks=blocks, frames_per_block=512, channels=2)
    # The caller (play_adm_wav) is the one that opens `with sink:`; here we
    # do the same so we exercise the same teardown contract.
    with sink:
        _run_playback_loop(
            f=fake,
            sink=sink,
            sr=48000,
            write_ch=2,
            objects=[],
            osc=None,
            block_frames=512,
            total_frames=blocks * 512,
            stop_event=None,
            pause_event=None,
            start_frame=0,
            on_progress=None,
            on_levels=None,
            channel_mix=None,
            progress_emit_interval_s=None,
            levels_emit_interval_s=None,
            on_error=on_error,
        )


class TestPlaybackError(unittest.TestCase):
    def test_sink_write_runtime_error_does_not_propagate(self) -> None:
        errors: list[str] = []
        sink = _ExplodingSink(explode_on=2)
        # Must not raise — testing the swallow contract.
        _run(sink, on_error=errors.append)
        # Outer `with sink:` closed it — guarantees PortAudio/IPC teardown.
        self.assertTrue(sink.exited)
        # The 2nd write raised; we expect exactly 2 attempts.
        self.assertEqual(sink.writes, 2)
        # on_error fired exactly once with the cause.
        self.assertEqual(len(errors), 1)
        self.assertIn("RuntimeError", errors[0])
        self.assertIn("Device unavailable", errors[0])

    def test_sink_write_os_error_also_caught(self) -> None:
        """OSError is the IPC ring producer's teardown signal."""
        errors: list[str] = []
        sink = _ExplodingSink(explode_on=1, exc=OSError("ring closed"))
        _run(sink, on_error=errors.append)
        self.assertTrue(sink.exited)
        self.assertEqual(len(errors), 1)
        self.assertIn("OSError", errors[0])

    def test_sink_write_error_without_callback_still_clean(self) -> None:
        """Even without on_error wired, the loop ends gracefully."""
        sink = _ExplodingSink(explode_on=1)
        _run(sink, on_error=None)
        self.assertTrue(sink.exited)
        # No exception propagated; loop exited; outer `with` closed sink.

    def test_on_error_callback_failure_does_not_propagate(self) -> None:
        """If on_error itself raises, the loop must still close cleanly."""
        sink = _ExplodingSink(explode_on=1)

        def boom(_: str) -> None:
            raise RuntimeError("error sink also broke")

        _run(sink, on_error=boom)
        self.assertTrue(sink.exited)


if __name__ == "__main__":
    unittest.main()
