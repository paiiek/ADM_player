from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

LevelsCallback = Callable[[list[float]], None]


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
        self._lock = threading.Lock()
        self._frames_done = 0
        self._sf: sf.SoundFile | None = None
        self._stream: sd.InputStream | None = None

    def current_frame(self) -> int:
        with self._lock:
            return self._frames_done

    def _callback(self, indata: np.ndarray, frames: int, _time, status) -> None:
        # indata: (frames, device_channels) → (frames, out_channels)
        out = indata @ self._route if indata.size > 0 else indata
        if self._on_levels is not None and out.size > 0:
            peaks = np.max(np.abs(out), axis=0).astype(float).tolist()
            try:
                self._on_levels(peaks)
            except Exception:
                pass
        if self._sf is None:
            return
        self._sf.write(np.asarray(out, copy=True))
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
            except Exception:
                pass
            self._stream = None
        if self._sf is not None:
            try:
                self._sf.close()
            except Exception:
                pass
            self._sf = None
