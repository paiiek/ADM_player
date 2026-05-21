# ADM Player ↔ Spatial Engine — 주간 진행 노트 (2026-W21, ~05-21)

> 이 문서는 프로젝트를 처음 보는 사람도 읽을 수 있도록 시스템 소개부터 시작합니다.
> "이번 주에 뭐 했나"만 보려면 [§4 이번 주 진행](#4-이번-주-진행-2026-05-18--05-21)으로.

---

## 1. 한 줄 요약 (TL;DR)

- **ADM Player**는 멀티채널 WAV(ADM BWF) 마스터를 재생하면서, 각 오디오 객체의 3D 위치를 **ADM-OSC**라는 표준 메시지로 외부 렌더러에 흘려보내는 도구다.
- 사내 렌더러인 **spatial_engine**(C++/JUCE)과 붙여서 "재생 → 공간 렌더 → 바이노럴/스피커 출력"의 전체 체인을 만드는 게 목표.
- 이번 주는 그 통합의 **Phase B(동기)·Phase C(샘플 정확 IPC)·Recorder 통합**에 대한 설계 문서(ADR) 3건을 쓰고, 그중 즉시 구현 가능한 **엔진 측 3개 PR을 실제로 구현·커밋**했다.
- 핵심 성과 하나: 엔진의 OSC 파서가 player가 보내는 `,d`(double) 메시지를 **통째로 버리고 있던 잠복 버그**를 발견하고 고쳤다. 이게 없었으면 Phase B 동기 기능 전체가 동작하지 않았다.

---

## 2. ADM Player가 뭔가 (처음 보는 사람을 위한 소개)

### 2.1 풀려는 문제

영화/공연용 **객체 기반 오디오(object-based audio)**에서는 "이 소리가 공간의 어디에 있는가"를 채널이 아니라 **객체 + 위치 메타데이터**로 표현한다 (Dolby Atmos가 대표적). 이때 두 가지가 따로 논다:

1. **오디오 본체** — 각 객체의 모노 음원 (멀티채널 WAV에 인터리브)
2. **위치 메타데이터** — 시간에 따라 객체가 어디로 움직이는가 (azimuth/elevation/distance)

ADM Player는 이 둘이 함께 들어 있는 **ADM BWF**(Broadcast WAV + ADM 메타데이터) 파일을 읽어서:
- 오디오는 사운드 디바이스로 재생하고,
- 위치 메타데이터는 **ADM-OSC v1.0** 메시지로 변환해 외부 렌더러에 실시간 전송한다.

### 2.2 핵심 개념 사전

| 용어 | 뜻 |
|---|---|
| **ADM** | Audio Definition Model. 객체 기반 오디오 메타데이터 표준(EBU Tech 3364). |
| **BWF** | Broadcast WAV Format. WAV에 `axml`(ADM XML) + `chna`(채널↔객체 인덱스) 청크를 끼운 것. |
| **ADM-OSC** | 위 위치 데이터를 OSC(Open Sound Control) 네트워크 메시지로 주고받는 사실상 표준. `/adm/obj/N/aed` 같은 주소. |
| **aed / xyz** | 위치 표현 방식. `aed`=구면(azimuth/elevation/distance), `xyz`=직교 좌표. |
| **렌더러** | 위치 메타데이터를 받아 실제 스피커/헤드폰 신호로 만드는 엔진. L-ISA, Spat Revolution, 그리고 우리의 `spatial_engine`. |

### 2.3 세 개의 프로그램

DreamScape 묶음에는 세 가지가 있다:

1. **ADM Player** (Python/PySide6) — BWF 마스터를 재생 + ADM-OSC 송신. 6종 외부 렌더러 프리셋 + `spatial_engine` 프리셋 내장.
2. **ADM Recorder** (Python) — 거꾸로, 라이브로 들어오는 ADM-OSC + 멀티채널 오디오를 받아 **Nuendo/Pro Tools 호환 ADM BWF 마스터로 기록**.
3. **spatial_engine** (C++/JUCE, 별도 레포) — ADM-OSC를 받아 VBAP/WFS/Ambisonic/바이노럴로 렌더링하는 실시간 엔진. 별도 git 레포(`/home/seung/mmhoa/spatial_engine`)다.

### 2.4 좌표 규약 (헷갈리기 쉬운 부분)

- `azimuth`: 도(degree), **왼쪽이 양수** (ADM-OSC/AmbiX 표준).
- `elevation`: 도, **위가 양수**, [-90, +90].
- `distance`: 정규화 [0, 1]. 1 초과 값은 미터로 보고 `ADM_OSC_MAX_DIST=20`으로 나눈다 (엔진 ADR 0006과 정합).
- 직교: `+X=오른쪽, +Y=앞, +Z=위`.

### 2.5 통합 단계 (Phase A → B → C)

| Phase | 내용 | 동기 정확도 |
|---|---|---|
| **A** | OSC만 전송, 오디오는 OS 라우팅(JACK/BlackHole/ASIO)으로 분리 | ~1 블록(~10ms) 앞섬, 인지적으론 무시 가능 |
| **B** | + handshake/heartbeat/transport timetag 동기 | 시작 시점 정렬, drift 잔존 |
| **C** | PCM을 공유메모리 ring으로 직접 전달 | **샘플 정확**, OS 디바이스 의존 제거 |

전체 로드맵은 [`.omc/plans/adm_player_integration.md`](../../.omc/plans/adm_player_integration.md) 참고. M1·M2(Phase A)·M3(Phase B player측)은 지난 주까지 닫혔고, 이번 주는 **엔진 측 + Phase C 설계 + Recorder 통합**을 다뤘다.

---

## 3. 시스템 구성도

```
   ┌──────────────┐   /adm/obj/N/aed (UDP 9100)   ┌────────────────────┐
   │  ADM Player  │ ─────────────────────────────►│   spatial_engine   │
   │ (BWF 재생)   │   /sys/handshake, /hb/ping,    │  (VBAP/HRTF 렌더)  │
   │              │   /transport/play  (Phase B)   │                    │
   └──────┬───────┘                                └─────────┬──────────┘
          │ 오디오 PCM                                       │ /adm echo (9102, M5)
          │ (Phase A: OS 라우팅                              ▼
          │  Phase C: shm ring) ════════════►        ┌────────────────┐
          └────────────────────────────────────────►│  ADM Recorder  │
                                                     │ (BWF 마스터 기록)│
                                                     └────────────────┘
```

---

## 4. 이번 주 진행 (2026-05-18 ~ 05-21)

지난 주 제안했던 다음 3개 사이클 후보를 **순서대로 모두** 처리했다:

> 1. engine 측 Phase B 핸들러 ADR
> 2. M4 Phase C PCM IPC (샘플 정확 동기, shm ring)
> 3. M5 Recorder 통합 (engine OSC echo → recorder 수신)

### 4.1 설계 문서 3건 작성

| 문서 | 위치 | 핵심 결정 |
|---|---|---|
| **ADR 0018** Phase B 동기 핸들러 | `spatial_engine/docs/adr/0018-phase-b-sync-handlers.md` | `,d`/`,h` 타입태그 호환, `/transport/play` 타임태그는 advisory(스케줄링 안 함), `/transport/pause`=stop 별칭, player heartbeat staleness 경고 |
| **ADR 0019** Phase C PCM IPC | `spatial_engine/docs/adr/0019-phase-c-pcm-ipc-shm-ring.md` | SPSC 공유메모리 ring, 4KiB 헤더 + planar f32 채널, audio-thread no-syscall/no-alloc, producer 죽으면 silence 출력 |
| **M5** Recorder 통합 | `adm_player/.omc/plans/m5_recorder_integration.md` | 엔진이 인바운드 OSC를 9102로 **echo** → recorder가 기존 `OscIngestRouter("adm")`로 그대로 수신. 엔진을 single-source-of-truth로 |

### 4.2 ⚠️ 발견한 잠복 버그 (이번 주 가장 중요한 발견)

ADR 0018 작성 중 코드를 실측하다가 발견: **엔진의 OSC 타입태그 파서(`CommandDecoder.cpp:84-124`)가 `,d`(OSC double)를 `default: return false`로 패킷 통째로 거부**하고 있었다. 게다가 `,h`(int64)는 `p += 8; // skip`으로 값을 어디에도 저장하지 않았다.

→ player가 M3에서 보내는 `/hb/ping ,d`, `/transport/play ,d`가 **엔진 도착 즉시 전량 폐기**되고 있었다. "주소 분기는 있으니 됐겠지"라는 착각 상태였고, Phase B는 실질적으로 **엔진 측에서 동작하지 않는 상태**였다. handshake(`,ii`)만 우연히 살아 있었던 것.

이 발견 때문에 ADR 0018을 "핸들러만 추가" → "파서 수정이 선행 조건(load-bearing)"으로 수정했다.

### 4.3 엔진 측 3개 PR 구현·커밋

세 작업을 격리 환경에서 병렬 실행 → 실제 `spatial_engine` 레포 `main`에 깔끔히 스택됨:

| 커밋 | 내용 | 테스트 |
|---|---|---|
| `67b7fcb` | **ADR 0018 PR1** — 파서가 `,d`를 `OscArgs.doubles[]`에 받고 `,h`를 `u64s[]`에 와이어업. `/hb/ping` 디스패처가 `,d` 초→ms 변환 | parser P1–P4 통과 |
| `9cd6d57` | **ADR 0019 PR1** — POSIX `SharedMemoryRegion` + `RingHeader` POD(4KiB, 전 오프셋 static_assert) | shm 9건 + full 91/91 |
| `aacff4a` | **M5.1** — 9102 echo plane, `/sys/handshake ,iis` subscribe, dirty-bit coalesce, 5kHz rate guard, 30초 heartbeat TTL | echo 10건 + full 92/92 |

전부 다른 파일군을 건드려 충돌 없이 순차 스택됐다. 풀 ctest는 PR1 후 91/91 → M5.1 후 92/92 green.

---

## 5. 현재 상태

- **spatial_engine `main`**: 위 3개 커밋이 반영됨. full ctest 92/92 green.
- **adm_player(player/recorder 측)**: 지난 주 M3까지 pytest 54/54, smoke 9/9 green 상태 그대로. 이번 주 player 코드 변경은 없음(엔진 측 작업이었음).
- 이번 주 작업은 각 ADR의 **PR1(기반)만** 구현했다. ADR 0018은 PR2~(주소 디스패처 본체, `/transport/pause` 별칭, staleness 경고)가, ADR 0019는 PR2~(`SharedRingBackend`, 엔진 wiring, Python sidecar)가, M5는 M5.2~(recorder ingest, axml writer 확장, soak)가 남았다.

### ⚠️ 미커밋 / 주의사항

1. **ADR 0018·0019 문서 자체가 spatial_engine에서 untracked 상태**(`??`)다 — 구현 커밋은 들어갔는데 문서가 아직 git에 안 올라갔다. M5 plan은 adm_player `.omc/plans/`에 있고 역시 미커밋.
2. **worktree 격리 주의**: 격리 worktree는 부모 MMHOA 레포 기준으로 생성됐는데, 작업 대상인 spatial_engine은 별도 nested 레포라 격리 밖이었다. 결과적으로 3개 PR이 worktree 브랜치가 아니라 **실제 spatial_engine `main`에 직접** 커밋됐다. 이번엔 파일이 겹치지 않아 깔끔히 스택됐지만, 다음에 spatial_engine을 병렬 수정할 땐 이 레포 안에서 worktree/브랜치를 떠야 한다.

---

## 6. 다음 단계 후보

1. **문서 커밋 정리** — ADR 0018/0019/0017 + M5 plan을 각 레포에 커밋 (지금 untracked).
2. **ADR 0018 PR2** — 주소 디스패처 본체(`/transport/play ,d` advisory 저장, `/transport/pause` 별칭) + player heartbeat staleness 경고 + soak harness.
3. **ADR 0019 PR2** — `SharedRingBackend : AudioBackend` 구현 + 엔진 `--input-backend shm:` CLI.
4. **M5.2** — recorder `--osc-source engine` 분기 + timeline_store gain/mute/width 확장 (adm_player 레포).
5. **end-to-end soak** — player + engine + recorder 3-프로세스 60초 시나리오로 Phase B 동기 + echo 기록 실증.

각 PR1이 독립이라 2·3·4는 다시 병렬 가능(단, spatial_engine 레포 내에서 브랜치 분리할 것).
