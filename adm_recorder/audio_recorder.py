from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

_log = logging.getLogger(__name__)

LevelsCallback = Callable[[list[float]], None]
CaptureErrorCallback = Callable[[str], None]


class AudioCapture:
    """Multichannel input stream, routing matrix, float32 WAV streaming write."""

    def __init__(
        self,
        device: int | str | None,
        samplerate: int,
        device_channels: int,
        out_channels: int,
        out_wav: Path,
        route: np.ndarray,
        *,
        on_levels: LevelsCallback | None = None,
        on_capture_error: CaptureErrorCallback | None = None,
    ) -> None:
        self.device = device
        self.samplerate = int(samplerate)
        self.device_channels = int(device_channels)
        self.channels = int(out_channels)
        self.out_wav = Path(out_wav)
        self._route = np.asarray(route, dtype=np.float32)
        if self._route.shape != (self.device_channels, self.channels):
            raise ValueError(
                f"route shape {self._route.shape} != "
                f"({self.device_channels}, {self.channels})"
            )
        self._on_levels = on_levels
        # PortAudio invokes this from the capture thread; the GUI must marshal
        # to its event loop (Qt queued signal) to stay thread-safe.
        self._on_capture_error = on_capture_error
        self._lock = threading.Lock()
        self._frames_done = 0
        self._overflow_count = 0
        self._write_errors = 0
        self._last_error: str | None = None
        self._sf: sf.SoundFile | None = None
        self._stream: sd.InputStream | None = None

    def current_frame(self) -> int:
        with self._lock:
            return self._frames_done

    @property
    def overflow_count(self) -> int:
        """Number of callbacks PortAudio flagged (input overflow etc.).

        Non-zero means the OS dropped capture frames before they reached us;
        the resulting WAV has a silent/missing gap that is otherwise invisible.
        """
        with self._lock:
            return self._overflow_count

    @property
    def last_error(self) -> str | None:
        """Last disk-write failure ("Type: message"), or None if writes are OK.

        Set when ``SoundFile.write`` raises inside the audio callback (e.g.
        disk full). Surfacing it lets the caller stop and warn instead of
        silently producing a truncated recording.
        """
        with self._lock:
            return self._last_error

    def _callback(self, indata: np.ndarray, frames: int, _time, status) -> None:
        # indata: (frames, device_channels) → (frames, out_channels)
        if status:
            # PortAudio dropped/glitched frames (input overflow, etc.). The lost
            # audio never reaches us, so record the event rather than swallowing
            # it — otherwise the WAV has an invisible silent gap.
            with self._lock:
                self._overflow_count += 1
            _log.warning("audio capture status flagged: %s (frames=%d)", status, frames)
        out = indata @ self._route if indata.size > 0 else indata
        if self._on_levels is not None and out.size > 0:
            peaks = np.max(np.abs(out), axis=0).astype(float).tolist()
            try:
                self._on_levels(peaks)
            except Exception as exc:  # noqa: BLE001 — levels are best-effort UI
                # Don't silently swallow: a broken levels sink (Qt slot raised,
                # closed widget, etc.) is worth a log line, but it must not
                # break capture itself.
                _log.warning("on_levels callback raised: %s", exc)
        if self._sf is None:
            return
        try:
            self._sf.write(np.asarray(out, copy=True))
            # Per-block flush bounds silent loss on crash to ~1 block. The
            # OS page cache still buffers; stop() pairs this with os.fsync().
            self._sf.flush()
        except Exception as exc:  # noqa: BLE001 — never let the audio callback die silently
            # A raise here would tear down the PortAudio stream with no trace,
            # leaving a truncated file. Latch the error so stop()/callers can
            # surface it; keep the stream alive so levels metering still works.
            with self._lock:
                self._write_errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                err_msg = self._last_error
            _log.error("audio capture write failed: %s", exc)
            cb = self._on_capture_error
            if cb is not None:
                try:
                    cb(err_msg)
                except Exception as cb_exc:  # noqa: BLE001 — keep capture alive
                    _log.warning("on_capture_error callback raised: %s", cb_exc)
            return
        with self._lock:
            self._frames_done += int(frames)

    def start(self) -> None:
        self.stop()
        self._frames_done = 0
        self.out_wav.parent.mkdir(parents=True, exist_ok=True)
        self._sf = sf.SoundFile(
            str(self.out_wav),
            mode="w",
            samplerate=self.samplerate,
            channels=self.channels,
            subtype="FLOAT",
            format="WAV",
        )
        self._stream = sd.InputStream(
            device=self.device,
            channels=self.device_channels,
            samplerate=self.samplerate,
            dtype="float32",
            callback=self._callback,
            blocksize=512,
            latency="high",
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # noqa: BLE001 — must always release
                _log.warning("audio capture stream close failed: %s", exc)
            self._stream = None
        if self._sf is not None:
            try:
                self._sf.flush()
            except Exception as exc:  # noqa: BLE001
                _log.warning("audio capture flush failed: %s", exc)
            try:
                self._sf.close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("audio capture close failed: %s", exc)
            self._sf = None
            # Push the WAV out of the OS page cache so SIGKILL / power loss
            # after stop() doesn't strand the last blocks. Best-effort: an
            # unwritable / missing path just logs.
            try:
                fd = os.open(str(self.out_wav), os.O_RDWR)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                _log.warning("audio capture fsync failed: %s", exc)
