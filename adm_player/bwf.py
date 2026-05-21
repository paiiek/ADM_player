from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

RIFF_SIZE_SENTINEL = 0xFFFFFFFF


@dataclass
class WavChunk:
    chunk_id: str
    data: bytes


def _parse_ds64_payload(payload: bytes) -> tuple[int, int, int, dict[str, int]]:
    """Parse a ds64 payload (per EBU Tech 3306).

    Returns (bw64_size, data_size, sample_count, chunk_size_overrides) where the
    override table maps chunk IDs (ASCII 4-char) → real 64-bit size for any chunks
    beyond `data` that overflow the 32-bit size field.
    """
    if len(payload) < 28:
        raise ValueError("ds64 chunk too short")
    bw64_size, data_size, sample_count, table_length = struct.unpack("<QQQI", payload[:28])
    overrides: dict[str, int] = {}
    off = 28
    for _ in range(table_length):
        if off + 12 > len(payload):
            break
        cid = payload[off : off + 4].decode("ascii", errors="replace")
        size = struct.unpack("<Q", payload[off + 4 : off + 12])[0]
        overrides[cid] = int(size)
        off += 12
    return int(bw64_size), int(data_size), int(sample_count), overrides


def iter_riff_chunks(path: Path | str, max_bytes: int | None = None) -> Iterator[WavChunk]:
    """Yield top-level RIFF/WAVE chunks.

    Supports:
      - Standard RIFF (32-bit chunk sizes, little-endian)
      - RIFX (32-bit chunk sizes, big-endian; rare)
      - BW64 / RF64 (EBU Tech 3306): 32-bit size fields are 0xFFFFFFFF sentinels,
        real sizes live in the leading `ds64` chunk (`data` size + an override table).
    """
    p = Path(path)
    size_limit = max_bytes if max_bytes is not None else p.stat().st_size
    with p.open("rb") as f:
        riff = f.read(4)
        if riff not in (b"RIFF", b"RIFX", b"BW64", b"RF64"):
            raise ValueError(f"Not a RIFF/BW64 file (got {riff!r})")
        endian = ">" if riff == b"RIFX" else "<"
        is_bw64 = riff in (b"BW64", b"RF64")
        hdr = f.read(8)
        if len(hdr) < 8:
            raise ValueError("Truncated RIFF header")
        riff_size_field = struct.unpack(f"{endian}I", hdr[:4])[0]
        wave = hdr[4:8]
        if wave != b"WAVE":
            raise ValueError("Not a WAVE RIFF")

        # BW64/RF64: scan ahead for ds64 (Tech 3306 requires it before `data`, but in
        # practice writers — including this project's — emit `fmt ` first and then ds64).
        # We accept ds64 anywhere before `data` and reject only if it never appears.
        ds64_data_size: int | None = None
        ds64_overrides: dict[str, int] = {}
        bw64_size: int | None = None
        if is_bw64:
            scan_pos = 12
            file_size = size_limit
            found_ds64 = False
            while scan_pos + 8 <= file_size:
                f.seek(scan_pos)
                cid = f.read(4)
                csize_raw = f.read(4)
                if len(cid) < 4 or len(csize_raw) < 4:
                    break
                csize = struct.unpack(f"{endian}I", csize_raw)[0]
                if cid == b"ds64":
                    payload = f.read(csize)
                    bw64_size, ds64_data_size, _sc, ds64_overrides = _parse_ds64_payload(payload)
                    found_ds64 = True
                    break
                if cid == b"data":
                    # `data` reached before `ds64` → malformed BW64
                    break
                # Skip over the chunk (size may be sentinel for non-data; treat as malformed).
                if csize == RIFF_SIZE_SENTINEL:
                    break
                scan_pos += 8 + csize + (csize & 1)
            if not found_ds64:
                raise ValueError("BW64/RF64 file is missing a 'ds64' chunk")

        # Determine the real end of the WAVE body.
        if is_bw64 and bw64_size is not None:
            end = min(8 + bw64_size, size_limit)
        elif riff_size_field == RIFF_SIZE_SENTINEL:
            end = size_limit
        else:
            end = min(8 + riff_size_field, size_limit)

        pos = 12
        while pos + 8 <= end:
            f.seek(pos)
            cid_bytes = f.read(4)
            if len(cid_bytes) < 4:
                break
            cid = cid_bytes.decode("ascii", errors="replace")
            csize_raw = f.read(4)
            if len(csize_raw) < 4:
                break
            csize = struct.unpack(f"{endian}I", csize_raw)[0]
            real_csize = csize
            # BW64 size override: data chunk uses the ds64 dataSize; other oversized
            # chunks come from the override table by chunk-id.
            if is_bw64 and csize == RIFF_SIZE_SENTINEL:
                if cid == "data" and ds64_data_size is not None:
                    real_csize = ds64_data_size
                elif cid in ds64_overrides:
                    real_csize = ds64_overrides[cid]
                else:
                    raise ValueError(
                        f"BW64 chunk {cid!r} has sentinel size but no ds64 override entry"
                    )
            pos += 8
            data = f.read(real_csize)
            pad = real_csize & 1
            pos += real_csize + pad
            # ds64 is parsed during the pre-scan; don't surface it as a regular chunk.
            if is_bw64 and cid == "ds64":
                continue
            yield WavChunk(chunk_id=cid, data=data)


def read_axml(path: Path | str) -> str:
    for ch in iter_riff_chunks(path):
        if ch.chunk_id == "axml":
            raw = ch.data
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            return raw.decode("utf-8", errors="replace")
    raise FileNotFoundError("No 'axml' chunk in WAVE file")


def normalize_track_uid(uid: str) -> str:
    """ADM/chna에서 16진 UID 대소문자 차이로 조회가 실패하지 않도록 통일합니다."""
    u = uid.strip()
    if not u:
        return u
    return u.upper()


def read_chna_mapping(path: Path | str) -> dict[str, int]:
    """
    Parse chna (EBU / ITU-R BS.2088): UID -> zero-based WAV 채널(트랙) 인덱스.

    각 audioID 레코드는 40바이트입니다 (14바이트 레이아웃이 아님):
      WORD trackIndex (1-based, 0 = 미사용 슬롯)
      CHAR UID[12], trackRef[14], packRef[11], pad[1]

    동일 trackIndex에 여러 UID가 올 수 있습니다(시간대별 객체 등).
    """
    for ch in iter_riff_chunks(path):
        if ch.chunk_id != "chna":
            continue
        data = ch.data
        if len(data) < 4:
            return {}
        # struct chna_chunk after ckSize: numTracks, numUIDs (EBU 문서 순서)
        _num_tracks, _num_uids = struct.unpack("<HH", data[:4])
        rec_size = 40
        payload = len(data) - 4
        n_slots = payload // rec_size
        off = 4
        out: dict[str, int] = {}
        for _ in range(n_slots):
            if off + rec_size > len(data):
                break
            track_idx = struct.unpack("<H", data[off : off + 2])[0]
            uid_bytes = data[off + 2 : off + 14]
            off += rec_size
            if track_idx == 0:
                continue
            uid_raw = uid_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
            if not uid_raw:
                continue
            uid = normalize_track_uid(uid_raw)
            if len(uid) >= 4 and uid.startswith("ATU"):
                out[uid] = int(track_idx) - 1
        return out
    return {}
