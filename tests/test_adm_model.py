import struct
import tempfile
import unittest
from pathlib import Path

from adm_player.adm_model import active_block, parse_adm_objects
from adm_player.bwf import read_axml, read_chna_mapping


MINIMAL_ADM = """<?xml version="1.0" encoding="UTF-8"?>
<ebuCore xmlns="urn:ebu:metadata:schema:ebuCore_2018">
  <audioPackFormat audioPackFormatID="Pack_01" typeLabel="0000" typeDefinition="Objects">
    <audioChannelFormatIDRef>Ch_01</audioChannelFormatIDRef>
  </audioPackFormat>
  <audioChannelFormat audioChannelFormatID="Ch_01" typeLabel="0000" typeDefinition="Objects">
    <audioBlockFormat rtime="00:00:00.000000" duration="00:00:01.000000">
      <position azimuth="30" elevation="5" distance="1"/>
    </audioBlockFormat>
  </audioChannelFormat>
  <audioObject audioObjectID="Obj1" typeDefinition="Objects">
    <audioPackFormatIDRef>Pack_01</audioPackFormatIDRef>
    <audioTrackUIDRef>ATU_00000001</audioTrackUIDRef>
  </audioObject>
</ebuCore>
"""

# Dolby Atmos master style: typeDefinition on audioPackFormat; audioObjectName; coordinate positions
DOLBY_STYLE_ADM = """<?xml version="1.0" encoding="UTF-8"?>
<ebuCore xmlns="urn:ebu:metadata:schema:ebuCore_2018">
  <audioPackFormat audioPackFormatID="AP_00031001" typeDefinition="Objects" typeLabel="0003">
    <audioChannelFormatIDRef>AC_00031001</audioChannelFormatIDRef>
  </audioPackFormat>
  <audioChannelFormat audioChannelFormatID="AC_00031001" typeDefinition="Objects">
    <audioBlockFormat rtime="00:00:00.00000" duration="00:00:01.00000">
      <cartesian>1</cartesian>
      <position coordinate="X">-0.5</position>
      <position coordinate="Y">0.25</position>
      <position coordinate="Z">0.0</position>
    </audioBlockFormat>
  </audioChannelFormat>
  <audioObject audioObjectID="AO_100b" audioObjectName="Atmos_Obj_1">
    <audioPackFormatIDRef>AP_00031001</audioPackFormatIDRef>
    <audioTrackUIDRef>ATU_0000000b</audioTrackUIDRef>
  </audioObject>
</ebuCore>
"""


def _build_wav_with_axml(
    axml: str,
    num_frames: int = 48,
    channels: int = 2,
    sr: int = 48000,
    chna_extra: bytes | None = None,
) -> bytes:
    axml_b = axml.encode("utf-8")
    # 16-bit PCM interleaved silence
    samples = num_frames * channels
    pcm = struct.pack("<" + "h" * samples, *([0] * samples))
    fmt = struct.pack(
        "<IHHIIHH",
        16,
        1,
        channels,
        sr,
        sr * channels * 2,
        channels * 2,
        16,
    )
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    axml_chunk = b"axml" + struct.pack("<I", len(axml_b)) + axml_b
    if len(axml_b) & 1:
        axml_chunk += b"\x00"
    extra = b""
    if chna_extra is not None:
        chna_chunk = b"chna" + struct.pack("<I", len(chna_extra)) + chna_extra
        if len(chna_extra) & 1:
            chna_chunk += b"\x00"
        extra = chna_chunk
    data_chunk = b"data" + struct.pack("<I", len(pcm)) + pcm
    if len(pcm) & 1:
        data_chunk += b"\x00"
    riff_body = b"WAVE" + fmt_chunk + axml_chunk + extra + data_chunk
    if len(fmt_chunk) & 1 or len(axml_chunk) & 1 or len(data_chunk) & 1:
        pass
    riff = b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body
    return riff


class TestAdmModel(unittest.TestCase):
    def test_parse_object_and_block(self) -> None:
        objs = parse_adm_objects(MINIMAL_ADM, 48000.0, {"ATU_00000001": 0})
        self.assertEqual(len(objs), 1)
        o = objs[0]
        self.assertEqual(o.adm_index, 1)
        self.assertEqual(o.wav_channels, [0])
        self.assertEqual(len(o.blocks), 1)
        b = o.blocks[0]
        self.assertAlmostEqual(b.start_sec, 0.0)
        self.assertGreater(b.end_sec, 0.5)
        self.assertEqual(b.position.mode, "polar")
        self.assertAlmostEqual(b.position.azimuth or 0.0, 30.0)
        self.assertAlmostEqual(b.position.elevation or 0.0, 5.0)

    def test_active_block(self) -> None:
        objs = parse_adm_objects(MINIMAL_ADM, 48000.0, {})
        blk = active_block(objs[0].blocks, 0.5)
        self.assertIsNotNone(blk)

    def test_osc_object_index_matches_wav_channel_1based(self) -> None:
        """chna 기준 WAV 채널(1-based) = /adm/obj/N."""
        objs = parse_adm_objects(DOLBY_STYLE_ADM, 48000.0, {"ATU_0000000B": 10})
        self.assertEqual(objs[0].osc_object_index, 11)

    def test_dolby_pack_type_and_coordinate_position(self) -> None:
        objs = parse_adm_objects(DOLBY_STYLE_ADM, 48000.0, {})
        self.assertEqual(len(objs), 1)
        o = objs[0]
        self.assertEqual(o.label, "Atmos_Obj_1")
        self.assertEqual(o.type_definition, "Objects")
        self.assertEqual(o.blocks[0].position.mode, "cartesian")
        self.assertAlmostEqual(o.blocks[0].position.x or 0.0, -0.5)
        self.assertAlmostEqual(o.blocks[0].position.y or 0.0, 0.25)


def _chna_40(track_idx: int, uid_ascii: bytes) -> bytes:
    """한 개의 EBU audioID 레코드(40바이트)."""
    uid = uid_ascii.ljust(12, b"\x00")[:12]
    return (
        struct.pack("<H", track_idx)
        + uid
        + b"\x00" * 14
        + b"\x00" * 11
        + b"\x00"
    )


class TestBwf(unittest.TestCase):
    def test_chna_ebu_40byte_layout(self) -> None:
        """BS.2088/EBU: 슬롯당 40바이트(구형 14바이트 파서와 구분)."""
        chna_body = struct.pack("<HH", 118, 110) + _chna_40(11, b"ATU_0000000b") + _chna_40(1, b"ATU_00000001")
        raw = _build_wav_with_axml(MINIMAL_ADM, chna_extra=chna_body)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(raw)
            path = Path(tmp.name)
        try:
            m = read_chna_mapping(path)
            self.assertEqual(m.get("ATU_0000000B"), 10)
            self.assertEqual(m.get("ATU_00000001"), 0)
        finally:
            path.unlink(missing_ok=True)

    def test_read_axml_roundtrip(self) -> None:
        raw = _build_wav_with_axml(MINIMAL_ADM)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(raw)
            path = Path(tmp.name)
        try:
            xml = read_axml(path)
            self.assertIn("audioObject", xml)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
