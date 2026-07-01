from __future__ import annotations

import argparse
import sys
from pathlib import Path

import soundfile as sf

from .adm_model import parse_adm_objects
from .bwf import normalize_track_uid, read_axml, read_chna_mapping
from .interactive import run_interactive
from .osc_emit import MAX_OSC_OBJECTS, AdmOscEmitter
from .osc_sync import SyncEmitter
from .playback import coerce_audio_device, max_output_channels, play_adm_wav, print_output_audio_devices


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="ADM BWF 재생 + ADM-OSC 객체 위치 스트리밍 (Objects 타입).",
        epilog=(
            "예: adm-player mix.wav --osc-host 127.0.0.1 --osc-port 9000\n"
            "출력 장치: --list-audio-devices 로 번호 확인 후 "
            "--audio-device 3 처럼 지정.\n"
            "외장 렌더러 좌표계는 --flip-azimuth / --azimuth-offset 으로 맞출 수 있습니다."
        ),
    )
    p.add_argument(
        "wav",
        type=Path,
        nargs="?",
        default=None,
        help="ADM BWF (.wav) 경로",
    )
    p.add_argument("--osc-host", default="127.0.0.1", help="OSC 수신 호스트")
    p.add_argument("--osc-port", type=int, default=9000, help="OSC UDP 포트")
    p.add_argument("--no-osc", action="store_true", help="OSC 전송 비활성화")
    p.add_argument("--prog", type=int, default=None, help="프로그램 번호 (선택, /adm/prog/N/... )")
    p.add_argument("--azimuth-offset", type=float, default=0.0, help="방위각 오프셋(도)")
    p.add_argument("--flip-azimuth", action="store_true", help="방위각 부호 반전")
    p.add_argument("--block-frames", type=int, default=512, help="오디오/OSC 블록 프레임 수")
    p.add_argument(
        "--sink",
        default=None,
        metavar="ipc://NAME",
        help=(
            "오디오 장치 대신 공유메모리 ring 으로 출력 (ADR 0019 shm IPC, headless). "
            "형식 ipc://NAME (POSIX shm 이름). 엔진은 --input-backend shm:/NAME 으로 짝을 맞춘다. "
            "--audio-device/--interactive/--list-audio-devices/--out-channels 와 함께 쓸 수 없다."
        ),
    )
    p.add_argument(
        "--block-size",
        type=int,
        default=256,
        metavar="N",
        help="--sink ipc:// 사용 시 shm ring 의 wire block_size (엔진 콜백 블록의 약수여야 함).",
    )
    p.add_argument(
        "--ring-frames",
        type=int,
        default=8192,
        metavar="N",
        help="--sink ipc:// 사용 시 ring 용량(프레임). 2의 거듭제곱이 아니면 다음 2^n 으로 패딩한다.",
    )
    p.add_argument(
        "--meta-sink",
        default=None,
        metavar="shm:/NAME",
        help=(
            "프레임 동기 ADM 메타데이터 sidecar 링을 함께 publish 한다 (TRACK A3 / ADR 0029, "
            "opt-in). 형식 shm:/NAME. 엔진은 --meta-backend shm:/NAME 으로 짝을 맞춘다. "
            "--sink ipc:// 와 함께 써야 한다 (PCM 링과 프레임을 공유). 생략 시 기존 동작(OSC fallback)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="오디오 출력 없이 axml/chna 파싱 결과만 표시",
    )
    p.add_argument(
        "--out-channels",
        type=int,
        default=None,
        metavar="N",
        help="재생할 채널 수(파일 앞쪽 N채널). 생략 시 출력 장치가 허용하는 만큼 자동.",
    )
    p.add_argument(
        "--audio-device",
        default=None,
        metavar="INDEX_OR_NAME",
        help="출력 장치: 정수 인덱스(권장) 또는 이름 일부 문자열. 생략 시 시스템 기본 출력.",
    )
    p.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="출력 가능한 오디오 장치 목록을 표시하고 종료합니다.",
    )
    p.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="채널 구성 표시 후 [p]재생 [s]정지 [q]종료 (재생 시간 표시)",
    )
    p.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Phase B sync 활성화: spatial_engine 등 호환 수신기에 handshake + "
            "1Hz heartbeat + /transport/play 시각을 송신합니다."
        ),
    )
    p.add_argument(
        "--sync-reply-port",
        type=int,
        default=9101,
        metavar="PORT",
        help="--sync 사용 시 handshake에 광고할 응답 포트 (engine ack 수신용).",
    )
    args = p.parse_args(argv)
    args.audio_device = coerce_audio_device(args.audio_device)

    sink_name: str | None = None
    if args.sink is not None:
        if not args.sink.startswith("ipc://"):
            p.error("--sink 는 ipc://NAME 형식이어야 합니다 (예: --sink ipc://spe-session-1).")
        sink_name = args.sink[len("ipc://") :]
        if not sink_name:
            p.error("--sink ipc:// 뒤에 shm 이름이 필요합니다 (예: --sink ipc://spe-session-1).")
        if args.audio_device is not None:
            p.error("--sink ipc:// 와 --audio-device 는 함께 사용할 수 없습니다.")
        if args.interactive:
            p.error("--sink ipc:// 와 --interactive 는 함께 사용할 수 없습니다.")
        if args.list_audio_devices:
            p.error("--sink ipc:// 와 --list-audio-devices 는 함께 사용할 수 없습니다.")
        if args.out_channels is not None:
            p.error("--sink ipc:// 와 --out-channels 는 함께 사용할 수 없습니다.")

    meta_name: str | None = None
    if args.meta_sink is not None:
        if not args.meta_sink.startswith("shm:/"):
            p.error("--meta-sink 는 shm:/NAME 형식이어야 합니다 (예: --meta-sink shm:/spe-session-1-meta).")
        meta_name = args.meta_sink[len("shm:/") :]
        if not meta_name:
            p.error("--meta-sink shm:/ 뒤에 shm 이름이 필요합니다 (예: --meta-sink shm:/spe-session-1-meta).")
        if sink_name is None:
            p.error("--meta-sink 는 --sink ipc:// 와 함께 써야 합니다 (PCM 링과 프레임을 공유).")

    if args.list_audio_devices:
        print("출력용 오디오 장치 (--audio-device 에 정수 인덱스로 지정):", flush=True)
        print_output_audio_devices()
        return 0

    if args.wav is None:
        p.error("wav 파일 경로가 필요합니다. (목록만 보려면 --list-audio-devices)")

    if args.dry_run and args.interactive:
        p.error("--dry-run 과 --interactive 는 함께 사용할 수 없습니다.")

    wav_path = args.wav.expanduser().resolve()
    if not wav_path.is_file():
        print(f"파일 없음: {wav_path}", file=sys.stderr)
        return 1

    try:
        axml = read_axml(wav_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    chna = read_chna_mapping(wav_path)
    with sf.SoundFile(str(wav_path)) as f:
        sample_rate = float(f.samplerate)
        n_ch = f.channels

    objects = parse_adm_objects(axml, sample_rate, chna)
    if not objects:
        print(
            "경고: typeDefinition=Objects 인 audioObject 가 없습니다. "
            "OSC는 전송되지 않을 수 있습니다.",
            file=sys.stderr,
        )

    n_over = sum(1 for o in objects if o.osc_object_index > MAX_OSC_OBJECTS)
    if n_over > 0:
        print(
            f"경고: 객체 {len(objects)}개 중 {n_over}개가 OSC 슬롯 한도(MAX={MAX_OSC_OBJECTS})를 초과합니다. "
            f"현재 한도는 첫 {MAX_OSC_OBJECTS} 슬롯이며 (기본 128, 64-빌드 엔진은 "
            "SPE_ADM_OSC_MAX_OBJECTS=64 로 낮춤) 초과 객체의 위치 메타데이터는 송신되지 않습니다 "
            "(오디오 재생은 영향 없음).",
            file=sys.stderr,
        )

    for obj in objects:
        if not obj.wav_channels and chna:
            missing = [u for u in obj.track_uids if normalize_track_uid(u) not in chna]
            if missing:
                print(
                    f"경고: 객체 {obj.label!r} 의 트랙 UID가 chna에 없음: {missing}",
                    file=sys.stderr,
                )
        if not obj.wav_channels and not chna and obj.adm_index <= n_ch:
            obj.wav_channels = [obj.adm_index - 1]
            print(
                f"참고: chna 없음 — 객체 {obj.adm_index} 를 WAV 채널 {obj.wav_channels[0]+1} 에 대응(추정).",
                file=sys.stderr,
            )

    osc: AdmOscEmitter | None = None
    if args.dry_run:
        print(f"샘플레이트: {int(sample_rate)} Hz, 채널: {n_ch}")
        # --dry-run is metadata-only (no rendering). Only probe an output device
        # when the user explicitly named one — otherwise a headless box with no
        # default output prints a misleading "출력 장치 조회 실패" on every dry run.
        if args.audio_device is not None:
            try:
                cap = max_output_channels(args.audio_device)
                print(f"선택한 출력 장치 최대 채널: {cap}")
            except Exception as e:
                print(f"출력 장치 조회 실패: {e}", file=sys.stderr)
        print(f"chna UID 매핑: {len(chna)} 개")
        for obj in objects:
            print(
                f"  객체 #{obj.adm_index} → /adm/obj/{obj.osc_object_index} (WAV채널 {obj.osc_object_index}) {obj.label!r} "
                f"UIDs={obj.track_uids} wav_ch={obj.wav_channels} blocks={len(obj.blocks)}"
            )
        return 0

    if not args.no_osc:
        osc = AdmOscEmitter(
            args.osc_host,
            args.osc_port,
            prog=args.prog,
            azimuth_offset=args.azimuth_offset,
            azimuth_flip=args.flip_azimuth,
        )
        for obj in objects:
            blk = obj.blocks[0] if obj.blocks else None
            if blk is not None:
                osc.send_object_config_cartesian(obj.osc_object_index, blk.position.mode == "cartesian")
                osc.send_object_position(obj, blk)

    sync: SyncEmitter | None = None
    if args.sync and not args.no_osc:
        sync = SyncEmitter(args.osc_host, args.osc_port)
        sync.send_handshake(args.sync_reply_port)
        sync.start_heartbeat()

    try:
        if args.interactive:
            try:
                if sync is not None:
                    sync.send_transport_play()
                run_interactive(
                    wav_path,
                    objects,
                    osc=osc,
                    axml=axml,
                    chna=chna,
                    block_frames=args.block_frames,
                    out_channels=args.out_channels,
                    device=args.audio_device,
                )
                if sync is not None:
                    sync.send_transport_stop()
            except KeyboardInterrupt:
                if sync is not None:
                    sync.send_transport_stop()
                print("\n중단됨.", file=sys.stderr)
                return 130
            return 0

        try:
            if sync is not None:
                sync.send_transport_play()
            if sink_name is not None:
                # shm IPC path: build IpcRingSink writing ALL file channels at
                # the --block-size granularity; bypass all device logic (PM10).
                from .ipc_sink import IpcRingSink

                ipc_sink = IpcRingSink(
                    sink_name,
                    sample_rate=int(sample_rate),
                    channels=n_ch,
                    block_size=args.block_size,
                    ring_frames=args.ring_frames,
                )
                meta_sink = None
                meta_builder = None
                if meta_name is not None:
                    # TRACK A3 sidecar (opt-in): frame-keyed metadata alongside
                    # the PCM ring. The builder mirrors the OSC emitter's coords
                    # so the sidecar is bit-consistent with OSC for each block.
                    from .adm_meta import MetaRecordBuilder
                    from .meta_sink import MetaRingSink

                    meta_sink = MetaRingSink(
                        meta_name,
                        sample_rate=int(sample_rate),
                        slot_count=MAX_OSC_OBJECTS,
                    )
                    meta_builder = MetaRecordBuilder(
                        azimuth_offset=args.azimuth_offset,
                        azimuth_flip=args.flip_azimuth,
                    )
                play_adm_wav(
                    wav_path,
                    objects,
                    osc=osc,
                    block_frames=args.block_size,
                    sink=ipc_sink,
                    meta_sink=meta_sink,
                    meta_builder=meta_builder,
                )
            else:
                play_adm_wav(
                    wav_path,
                    objects,
                    osc=osc,
                    block_frames=args.block_frames,
                    out_channels=args.out_channels,
                    device=args.audio_device,
                )
            if sync is not None:
                sync.send_transport_stop()
        except KeyboardInterrupt:
            if sync is not None:
                sync.send_transport_stop()
            print("\n중단됨.", file=sys.stderr)
            return 130
        return 0
    finally:
        if sync is not None:
            sync.close()


if __name__ == "__main__":
    raise SystemExit(main())
