from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import soundfile as sf

from .channel_config import ChannelMapState, ChannelRole
from .timeline_store import ObjectBlock, frames_to_smpte_timecode

# Streaming PCM_24 encoder constants — keep RAM usage flat regardless of master length.
PCM24_BYTES = 3
PCM_BLOCK_FRAMES = 65536          # ~ 4.7 MB per channel at 24-bit → bounded RAM
RIFF_MAX_32BIT = 0xFFFFFFFF
# Per EBU Tech 3306: when BW64 mode kicks in, the legacy 32-bit size fields are
# written as 0xFFFFFFFF and the real 64-bit sizes live in the ds64 chunk. This is
# kept separate from RIFF_MAX_32BIT (the size *threshold*) so tests can monkeypatch
# the threshold to force BW64 without corrupting the on-wire sentinel value.
RIFF_SIZE_SENTINEL = 0xFFFFFFFF
RIFF_BW64_SAFETY_MARGIN = 1 << 20  # 1 MiB headroom before switching to BW64

# Nuendo / Dolby ADM BWF: ebuCoreMain + audioFormatExtended (see working reference BWFs)
EBU_MAIN_NS = "urn:ebu:metadata-schema:ebuCore_2016"

# Table 2-11 / 2-14: label → (audioChannelFormatName, speakerLabel, X, Y, Z)
_BED_SPEAKER_DOLBY: dict[str, tuple[str, str, tuple[float, float, float]]] = {
    "L": ("RoomCentricLeft", "RC_L", (-1.0, 1.0, 0.0)),
    "R": ("RoomCentricRight", "RC_R", (1.0, 1.0, 0.0)),
    "C": ("RoomCentricCenter", "RC_C", (0.0, 1.0, 0.0)),
    "LFE": ("RoomCentricLFE", "RC_LFE", (-1.0, 1.0, -1.0)),
    "Ls": ("RoomCentricLeftSurround", "RC_Ls", (-1.0, -1.0, 0.0)),
    "Rs": ("RoomCentricRightSurround", "RC_Rs", (1.0, -1.0, 0.0)),
    "Lss": ("RoomCentricLeftSideSurround", "RC_Lss", (-1.0, 0.0, 0.0)),
    "Rss": ("RoomCentricRightSideSurround", "RC_Rss", (1.0, 0.0, 0.0)),
    "Lrs": ("RoomCentricLeftRearSurround", "RC_Lrs", (-1.0, -1.0, 0.0)),
    "Rrs": ("RoomCentricRightRearSurround", "RC_Rrs", (1.0, -1.0, 0.0)),
    "Lsr": ("RoomCentricLeftRearSurround", "RC_Lrs", (-1.0, -1.0, 0.0)),
    "Rsr": ("RoomCentricRightRearSurround", "RC_Rrs", (1.0, -1.0, 0.0)),
    # 7.1.2 single top pair (above listener)
    "Lts": ("RoomCentricLeftTopSurround", "RC_Lts", (-1.0, 0.0, 1.0)),
    "Rts": ("RoomCentricRightTopSurround", "RC_Rts", (1.0, 0.0, 1.0)),
    # 7.1.4 distinct Top Front / Top Rear (must not collapse to single Top Surround pair —
    # Nuendo/Pro Tools relies on these labels to keep 4 height channels separate)
    "Ltf": ("RoomCentricLeftTopFront", "RC_Ltf", (-1.0, 1.0, 1.0)),
    "Rtf": ("RoomCentricRightTopFront", "RC_Rtf", (1.0, 1.0, 1.0)),
    "Ltr": ("RoomCentricLeftTopRear", "RC_Ltr", (-1.0, -1.0, 1.0)),
    "Rtr": ("RoomCentricRightTopRear", "RC_Rtr", (1.0, -1.0, 1.0)),
    # Tfl/Tfr alias to Top Front pair (same role as Ltf/Rtf in 7.1.2 layouts)
    "Tfl": ("RoomCentricLeftTopFront", "RC_Ltf", (-1.0, 1.0, 1.0)),
    "Tfr": ("RoomCentricRightTopFront", "RC_Rtf", (1.0, 1.0, 1.0)),
}


def _chna_pad_field(s: str, length: int) -> bytes:
    raw = s.encode("ascii", errors="replace")[:length]
    return raw.ljust(length, b"\x00")


def build_chna_bytes(
    rows: list[tuple[int, str, str, str]],
) -> bytes:
    """EBU chna: rows (track_1based, ATU, AT_…_01, AP_…)."""
    n = len(rows)
    buf = struct.pack("<HH", n, n)
    for track_idx, uid_s, track_ref, pack_ref in rows:
        if len(track_ref) > 14 or len(pack_ref) > 11:
            raise ValueError("chna trackRef/packRef exceeds EBU field size")
        buf += (
            struct.pack("<H", int(track_idx))
            + _chna_pad_field(uid_s, 12)
            + _chna_pad_field(track_ref, 14)
            + _chna_pad_field(pack_ref, 11)
            + b"\x00"
        )
    return buf


def _load_dbmd_template() -> bytes:
    p = Path(__file__).with_name("dbmd_template.bin")
    if p.is_file():
        return p.read_bytes()
    return b""


# ── PCM_24 streaming encoder ──────────────────────────────────────────────────

def _pcm24_bytes_from_float32(arr: np.ndarray, rng: np.random.Generator) -> bytes:
    """Convert float32 [-1, 1] interleaved samples to little-endian PCM_24 bytes.

    TPDF (Triangular Probability Density Function) dither is applied at ±1 LSB
    peak-to-peak before quantization — eliminates the low-level quantization
    harmonics that the previous `sf.write(subtype="PCM_24")` path left in.
    """
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32, copy=False)
    # Two independent uniform draws → triangular distribution after subtraction.
    dith = rng.random(arr.shape, dtype=np.float32) - rng.random(arr.shape, dtype=np.float32)
    full_scale = float(1 << 23)  # 8388608
    scaled = arr * full_scale + dith
    np.clip(scaled, -full_scale, full_scale - 1.0, out=scaled)
    # Round-to-nearest (not truncate) so DC zero with TPDF dither stays at the
    # decorrelated ±1 LSB pattern instead of collapsing to all-zeros via int trunc.
    ints = np.rint(scaled).astype(np.int32).reshape(-1)
    out = np.empty(ints.size * PCM24_BYTES, dtype=np.uint8)
    out[0::3] = (ints & 0xFF).astype(np.uint8)
    out[1::3] = ((ints >> 8) & 0xFF).astype(np.uint8)
    out[2::3] = ((ints >> 16) & 0xFF).astype(np.uint8)
    return out.tobytes()


# ── RIFF / BW64 layout planner ────────────────────────────────────────────────

def _pad4_len(payload_size: int) -> int:
    """Total bytes occupied by a chunk = 8 (header) + payload + 1 (pad if odd)."""
    return 8 + payload_size + (payload_size & 1)


def _compute_riff_layout(
    *,
    n_frames: int,
    n_channels: int,
    fmt_size: int,
    axml_size: int,
    chna_size: int,
    dbmd_size: int,
) -> dict:
    """Pre-compute byte offsets and decide whether BW64 (ds64) is required.

    Returns a dict with `data_payload`, `riff_chunk_size`, `need_bw64`, `sample_count`.
    `need_bw64` is True when the predicted file size or data payload would overflow
    the 32-bit RIFF size field (4 GiB - safety margin).
    """
    data_payload = n_frames * n_channels * PCM24_BYTES
    junk_total = 8 + 64  # JUNK reservation = ds64 reservation when BW64
    body_bytes = (
        4  # "WAVE"
        + _pad4_len(fmt_size)
        + junk_total
        + _pad4_len(data_payload)
        + _pad4_len(axml_size)
        + _pad4_len(chna_size)
    )
    if dbmd_size > 0:
        body_bytes += _pad4_len(dbmd_size)
    riff_chunk_size = body_bytes  # what goes into the 32-bit size field after RIFF id
    need_bw64 = (
        riff_chunk_size > RIFF_MAX_32BIT - RIFF_BW64_SAFETY_MARGIN
        or data_payload > RIFF_MAX_32BIT
    )
    return {
        "data_payload": data_payload,
        "body_bytes": body_bytes,
        "riff_chunk_size": riff_chunk_size,
        "need_bw64": need_bw64,
        "sample_count": int(n_frames),
    }


def _build_ds64_chunk(bw64_size: int, data_size: int, sample_count: int) -> bytes:
    """Return a 72-byte ds64 chunk (8-byte header + 64-byte payload) per EBU Tech 3306.

    Occupies the exact same 72-byte slot as `_build_junk_chunk_64()` so downstream
    chunk offsets are byte-identical whether or not BW64 mode kicks in. The fixed
    fields take 28 bytes (3 × u64 + u32 tableLength=0); the remaining 36 bytes are
    table padding (still legal — readers stop after tableLength*12 bytes).
    """
    payload = (
        struct.pack("<QQQI", bw64_size, data_size, sample_count, 0)  # tableLength = 0
        + b"\x00" * 36                                                # padding → 64 bytes
    )
    assert len(payload) == 64
    return b"ds64" + struct.pack("<I", 64) + payload


def _build_junk_chunk_64() -> bytes:
    return b"JUNK" + struct.pack("<I", 64) + b"\x00" * 64


def _build_fmt_body_pcm24(sample_rate: int, n_channels: int) -> bytes:
    """WAVE_FORMAT_PCM fmt body for 24-bit interleaved PCM."""
    block_align = n_channels * PCM24_BYTES
    return struct.pack(
        "<HHIIHH",
        1,                    # WAVE_FORMAT_PCM
        n_channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        24,
    )


# ── Public streaming writer ──────────────────────────────────────────────────

def embed_axml_chna_into_wav(src_wav: Path, dst_wav: Path, axml: str, chna: bytes) -> None:
    """Stream-encode `src_wav` (any subtype soundfile reads) into a Dolby/Nuendo
    -compatible 24-bit BWF, with axml + chna + optional dbmd.

    Chunk order matches the reference Atmos master layout:
        fmt, ds64-or-JUNK(64), data, axml, chna, [dbmd]

    Memory usage is bounded by PCM_BLOCK_FRAMES (currently 64K frames). For masters
    whose predicted file size exceeds the 32-bit RIFF limit (~4 GiB), the writer
    switches to BW64 + ds64 (EBU Tech 3306) automatically — the 4-byte RIFF/data
    size fields become 0xFFFFFFFF sentinels and the real 64-bit sizes live in ds64.

    Raises:
        ValueError if the source sample rate is not 48000 or 96000 Hz.
        RuntimeError if streamed PCM bytes drift from the pre-computed size.
    """
    src_wav = Path(src_wav)
    dst_wav = Path(dst_wav)
    info = sf.info(str(src_wav))
    if info.samplerate not in (48000, 96000):
        raise ValueError(
            f"ADM BWF: sample rate must be 48000 or 96000 Hz (got {info.samplerate})"
        )
    sr = int(info.samplerate)
    ch = int(info.channels)

    fmt_body = _build_fmt_body_pcm24(sr, ch)
    axml_b = axml.encode("utf-8")
    dbmd = _load_dbmd_template()

    layout = _compute_riff_layout(
        n_frames=int(info.frames),
        n_channels=ch,
        fmt_size=len(fmt_body),
        axml_size=len(axml_b),
        chna_size=len(chna),
        dbmd_size=len(dbmd),
    )

    riff_id = b"BW64" if layout["need_bw64"] else b"RIFF"
    riff_size_field = RIFF_SIZE_SENTINEL if layout["need_bw64"] else layout["riff_chunk_size"]
    data_size_field = RIFF_SIZE_SENTINEL if layout["need_bw64"] else layout["data_payload"]

    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng()

    def _write_chunk(f, cid: bytes, payload: bytes) -> None:
        f.write(cid)
        f.write(struct.pack("<I", len(payload)))
        f.write(payload)
        if len(payload) & 1:
            f.write(b"\x00")

    with dst_wav.open("wb") as f:
        # RIFF/BW64 header
        f.write(riff_id)
        f.write(struct.pack("<I", riff_size_field))
        f.write(b"WAVE")

        # fmt
        _write_chunk(f, b"fmt ", fmt_body)

        # ds64 or JUNK reservation (always 64 bytes total → preserves chunk offsets)
        if layout["need_bw64"]:
            f.write(_build_ds64_chunk(
                bw64_size=layout["riff_chunk_size"],
                data_size=layout["data_payload"],
                sample_count=layout["sample_count"],
            ))
        else:
            f.write(_build_junk_chunk_64())

        # data chunk header + streaming PCM
        f.write(b"data")
        f.write(struct.pack("<I", data_size_field))
        written_bytes = 0
        with sf.SoundFile(str(src_wav), mode="r") as src:
            while True:
                block = src.read(PCM_BLOCK_FRAMES, dtype="float32", always_2d=True)
                if block.shape[0] == 0:
                    break
                np.clip(block, -1.0, 1.0, out=block)
                f.write(_pcm24_bytes_from_float32(block, rng))
                written_bytes += block.shape[0] * ch * PCM24_BYTES
        if written_bytes != layout["data_payload"]:
            raise RuntimeError(
                f"BWF write: streamed PCM bytes {written_bytes} != expected {layout['data_payload']} "
                "(source frame count changed mid-write?)"
            )
        if written_bytes & 1:
            f.write(b"\x00")

        # axml + chna + optional dbmd
        _write_chunk(f, b"axml", axml_b)
        _write_chunk(f, b"chna", chna)
        if dbmd:
            _write_chunk(f, b"dbmd", dbmd)


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def _ab_id_from_ac(ac_id: str, z: int) -> str:
    rest = ac_id.split("_", 1)[1]
    return f"AB_{rest}_{z & 0xFFFFFFFF:08x}"


def _as_object_block(b: ObjectBlock | tuple) -> ObjectBlock:
    """Accept a legacy ``(start, end, x, y, z)`` tuple or a full :class:`ObjectBlock`.

    Keeps every pre-M5.3 caller (which passes position-only 5-tuples) working
    without change while new callers can carry gain/width.
    """
    if isinstance(b, ObjectBlock):
        return b
    gain = float(b[5]) if len(b) > 5 and b[5] is not None else None
    width = float(b[6]) if len(b) > 6 and b[6] is not None else None
    return ObjectBlock(int(b[0]), int(b[1]), float(b[2]), float(b[3]), float(b[4]), gain, width)


def _jump_interpolation_length(sample_rate: float) -> str:
    if abs(sample_rate - 48000.0) < 0.5:
        return "0.005208"
    if abs(sample_rate - 96000.0) < 0.5:
        return f"{500.0 / 96000.0:.9f}"
    return f"{250.0 / max(sample_rate, 1.0):.9f}"


def build_axml_ebu(
    *,
    channel_roles: list[ChannelRole],
    bed_assignments: list[tuple[int, tuple[str, float, float, float]]],
    blocks_per_object: dict[int, list[ObjectBlock | tuple]],
    total_frames: int,
    sample_rate: float,
    programme_name: str = "ADM Recorder",
    object_names: dict[int, str] | None = None,
) -> tuple[str, bytes]:
    """
    Nuendo-compatible ADM: ebuCoreMain → audioFormatExtended, grouped bed object,
    element order matching commercial Atmos masters; lowercase hex IDs.

    ``blocks_per_object`` entries may be legacy ``(start, end, x, y, z)`` tuples or
    :class:`ObjectBlock` carrying per-block ``gain``/``width`` (M5.3). ``object_names``
    overrides ``audioObjectName`` per object channel (else ``Atmos_Obj_{n}``).
    """
    sr = float(sample_rate)
    if int(round(sr)) not in (48000, 96000):
        raise ValueError("sample_rate must be 48000 or 96000")
    names = object_names or {}

    sr_s = str(int(round(sr)))
    bed_set = {b[0] for b in bed_assignments}
    bed_label_by_ch: dict[int, str] = {b[0]: b[1][0] for b in bed_assignments}
    # Preserve per-channel layout xyz so axml writes the active BedLayout's coordinates
    # instead of falling back to the generic _BED_SPEAKER_DOLBY constants.
    bed_xyz_by_ch: dict[int, tuple[float, float, float]] = {
        b[0]: (float(b[1][1]), float(b[1][2]), float(b[1][3])) for b in bed_assignments
    }
    bed_ordered = sorted(bed_set)
    n_ch = len(channel_roles)

    prog_dur = frames_to_smpte_timecode(max(0, total_frames - 1) + 1, sr)
    if total_frames <= 0:
        prog_dur = "00:00:00.00000"

    # Per WAV channel (1-based): ids and labels
    ch_atu: dict[int, str] = {}
    ch_at: dict[int, str] = {}
    ch_ap: dict[int, str] = {}
    ch_ac: dict[int, str] = {}
    ch_as: dict[int, str] = {}
    is_bed_ch: dict[int, bool] = {}

    ap_bed = f"AP_0001{0x1001:04x}"
    for j, ch1 in enumerate(bed_ordered):
        seq = 0x1001 + j
        ch_ac[ch1] = f"AC_0001{seq:04x}"
        ch_as[ch1] = f"AS_0001{seq:04x}"
        ch_at[ch1] = f"AT_0001{seq:04x}_01"
        ch_ap[ch1] = ap_bed
        ch_atu[ch1] = f"ATU_{ch1:08x}"
        is_bed_ch[ch1] = True

    obj_idx = 0
    for ch1 in range(1, n_ch + 1):
        if ch1 in bed_set:
            continue
        obj_idx += 1
        seq = 0x1000 + obj_idx
        ch_ac[ch1] = f"AC_0003{seq:04x}"
        ch_ap[ch1] = f"AP_0003{seq:04x}"
        ch_as[ch1] = f"AS_0003{seq:04x}"
        ch_at[ch1] = f"AT_0003{seq:04x}_01"
        ch_atu[ch1] = f"ATU_{ch1:08x}"
        is_bed_ch[ch1] = False

    afe = ET.Element("audioFormatExtended")

    prog = _sub(
        afe,
        "audioProgramme",
        audioProgrammeID="APR_1001",
        audioProgrammeName=programme_name,
        start="00:00:00.00000",
        end=prog_dur,
    )
    _sub(prog, "audioContentIDRef", "ACO_1001")

    content_refs: list[str] = []
    if bed_ordered:
        content_refs.append("AO_1001")
    oi = 0
    for ch1 in range(1, n_ch + 1):
        if ch1 not in bed_set:
            oi += 1
            content_refs.append(f"AO_{0x100a + oi:04x}")

    cnt = _sub(
        afe,
        "audioContent",
        audioContentID="ACO_1001",
        audioContentName="Atmos_Master_Content",
    )
    for oid in content_refs:
        _sub(cnt, "audioObjectIDRef", oid)
    dlg = ET.SubElement(cnt, "dialogue", {"mixedContentKind": "0"})
    dlg.text = "2"

    if bed_ordered:
        o_bed = _sub(
            afe,
            "audioObject",
            audioObjectID="AO_1001",
            audioObjectName="Atmos_Bed_1",
            start="00:00:00.00000",
            duration=prog_dur,
        )
        _sub(o_bed, "audioPackFormatIDRef", ap_bed)
        for ch1 in bed_ordered:
            _sub(o_bed, "audioTrackUIDRef", ch_atu[ch1])

    oi = 0
    for ch1 in range(1, n_ch + 1):
        if ch1 in bed_set:
            continue
        oi += 1
        ao_id = f"AO_{0x100a + oi:04x}"
        oo = _sub(
            afe,
            "audioObject",
            audioObjectID=ao_id,
            audioObjectName=names.get(ch1) or f"Atmos_Obj_{oi}",
            start="00:00:00.00000",
            duration=prog_dur,
        )
        _sub(oo, "audioPackFormatIDRef", ch_ap[ch1])
        _sub(oo, "audioTrackUIDRef", ch_atu[ch1])

    if bed_ordered:
        pk = _sub(
            afe,
            "audioPackFormat",
            audioPackFormatID=ap_bed,
            audioPackFormatName="AtmosCustomPackFormat1",
            typeDefinition="DirectSpeakers",
            typeLabel="0001",
        )
        for ch1 in bed_ordered:
            _sub(pk, "audioChannelFormatIDRef", ch_ac[ch1])

    for ch1 in range(1, n_ch + 1):
        if ch1 in bed_set:
            continue
        oi = sum(1 for c in range(1, ch1 + 1) if c not in bed_set)
        opk = _sub(
            afe,
            "audioPackFormat",
            audioPackFormatID=ch_ap[ch1],
            audioPackFormatName=f"Atmos_Obj_{oi}",
            typeDefinition="Objects",
            typeLabel="0003",
        )
        _sub(opk, "audioChannelFormatIDRef", ch_ac[ch1])

    for ch1 in range(1, n_ch + 1):
        ac_id = ch_ac[ch1]
        if is_bed_ch[ch1]:
            lbl_spk = bed_label_by_ch.get(ch1, "L")
            spec = _BED_SPEAKER_DOLBY.get(lbl_spk, _BED_SPEAKER_DOLBY["L"])
            ch_name, spk_lbl, default_xyz = spec
            xyz = bed_xyz_by_ch.get(ch1, default_xyz)
            chf = _sub(
                afe,
                "audioChannelFormat",
                audioChannelFormatID=ac_id,
                audioChannelFormatName=ch_name,
                typeDefinition="DirectSpeakers",
                typeLabel="0001",
            )
            bf = _sub(chf, "audioBlockFormat", audioBlockFormatID=_ab_id_from_ac(ac_id, 1))
            _sub(bf, "speakerLabel", spk_lbl)
            _sub(bf, "cartesian", "1")
            _sub(bf, "position", f"{xyz[0]:.10f}", coordinate="X")
            _sub(bf, "position", f"{xyz[1]:.10f}", coordinate="Y")
            _sub(bf, "position", f"{xyz[2]:.10f}", coordinate="Z")
        else:
            oi = sum(1 for c in range(1, ch1 + 1) if c not in bed_set)
            chf = _sub(
                afe,
                "audioChannelFormat",
                audioChannelFormatID=ac_id,
                audioChannelFormatName=f"Atmos_Obj_{oi}",
                typeDefinition="Objects",
                typeLabel="0003",
            )
            blks = blocks_per_object.get(ch1, [])
            if not blks and total_frames > 0:
                blks = [(0, total_frames, 0.0, 0.0, 0.0)]
            jmp = _jump_interpolation_length(sr)
            bi = 1
            for blk in blks:
                b = _as_object_block(blk)
                if b.end_frame <= b.start_frame:
                    continue
                st = frames_to_smpte_timecode(b.start_frame, sr)
                span = max(1e-9, (b.end_frame - b.start_frame) / sr)
                h = int(span // 3600)
                m = int((span % 3600) // 60)
                s = span - h * 3600 - m * 60
                dur_str = f"{h:02d}:{m:02d}:{s:012.9f}"
                bf = _sub(
                    chf,
                    "audioBlockFormat",
                    audioBlockFormatID=_ab_id_from_ac(ac_id, bi),
                    rtime=st,
                    duration=dur_str,
                )
                bi += 1
                _sub(bf, "cartesian", "1")
                _sub(bf, "position", f"{b.x:.8f}", coordinate="X")
                _sub(bf, "position", f"{b.y:.8f}", coordinate="Y")
                _sub(bf, "position", f"{b.z:.8f}", coordinate="Z")
                # M5.3: gain is linear (mute already folded to 0.0 by the
                # combiner); width is the BS.2076 angular extent, forwarded from
                # ADM-OSC unchanged. Both are omitted when never sent, so
                # position-only masters are byte-identical to before.
                if b.gain is not None:
                    _sub(bf, "gain", f"{b.gain:.6f}")
                if b.width is not None:
                    _sub(bf, "width", f"{b.width:.6f}")
                jp = _sub(bf, "jumpPosition", "1")
                jp.set("interpolationLength", jmp)

    for ch1 in range(1, n_ch + 1):
        ac_id = ch_ac[ch1]
        ap_id = ch_ap[ch1]
        as_id = ch_as[ch1]
        at_id = ch_at[ch1]
        if is_bed_ch[ch1]:
            lbl_spk = bed_label_by_ch.get(ch1, "L")
            spec = _BED_SPEAKER_DOLBY.get(lbl_spk, _BED_SPEAKER_DOLBY["L"])
            stream_name = f"PCM_{spec[0]}"
        else:
            oi = sum(1 for c in range(1, ch1 + 1) if c not in bed_set)
            stream_name = f"PCM_Atmos_Obj_{oi}"
        st = _sub(
            afe,
            "audioStreamFormat",
            audioStreamFormatID=as_id,
            audioStreamFormatName=stream_name,
            formatDefinition="PCM",
            formatLabel="0001",
        )
        _sub(st, "audioChannelFormatIDRef", ac_id)
        _sub(st, "audioPackFormatIDRef", ap_id)
        _sub(st, "audioTrackFormatIDRef", at_id)

    for ch1 in range(1, n_ch + 1):
        ac_id = ch_ac[ch1]
        as_id = ch_as[ch1]
        at_id = ch_at[ch1]
        if is_bed_ch[ch1]:
            lbl_spk = bed_label_by_ch.get(ch1, "L")
            spec = _BED_SPEAKER_DOLBY.get(lbl_spk, _BED_SPEAKER_DOLBY["L"])
            tname = f"PCM_{spec[0]}"
        else:
            oi = sum(1 for c in range(1, ch1 + 1) if c not in bed_set)
            tname = f"PCM_Atmos_Obj_{oi}"
        tf = _sub(
            afe,
            "audioTrackFormat",
            audioTrackFormatID=at_id,
            audioTrackFormatName=tname,
            formatDefinition="PCM",
            formatLabel="0001",
        )
        _sub(tf, "audioStreamFormatIDRef", as_id)

    for ch1 in range(1, n_ch + 1):
        tu = _sub(
            afe,
            "audioTrackUID",
            UID=ch_atu[ch1],
            bitDepth="24",
            sampleRate=sr_s,
        )
        _sub(tu, "audioTrackFormatIDRef", ch_at[ch1])
        _sub(tu, "audioPackFormatIDRef", ch_ap[ch1])

    ET.indent(afe, space="\t")
    inner = ET.tostring(afe, encoding="unicode")
    axml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ebuCoreMain xmlns="{EBU_MAIN_NS}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{EBU_MAIN_NS} ebucore.xsd" xml:lang="en">\n'
        "\t<coreMetadata>\n\t\t<format>\n"
        f"{inner}"
        "\t\t</format>\n\t</coreMetadata>\n</ebuCoreMain>\n"
    )

    chna_rows = [(c, ch_atu[c], ch_at[c], ch_ap[c]) for c in range(1, n_ch + 1)]
    return axml, build_chna_bytes(chna_rows)


def finalize_bwf_session(
    *,
    temp_wav: Path,
    out_bwf: Path,
    cmap: ChannelMapState,
    blocks_per_object: dict[int, list[ObjectBlock | tuple]],
    total_frames: int,
    sample_rate: float,
    object_names: dict[int, str] | None = None,
) -> None:
    bed_asg = cmap.bed_assignments_ordered()
    roles = cmap.roles
    axml, chna = build_axml_ebu(
        channel_roles=roles,
        bed_assignments=bed_asg,
        blocks_per_object=blocks_per_object,
        total_frames=total_frames,
        sample_rate=sample_rate,
        object_names=object_names,
    )
    embed_axml_chna_into_wav(temp_wav, out_bwf, axml, chna)
