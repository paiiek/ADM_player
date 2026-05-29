from __future__ import annotations

import select
import sys
import threading
from pathlib import Path

import soundfile as sf

from .adm_model import AdmObject, parse_track_uid_metadata
from .osc_emit import AdmOscEmitter
from .playback import coerce_audio_device, play_adm_wav


def _fmt_duration(sec: float) -> str:
    sec = max(0.0, sec)
    s = int(sec % 60)
    m = int((sec // 60) % 60)
    h = int(sec // 3600)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def channel_layout_rows(
    n_channels: int,
    chna: dict[str, int],
    uid_meta: dict[str, tuple[str, str]],
) -> list[tuple[int, str, str]]:
    """테이블용 (1-based 채널, UID 문자열, ADM 설명)."""
    inv: dict[int, list[str]] = {}
    for uid, idx in chna.items():
        inv.setdefault(idx, []).append(uid)
    rows: list[tuple[int, str, str]] = []
    for i in range(n_channels):
        uids = sorted(inv.get(i, []))
        uid_str = ", ".join(uids) if uids else "—"
        if uids:
            labels: list[str] = []
            for u in uids:
                if u in uid_meta:
                    lab, td = uid_meta[u]
                    labels.append(f"{lab} · {td}")
                else:
                    labels.append("(ADM에 UID 없음)")
            meta = " | ".join(labels)
        else:
            meta = "(chna 매핑 없음)"
        rows.append((i + 1, uid_str, meta))
    return rows


def format_channel_layout(
    n_channels: int,
    sample_rate: float,
    duration_sec: float,
    format_info: str,
    chna: dict[str, int],
    uid_meta: dict[str, tuple[str, str]],
) -> str:
    """ADM BWF 채널 구성 표를 여러 줄 문자열로 반환합니다."""
    inv: dict[int, list[str]] = {}
    for uid, idx in chna.items():
        inv.setdefault(idx, []).append(uid)
    lines: list[str] = [
        "",
        "─" * 72,
        "ADM BWF 요약",
        "─" * 72,
        f"  샘플레이트   : {int(sample_rate)} Hz",
        f"  길이         : {_fmt_duration(duration_sec)} ({duration_sec:.2f} s)",
        f"  포맷         : {format_info}",
        f"  인터리브 채널: {n_channels}",
        f"  chna 트랙 수 : {len(chna)}",
        "",
        "  채널(1-based) | 트랙 UID        | ADM 요소 / typeDefinition",
        "  " + "-" * 68,
    ]
    for i in range(n_channels):
        uids = sorted(inv.get(i, []))
        uid_str = ", ".join(uids) if uids else "—"
        if uids:
            labels: list[str] = []
            for u in uids:
                if u in uid_meta:
                    lab, td = uid_meta[u]
                    labels.append(f"{lab} ({td})")
                else:
                    labels.append("(ADM에 UID 없음)")
            meta = " | ".join(labels)
        else:
            meta = "(chna에 없음 — 매핑 미상)"
        lines.append(f"  {i + 1:5d}         | {uid_str[:28]:<28} | {meta[:44]}")
    lines.extend(["  " + "-" * 68, ""])
    return "\n".join(lines)


def print_channel_layout(
    n_channels: int,
    sample_rate: float,
    duration_sec: float,
    format_info: str,
    chna: dict[str, int],
    uid_meta: dict[str, tuple[str, str]],
) -> None:
    """터미널에 ADM BWF 채널 구성 표를 출력합니다."""
    print(format_channel_layout(n_channels, sample_rate, duration_sec, format_info, chna, uid_meta))


def _restore_tty(fd: int, old: list) -> None:
    try:
        import termios

        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError, AttributeError):
        pass


def _read_char_timeout(fd: int, timeout: float) -> str | None:
    if select.select([fd], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None


def run_interactive(
    wav_path: Path,
    objects: list[AdmObject],
    osc: AdmOscEmitter | None,
    axml: str,
    chna: dict[str, int],
    block_frames: int,
    out_channels: int | None,
    device: int | str | None,
) -> None:
    wav_path = wav_path.resolve()
    device = coerce_audio_device(device)
    uid_meta = parse_track_uid_metadata(axml)
    with sf.SoundFile(str(wav_path)) as f:
        n_ch = f.channels
        sr = float(f.samplerate)
        n_frames = int(len(f))
        format_info = f"{f.format}/{f.subtype}" if f.subtype else str(f.format)
    duration_sec = n_frames / sr if sr else 0.0

    print_channel_layout(n_ch, sr, duration_sec, format_info, chna, uid_meta)

    fd = sys.stdin.fileno()
    old_tty: list | None = None
    use_raw = False
    if sys.stdin.isatty():
        try:
            import termios
            import tty

            old_tty = termios.tcgetattr(fd)
            tty.setraw(fd)
            use_raw = True
        except (ImportError, OSError, AttributeError):
            old_tty = None
            use_raw = False

    playing = False
    stop_ev: threading.Event | None = None
    play_thread: threading.Thread | None = None
    pos_frames = 0

    def on_progress(pos: int, _total: int, _sample_rate: int) -> None:
        nonlocal pos_frames
        pos_frames = pos

    def play_worker(ev: threading.Event) -> None:
        nonlocal playing, pos_frames
        try:
            play_adm_wav(
                wav_path,
                objects,
                osc=osc,
                block_frames=block_frames,
                out_channels=out_channels,
                device=device,
                stop_event=ev,
                on_progress=on_progress,
                quiet_truncation=True,
            )
        finally:
            playing = False
            pos_frames = 0

    print("키 안내:  [p] 재생   [s] 정지   [q] 종료", flush=True)
    if not use_raw:
        print("(한 글자 + Enter: p / s / q)", flush=True)

    try:
        while True:
            if playing and stop_ev is not None and play_thread is not None:
                elapsed = pos_frames / sr if sr else 0.0
                sys.stdout.write(
                    f"\r\033[K재생 {_fmt_duration(elapsed)} / {_fmt_duration(duration_sec)}    (s: 정지, q: 종료)   "
                )
                sys.stdout.flush()
                ch: str | None
                if use_raw:
                    ch = _read_char_timeout(fd, 0.12)
                else:
                    if select.select([sys.stdin], [], [], 0.12)[0]:
                        line = sys.stdin.readline()
                        ch = line[0] if line else None
                    else:
                        ch = None
                if ch is None:
                    if not play_thread.is_alive():
                        playing = False
                        sys.stdout.write("\r\033[K재생이 끝났습니다.\n")
                        sys.stdout.flush()
                    continue
                c = ch.lower()
                if c == "s":
                    stop_ev.set()
                    play_thread.join(timeout=60.0)
                    playing = False
                    sys.stdout.write("\r\033[K정지했습니다.\n")
                    sys.stdout.flush()
                elif c == "q":
                    stop_ev.set()
                    play_thread.join(timeout=60.0)
                    sys.stdout.write("\n종료합니다.\n")
                    sys.stdout.flush()
                    break
            else:
                sys.stdout.write("\r\033[K대기 — [p] 재생  [q] 종료 > ")
                sys.stdout.flush()
                if use_raw:
                    ch = sys.stdin.read(1)
                else:
                    line = sys.stdin.readline()
                    ch = line[0] if line else ""
                if not ch:
                    break
                c = ch.lower()
                if c == "p":
                    stop_ev = threading.Event()
                    playing = True
                    pos_frames = 0
                    play_thread = threading.Thread(
                        target=play_worker, args=(stop_ev,), daemon=True
                    )
                    play_thread.start()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif c == "q":
                    sys.stdout.write("\n종료합니다.\n")
                    sys.stdout.flush()
                    break
    finally:
        if old_tty is not None:
            _restore_tty(fd, old_tty)
