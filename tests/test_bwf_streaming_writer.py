"""C3 + C4 regression: BWF streaming writer.

Covers what was broken before this cycle:
  - C3: full RIFF buffered into RAM (~6.2 GiB for 30-min 24-ch master) and the
        32-bit RIFF size field silently overflowed past 4 GiB.
        Fixed by chunk-streaming PCM and switching to BW64 + ds64 above 4 GiB.
  - C4: float32 → PCM_24 conversion had no dither, producing low-level
        quantization harmonics on quiet content. Fixed with TPDF dither at ±1 LSB.

Tests target small synthetic WAVs plus a monkeypatched 4-GiB sentinel so the
BW64 branch is exercised without writing an actual multi-GiB file.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from adm_recorder.bwf_atmos_writer import (
    PCM24_BYTES,
    RIFF_MAX_32BIT,
    RIFF_SIZE_SENTINEL,
    _build_ds64_chunk,
    _build_junk_chunk_64,
    _compute_riff_layout,
    _pcm24_bytes_from_float32,
    embed_axml_chna_into_wav,
)


# ── PCM_24 encoder (C4: dither + accurate quantization) ─────────────────────

def _decode_pcm24_le(raw: bytes) -> np.ndarray:
    """Decode little-endian PCM_24 bytes back to signed int32 samples."""
    buf = np.frombuffer(raw, dtype=np.uint8)
    ints = (
        buf[0::3].astype(np.int32)
        | (buf[1::3].astype(np.int32) << 8)
        | (buf[2::3].astype(np.int32) << 16)
    )
    return np.where(ints & 0x800000, ints | ~0xFFFFFF, ints)


def test_pcm24_full_scale_quantizes_to_extremes() -> None:
    rng = np.random.default_rng(seed=0)
    arr = np.array([[1.0], [-1.0]] * 1024, dtype=np.float32)
    ints = _decode_pcm24_le(_pcm24_bytes_from_float32(arr, rng))
    plus = ints[0::2]
    minus = ints[1::2]
    # +1.0 should land at full_scale - 1 = 8388607 (with TPDF jitter ±1).
    assert plus.min() >= (1 << 23) - 2, f"+1.0 quantized too low: min={plus.min()}"
    # -1.0 should land at -full_scale = -8388608 (with TPDF jitter ±1).
    assert minus.max() <= -(1 << 23) + 1, f"-1.0 quantized too high: max={minus.max()}"


def test_pcm24_dither_present_at_dc_zero() -> None:
    """DC 0 input must not produce all-zero PCM (otherwise dither is absent).
    TPDF (±1 LSB) should give a mean near 0 with bounded peaks."""
    rng = np.random.default_rng(seed=0)
    arr = np.zeros((8192, 1), dtype=np.float32)
    ints = _decode_pcm24_le(_pcm24_bytes_from_float32(arr, rng))
    assert np.any(ints != 0), "TPDF dither absent — DC zero produced exact zeros"
    # TPDF mean is 0 in the limit; with 8K samples the empirical mean is tight.
    assert abs(float(ints.mean())) < 0.2, f"dither mean should ≈ 0; got {ints.mean()}"
    assert ints.max() <= 1 and ints.min() >= -1, (
        f"TPDF should clamp to ±1 LSB; got [{ints.min()}, {ints.max()}]"
    )


# ── layout planner (C3: 4 GiB boundary) ──────────────────────────────────────

def test_layout_planner_small_master_stays_riff() -> None:
    layout = _compute_riff_layout(
        n_frames=48_000, n_channels=8,
        fmt_size=16, axml_size=10_000, chna_size=500, dbmd_size=0,
    )
    assert not layout["need_bw64"]
    assert layout["data_payload"] == 48_000 * 8 * 3
    assert layout["sample_count"] == 48_000


def test_layout_planner_30min_24ch_switches_to_bw64() -> None:
    """The exact case the original review flagged: 24 ch × 30 min ≈ 6.2 GiB."""
    layout = _compute_riff_layout(
        n_frames=48_000 * 60 * 30, n_channels=24,
        fmt_size=16, axml_size=10_000, chna_size=500, dbmd_size=0,
    )
    assert layout["need_bw64"]
    assert layout["data_payload"] > RIFF_MAX_32BIT


def test_layout_planner_respects_safety_margin() -> None:
    """A file whose total is just under the sentinel must still switch to BW64
    (1 MiB safety margin) — otherwise the 32-bit size field would land at
    0xFFFFFFFF-ish and confuse picky readers."""
    target_payload = RIFF_MAX_32BIT - (512 << 10)   # 512 KiB headroom only
    n_channels = 8
    n_frames = target_payload // (n_channels * PCM24_BYTES)
    layout = _compute_riff_layout(
        n_frames=n_frames, n_channels=n_channels,
        fmt_size=16, axml_size=1024, chna_size=128, dbmd_size=0,
    )
    assert layout["need_bw64"], "must switch within the 1 MiB safety margin"


# ── ds64 + JUNK chunk byte layout ─────────────────────────────────────────────

def test_ds64_chunk_is_72_bytes_with_decodable_fields() -> None:
    ds64 = _build_ds64_chunk(bw64_size=0x1_0000_0000, data_size=0x0_FFFF_FFF0, sample_count=12345)
    assert len(ds64) == 72  # 8 header + 64 payload, matches JUNK slot
    assert ds64[:4] == b"ds64"
    assert struct.unpack("<I", ds64[4:8])[0] == 64
    bw64s, datas, sc, tbl_len = struct.unpack("<QQQI", ds64[8:36])
    assert bw64s == 0x1_0000_0000
    assert datas == 0x0_FFFF_FFF0
    assert sc == 12345
    assert tbl_len == 0
    assert ds64[36:72] == b"\x00" * 36   # table-area padding (tableLength=0 → readers skip)


def test_junk_chunk_matches_ds64_slot_size() -> None:
    """JUNK and ds64 must occupy the exact same 72-byte slot (8 header + 64 payload)
    so chunk offsets don't shift when the writer flips between modes."""
    assert len(_build_junk_chunk_64()) == len(_build_ds64_chunk(0, 0, 0))
    assert len(_build_junk_chunk_64()) == 72


# ── End-to-end: RIFF path ────────────────────────────────────────────────────

def _write_synthetic_float_wav(path: Path, n_frames: int, n_ch: int, sr: int = 48000) -> None:
    data = (np.random.rand(n_frames, n_ch).astype(np.float32) - 0.5) * 0.8
    sf.write(str(path), data, sr, subtype="FLOAT")


def _walk_chunks(raw: bytes, max_chunks: int = 20) -> list[tuple[str, int, int]]:
    """Walk top-level RIFF chunks (naïve 32-bit). Stops at the first unrecognized id
    or after `max_chunks` to avoid runaway parsing on BW64 sentinels."""
    out: list[tuple[str, int, int]] = []
    pos = 12
    while pos + 8 <= len(raw) and len(out) < max_chunks:
        cid = raw[pos:pos+4].decode("ascii", "replace")
        csize = struct.unpack("<I", raw[pos+4:pos+8])[0]
        out.append((cid, pos, csize))
        if cid == "data":
            break  # data size may be a BW64 sentinel — stop walking past it.
        pos += 8 + csize + (csize & 1)
    return out


def test_riff_path_produces_expected_chunk_order(tmp_path: Path) -> None:
    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_synthetic_float_wav(src, n_frames=4800, n_ch=4)
    embed_axml_chna_into_wav(src, dst, "<?xml version='1.0'?><doc/>", b"\x00\x00\x00\x00")

    raw = dst.read_bytes()
    assert raw[:4] == b"RIFF"
    assert raw[8:12] == b"WAVE"
    ids = [cid for cid, _, _ in _walk_chunks(raw)]
    assert ids[0] == "fmt "
    assert ids[1] in ("JUNK", "ds64")
    assert ids[2] == "data"


def test_riff_data_chunk_size_matches_input_frames(tmp_path: Path) -> None:
    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    n_frames, n_ch = 16_384, 6
    _write_synthetic_float_wav(src, n_frames=n_frames, n_ch=n_ch)
    embed_axml_chna_into_wav(src, dst, "<x/>", b"\x00\x00\x00\x00")

    raw = dst.read_bytes()
    for cid, _, csize in _walk_chunks(raw):
        if cid == "data":
            assert csize == n_frames * n_ch * PCM24_BYTES
            return
    pytest.fail("no data chunk in output")


def test_riff_round_trip_preserves_axml_and_chna(tmp_path: Path) -> None:
    """The existing test_adm_recorder covers small writes; this asserts that the
    new streaming writer still produces an axml that the player's reader can find."""
    from adm_player.bwf import iter_riff_chunks

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_synthetic_float_wav(src, n_frames=240, n_ch=2)
    axml_in = "<?xml version='1.0'?><root>roundtrip</root>"
    chna_in = b"abcd1234"
    embed_axml_chna_into_wav(src, dst, axml_in, chna_in)

    found_axml = None
    found_chna = None
    for ch in iter_riff_chunks(dst):
        if ch.chunk_id == "axml":
            found_axml = ch.data
        elif ch.chunk_id == "chna":
            found_chna = ch.data
    assert found_axml is not None and found_axml.decode("utf-8") == axml_in
    assert found_chna == chna_in


# ── End-to-end: BW64 path (forced via monkeypatch) ───────────────────────────

def test_bw64_path_emits_ds64_and_sentinel_data_size(tmp_path: Path, monkeypatch) -> None:
    """Force BW64 by shrinking the size sentinel so anything > 200 bytes triggers
    the upgrade. Verifies header, ds64 fields, and the 0xFFFFFFFF-style sentinel
    in the legacy data chunk size field."""
    monkeypatch.setattr("adm_recorder.bwf_atmos_writer.RIFF_MAX_32BIT", 200)

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    n_frames, n_ch = 256, 2
    _write_synthetic_float_wav(src, n_frames=n_frames, n_ch=n_ch)
    embed_axml_chna_into_wav(src, dst, "<x/>", b"\x00\x00\x00\x00")

    raw = dst.read_bytes()
    # Header upgraded to BW64; RIFF/file size field becomes the 0xFFFFFFFF sentinel.
    assert raw[:4] == b"BW64", f"expected BW64 header; got {raw[:4]!r}"
    assert struct.unpack("<I", raw[4:8])[0] == RIFF_SIZE_SENTINEL, "riff size field must equal 0xFFFFFFFF sentinel"
    assert raw[8:12] == b"WAVE"

    # Walk to the ds64 chunk and the legacy data header.
    pos = 12
    found_ds64 = False
    data_size_field = None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos+4]
        csize = struct.unpack("<I", raw[pos+4:pos+8])[0]
        if cid == b"ds64":
            found_ds64 = True
            payload = raw[pos+8:pos+8+csize]
            bw64s, datas, sc, tbl_len = struct.unpack("<QQQI", payload[:28])
            assert datas == n_frames * n_ch * PCM24_BYTES, (
                f"ds64 dataSize ({datas}) must equal actual PCM bytes"
            )
            assert sc == n_frames, f"ds64 sampleCount ({sc}) must equal frame count"
            assert tbl_len == 0
            assert bw64s > 0
        if cid == b"data":
            data_size_field = csize
            break  # don't follow the sentinel past data
        pos += 8 + csize + (csize & 1)

    assert found_ds64, "BW64 mode must include a ds64 chunk"
    assert data_size_field == RIFF_SIZE_SENTINEL, (
        f"legacy data size field must be the 0xFFFFFFFF sentinel; got {data_size_field}"
    )


# ── BW64 reader: self round-trip via adm_player.bwf.iter_riff_chunks ─────────

def test_bw64_round_trip_reader_recovers_axml_and_chna(tmp_path: Path, monkeypatch) -> None:
    """The writer can emit BW64+ds64 for >4GB masters. Verify the player's reader
    correctly parses ds64 sentinel-overridden chunk sizes and recovers axml+chna
    intact — without this round-trip working, large masters become read-only artifacts."""
    from adm_player.bwf import iter_riff_chunks, read_axml

    # Force BW64 mode via the threshold (sentinel stays 0xFFFFFFFF — wire format intact).
    monkeypatch.setattr("adm_recorder.bwf_atmos_writer.RIFF_MAX_32BIT", 200)

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    n_frames, n_ch = 1024, 4
    _write_synthetic_float_wav(src, n_frames=n_frames, n_ch=n_ch)
    axml_in = "<?xml version='1.0'?><root>bw64-roundtrip</root>"
    chna_in = b"chna-payload-bytes"
    embed_axml_chna_into_wav(src, dst, axml_in, chna_in)

    # Sanity: the file actually went through the BW64 branch (otherwise the test is moot).
    assert dst.read_bytes()[:4] == b"BW64"

    # Walk chunks: the reader must transparently substitute ds64 sizes for the
    # 0xFFFFFFFF sentinels so axml/chna land where they should and have correct lengths.
    chunks = {ch.chunk_id: ch.data for ch in iter_riff_chunks(dst)}
    assert "ds64" not in chunks, "ds64 is parsed internally and must not be yielded as a regular chunk"
    assert "data" in chunks, "data chunk missing from BW64 round-trip"
    assert len(chunks["data"]) == n_frames * n_ch * PCM24_BYTES, (
        f"data chunk size mismatch: got {len(chunks['data'])}, want {n_frames * n_ch * PCM24_BYTES}"
    )
    assert chunks["axml"].decode("utf-8") == axml_in
    assert chunks["chna"] == chna_in

    # The high-level helper must also work (covers the iter_riff_chunks + decode path).
    assert read_axml(dst) == axml_in


def test_bw64_reader_rejects_missing_ds64() -> None:
    """A BW64-magic file without a leading ds64 chunk is malformed — reader must raise."""
    from io import BytesIO

    import pytest as _pytest

    from adm_player.bwf import iter_riff_chunks

    # Build a synthetic BW64 with a fmt chunk where ds64 should be.
    buf = BytesIO()
    buf.write(b"BW64" + struct.pack("<I", RIFF_SIZE_SENTINEL) + b"WAVE")
    buf.write(b"fmt " + struct.pack("<I", 4) + b"\x00\x00\x00\x00")
    path = Path("/tmp/bw64_malformed_no_ds64.wav")
    path.write_bytes(buf.getvalue())
    try:
        with _pytest.raises(ValueError, match="ds64"):
            list(iter_riff_chunks(path))
    finally:
        path.unlink(missing_ok=True)


def test_bw64_reader_handles_chunk_size_override_table(tmp_path: Path) -> None:
    """ds64 carries an override table for non-`data` chunks that overflow 32-bit.
    Build a synthetic file where an oversized `axml` lives behind a sentinel and is
    resolved through the override table — covers the table-lookup path in the reader."""
    from adm_player.bwf import iter_riff_chunks

    # Synthetic axml payload + small data; force axml to be the chunk whose real size
    # comes from the override table.
    axml_payload = b"<x>" + (b"A" * 64) + b"</x>"
    fmt_body = struct.pack("<HHIIHH", 1, 1, 48000, 48000 * 3, 3, 24)
    data_payload = b"\x00\x00\x00" * 16

    # ds64: bw64Size set later, dataSize = real data size, sampleCount = data frames,
    # tableLength = 1, table: ("axml", len(axml_payload)).
    ds64_payload = (
        struct.pack("<QQQI", 0, len(data_payload), 16, 1)
        + b"axml" + struct.pack("<Q", len(axml_payload))
    )
    # Pad ds64 payload to a stable size; tableLength=1 → 28 + 12 = 40 bytes (no padding needed).
    ds64_chunk = b"ds64" + struct.pack("<I", len(ds64_payload)) + ds64_payload

    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
    data_chunk = b"data" + struct.pack("<I", RIFF_SIZE_SENTINEL) + data_payload + (b"\x00" if len(data_payload) & 1 else b"")
    axml_chunk_hdr = b"axml" + struct.pack("<I", RIFF_SIZE_SENTINEL)  # sentinel — real size from ds64 table

    body = fmt_chunk + ds64_chunk + data_chunk + axml_chunk_hdr + axml_payload
    if len(axml_payload) & 1:
        body += b"\x00"

    header = b"BW64" + struct.pack("<I", RIFF_SIZE_SENTINEL) + b"WAVE"
    # Reconstruct ds64 with proper bw64Size.
    bw64_size = 4 + len(fmt_chunk) + len(ds64_chunk) + len(data_chunk) + len(axml_chunk_hdr) + len(axml_payload) + (len(axml_payload) & 1)
    ds64_payload_real = (
        struct.pack("<QQQI", bw64_size, len(data_payload), 16, 1)
        + b"axml" + struct.pack("<Q", len(axml_payload))
    )
    ds64_chunk_real = b"ds64" + struct.pack("<I", len(ds64_payload_real)) + ds64_payload_real
    body = fmt_chunk + ds64_chunk_real + data_chunk + axml_chunk_hdr + axml_payload
    if len(axml_payload) & 1:
        body += b"\x00"

    path = tmp_path / "bw64_with_override.wav"
    path.write_bytes(header + body)

    chunks = {ch.chunk_id: ch.data for ch in iter_riff_chunks(path)}
    assert chunks["fmt "] == fmt_body
    assert chunks["data"] == data_payload
    assert chunks["axml"] == axml_payload
