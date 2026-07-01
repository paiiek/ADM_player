from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

import numpy as np
import soundfile as sf

from .adm_model import AdmObject, active_block
from .osc_emit import AdmOscEmitter

_log = logging.getLogger(__name__)


def _sd():
    """Lazy `sounddevice` import.

    sounddevice loads PortAudio at import; on a headless host that has no
    PortAudio the import raises. The ipc (`--sink ipc://`) path never touches a
    device, so importing sounddevice eagerly would needlessly break headless
    runs (ADR 0019 PR5 PM10 / AC-11). Only the device path calls this.
    """
    import sounddevice as sd  # noqa: PLC0415

    return sd


class AudioSink(Protocol):
    """The minimal sink contract the playback loop drives.

    Both the default `sd.OutputStream` and `IpcRingSink` satisfy it: a
    `.write(np.ndarray)` plus the context-manager protocol.
    """

    def write(self, data: np.ndarray) -> None: ...

    def __enter__(self) -> AudioSink: ...

    def __exit__(self, *exc: object) -> object: ...


class OscPlaybackRef:
    """
    Mutable OSC emitter holder. play_adm_wav reads ``current`` each audio block so the GUI can
    swap presets or connection settings without restarting playback.
    """

    __slots__ = ("current",)

    def __init__(self, emitter: AdmOscEmitter | object | None) -> None:
        self.current = emitter


class ChannelMixState:
    """인터리브 WAV 채널(1-based 번호) 단위 뮤트/솔로. 솔로가 하나라도 있으면 솔로 채널만 재생."""

    __slots__ = ("_lock", "_mute", "_solo")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mute: set[int] = set()
        self._solo: set[int] = set()

    def clear(self) -> None:
        with self._lock:
            self._mute.clear()
            self._solo.clear()

    def set_mute(self, ch1: int, on: bool) -> None:
        with self._lock:
            if on:
                self._mute.add(ch1)
            else:
                self._mute.discard(ch1)

    def set_solo(self, ch1: int, on: bool) -> None:
        with self._lock:
            if on:
                self._solo.add(ch1)
            else:
                self._solo.discard(ch1)

    def snapshot_gains(self, n_ch: int) -> np.ndarray:
        g = np.ones(max(0, n_ch), dtype=np.float32)
        with self._lock:
            if self._solo:
                for i in range(n_ch):
                    if (i + 1) not in self._solo:
                        g[i] = 0.0
            else:
                for ch1 in self._mute:
                    if 1 <= ch1 <= n_ch:
                        g[ch1 - 1] = 0.0
        return g


def coerce_audio_device(device: int | str | None) -> int | str | None:
    """
    CLI에서 온 --audio-device 값을 sounddevice에 맞게 정규화합니다.
    숫자만 있으면 장치 인덱스(int), 아니면 이름 부분 일치용 문자열로 둡니다.
    """
    if device is None:
        return None
    if isinstance(device, int):
        return device
    s = str(device).strip()
    if s.isdigit():
        return int(s)
    return s


def _output_device_id(device: int | str | None) -> int | str:
    if device is not None:
        return device
    sd = _sd()
    # sounddevice uses _InputOutputPair: supports default[1] / ['output'], not len().
    try:
        out_idx = sd.default.device[1]
    except (TypeError, IndexError, KeyError):
        out_idx = None
    if out_idx is None or (isinstance(out_idx, int) and out_idx < 0):
        raise RuntimeError("기본 출력 오디오 장치를 찾을 수 없습니다.")
    return out_idx


def max_output_channels(device: int | str | None) -> int:
    sd = _sd()
    dev = _output_device_id(device)
    info = sd.query_devices(dev, "output")
    n = int(info.get("max_output_channels") or 0)
    return max(1, n)


def print_output_audio_devices(stream: TextIO | None = None) -> None:
    """PortAudio 출력 가능 장치 목록을 인덱스와 함께 stdout에 씁니다."""
    sd = _sd()
    out = stream or sys.stdout
    try:
        default_out = sd.default.device[1]
    except (TypeError, IndexError, KeyError):
        default_out = None
    for i, d in enumerate(sd.query_devices()):
        n_out = int(d.get("max_output_channels") or 0)
        if n_out < 1:
            continue
        name = d.get("name", "?")
        sr = int(float(d.get("default_samplerate") or 0))
        mark = " ← 기본 출력" if default_out is not None and i == default_out else ""
        print(f"  [{i:3d}]  최대 {n_out:3d}ch  {sr:5d} Hz  {name}{mark}", file=out)


def resolve_playback_channels(
    file_channels: int,
    requested_out: int | None,
    device: int | str | None,
) -> tuple[int, int]:
    """
    Returns (out_channels_for_stream, file_channels) after applying device limits.
    If the file has more channels than the device allows, only the leading channels are played.
    """
    if file_channels < 1:
        raise ValueError("No audio channels in file")
    cap = max_output_channels(device)
    want = min(requested_out, file_channels) if requested_out is not None else file_channels
    out = min(want, cap)
    if out < 1:
        out = 1
    return out, file_channels


def _osc_for_audio_block(osc: AdmOscEmitter | OscPlaybackRef | None) -> AdmOscEmitter | object | None:
    if osc is None:
        return None
    if isinstance(osc, OscPlaybackRef):
        return osc.current
    return osc


def play_adm_wav(
    path: Path | str,
    objects: list[AdmObject],
    osc: AdmOscEmitter | OscPlaybackRef | None,
    block_frames: int = 512,
    out_channels: int | None = None,
    device: int | str | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    start_frame: int = 0,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_levels: Callable[[list[float]], None] | None = None,
    channel_mix: ChannelMixState | None = None,
    quiet_truncation: bool = False,
    progress_emit_interval_s: float | None = None,
    levels_emit_interval_s: float | None = None,
    sink: AudioSink | None = None,
    on_error: Callable[[str], None] | None = None,
    meta_sink: object | None = None,
    meta_builder: object | None = None,
) -> None:
    """
    on_progress: (현재 프레임 위치, 총 프레임 수, samplerate) 블록마다 호출.
    on_levels: 채널별 해당 블록 피크(|x|_∞). 뮤트/솔로 게인 적용 **후**, 장치로 줄 채널 자르기 **전**.
    channel_mix: GUI 뮤트/솔로(파일 채널 수 기준).
    stop_event: set 되면 재생 루프 종료.
    pause_event: set 되면 재생 위치 유지하며 대기(일시정지).
    start_frame: 파일 내 시작 프레임(0-based, 이전까지 건너뜀).
    quiet_truncation: True 이면 채널 잘림 안내를 stderr에 찍지 않음(인터랙티브 UI용).
    progress_emit_interval_s / levels_emit_interval_s: None이면 블록마다 콜백.
        양수(초)이면 해당 간격으로만 호출(GUI 등 Qt 시그널 부하 완화용).
    sink: 제공되면(예: IpcRingSink) 오디오 장치 대신 이 sink 로 출력하며, 장치 질의/채널
        잘림 로직을 전혀 거치지 않고 파일의 **모든** 채널을 sink.write() 로 보낸다
        (ADR 0019 PR5 `--sink ipc://`, headless). None 이면 기존 sd.OutputStream 경로.
    on_error: sink.write 가 RuntimeError / OSError 를 던진 경우(출력 장치 분리, IPC ring
        producer teardown 등) 한 번 호출되는 콜백. 호출 후 루프는 정상 종료해 워커가
        finalizer까지 도달한다 — raise 하지 않는다.
    meta_sink: 제공되면(예: MetaRingSink) 블록마다 frame-keyed ADM 메타데이터 sidecar
        레코드를 PCM 보다 **먼저** publish 한다 (TRACK A3 / ADR 0029, meta-before-PCM
        ordering; 소비자의 PCM-acquire 가 meta store 를 transitively 관측하도록). None 이면
        기존 동작(OSC fallback)과 동일. meta_sink 와 짝을 이루는 meta_builder 가 필요.
    meta_builder: MetaRecordBuilder — meta_sink 사용 시 블록별 레코드를 만든다.
    """
    path = Path(path)
    with sf.SoundFile(str(path)) as f:
        sr = int(f.samplerate)
        file_ch = f.channels
        total_frames = int(len(f))

        if sink is not None:
            # ipc / injected-sink path: bypass ALL device logic (no
            # sd.query_devices / _output_device_id / resolve_playback_channels /
            # max_output_channels) and write ALL file channels (PM10 / AC-11).
            write_ch = file_ch
            import contextlib  # noqa: PLC0415

            with contextlib.ExitStack() as stack:
                active_sink = stack.enter_context(sink)
                # The metadata sidecar (opt-in) is opened alongside the PCM sink
                # and torn down on the same exit; None keeps today's behaviour.
                active_meta = stack.enter_context(meta_sink) if meta_sink is not None else None
                _run_playback_loop(
                    f=f,
                    sink=active_sink,
                    sr=sr,
                    write_ch=write_ch,
                    objects=objects,
                    osc=osc,
                    block_frames=block_frames,
                    total_frames=total_frames,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    start_frame=start_frame,
                    on_progress=on_progress,
                    on_levels=on_levels,
                    channel_mix=channel_mix,
                    progress_emit_interval_s=progress_emit_interval_s,
                    levels_emit_interval_s=levels_emit_interval_s,
                    on_error=on_error,
                    meta_sink=active_meta,
                    meta_builder=meta_builder,
                )
            return

        # Default device path: resolve + (possibly) truncate channels.
        device_norm = coerce_audio_device(device)
        out_ch, _ = resolve_playback_channels(file_ch, out_channels, device_norm)
        cap = max_output_channels(device_norm)
        if not quiet_truncation and out_ch < file_ch:
            extra = (
                f"출력 장치 최대 {cap}채널이라 앞쪽만 재생합니다. "
                f"멀티채널 인터페이스 연결 또는 --audio-device 로 전체 경로를 선택할 수 있습니다."
                if file_ch > cap
                else "--out-channels 로 제한한 경우입니다."
            )
            print(
                f"참고: 파일 {file_ch}채널 → {out_ch}채널만 재생. {extra}",
                file=sys.stderr,
            )
        dev_id = _output_device_id(device_norm)
        sd = _sd()
        with sd.OutputStream(
            device=dev_id,
            samplerate=sr,
            channels=out_ch,
            dtype="float32",
            blocksize=block_frames,
        ) as stream:
            _run_playback_loop(
                f=f,
                sink=stream,
                sr=sr,
                write_ch=out_ch,
                objects=objects,
                osc=osc,
                block_frames=block_frames,
                total_frames=total_frames,
                stop_event=stop_event,
                pause_event=pause_event,
                start_frame=start_frame,
                on_progress=on_progress,
                on_levels=on_levels,
                channel_mix=channel_mix,
                progress_emit_interval_s=progress_emit_interval_s,
                levels_emit_interval_s=levels_emit_interval_s,
                on_error=on_error,
            )


def _run_playback_loop(
    *,
    f: sf.SoundFile,
    sink: AudioSink,
    sr: int,
    write_ch: int,
    objects: list[AdmObject],
    osc: AdmOscEmitter | OscPlaybackRef | None,
    block_frames: int,
    total_frames: int,
    stop_event: threading.Event | None,
    pause_event: threading.Event | None,
    start_frame: int,
    on_progress: Callable[[int, int, int], None] | None,
    on_levels: Callable[[list[float]], None] | None,
    channel_mix: ChannelMixState | None,
    progress_emit_interval_s: float | None,
    levels_emit_interval_s: float | None,
    on_error: Callable[[str], None] | None = None,
    meta_sink: object | None = None,
    meta_builder: object | None = None,
) -> None:
    """Shared read → OSC-emit → sink.write loop for both the device and ipc sinks.

    `sink` is already an open context (the caller `with`-opened it). `write_ch`
    is the number of leading channels written: the file channel count for the
    ipc path (no truncation), the device channel cap for the device path.
    """
    if start_frame < 0:
        start_frame = 0
    if start_frame > total_frames:
        start_frame = total_frames
    if start_frame > 0:
        f.seek(start_frame)
    pos = start_frame
    last_progress_t = 0.0
    last_levels_t = 0.0
    prog_iv = progress_emit_interval_s
    lev_iv = levels_emit_interval_s
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        while pause_event is not None and pause_event.is_set():
            time.sleep(0.02)
            if stop_event is not None and stop_event.is_set():
                break
        if stop_event is not None and stop_event.is_set():
            break
        data = f.read(block_frames, dtype="float32", always_2d=True)
        if data.size == 0:
            break
        frames = data.shape[0]
        data = np.asarray(data, dtype=np.float32, order="C")
        if channel_mix is not None and data.shape[1] > 0:
            gains = channel_mix.snapshot_gains(data.shape[1])
            data = data * gains
        if on_levels is not None and data.shape[1] > 0:
            now_mono = time.monotonic()
            emit_levels = lev_iv is None or (now_mono - last_levels_t) >= lev_iv
            if emit_levels:
                peak = np.max(np.abs(data), axis=0)
                on_levels([float(x) for x in peak.tolist()])
                if lev_iv is not None:
                    last_levels_t = now_mono
        if data.shape[1] > write_ch:
            data = data[:, :write_ch].copy()
        t0 = pos / sr
        emitter = _osc_for_audio_block(osc)
        if emitter is not None:
            for obj in objects:
                blk = active_block(obj.blocks, t0)
                emitter.send_object_position(obj, blk)
        # ⟪TRACK A3 Amendment 2 — CRITICAL ORDERING⟫ publish META FIRST, then PCM
        # (below). `pos` is the block's absolute first frame (pre-increment,
        # matching `pos += frames` at the end) → frame_index = pos (A1). OSC
        # emission above stays as the compatibility fallback.
        if meta_sink is not None and meta_builder is not None:
            records = meta_builder.build(objects, t0, pos)
            meta_sink.publish(records, pos)
        if on_progress is not None:
            now_mono = time.monotonic()
            if prog_iv is None or (now_mono - last_progress_t) >= prog_iv:
                on_progress(pos, total_frames, sr)
                if prog_iv is not None:
                    last_progress_t = now_mono
        try:
            sink.write(data)
        except (RuntimeError, OSError) as exc:
            # Output device disconnected mid-stream (USB unplug, ALSA hang,
            # JACK server quit) or IPC ring producer teardown. Don't propagate
            # — the worker thread should see a clean end-of-stream, surface
            # the cause via on_error, and let the outer `with` close the
            # PortAudio stream / IPC sink for us.
            msg = f"{type(exc).__name__}: {exc}"
            _log.error("audio output write failed: %s", msg)
            if on_error is not None:
                try:
                    on_error(msg)
                except Exception as cb_exc:  # noqa: BLE001
                    _log.warning("on_error callback raised: %s", cb_exc)
            break
        pos += frames
    if on_progress is not None:
        on_progress(pos, total_frames, sr)
