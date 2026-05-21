"""ADM Recorder: smoke test for axml build and RIFF embed."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from adm_player.bwf import read_axml, read_chna_mapping
from adm_recorder.bwf_atmos_writer import build_axml_ebu, embed_axml_chna_into_wav
from adm_recorder.channel_config import ChannelMapState, ChannelRole


class TestAdmRecorderBwf(unittest.TestCase):
    def test_embed_and_read_axml_chna(self) -> None:
        cm = ChannelMapState(n_channels=3)
        cm.bed_layout_id = "stereo"
        cm.roles = [ChannelRole.BED, ChannelRole.BED, ChannelRole.OBJECT]
        bed = cm.bed_assignments_ordered()
        axml, chna = build_axml_ebu(
            channel_roles=cm.roles,
            bed_assignments=bed,
            blocks_per_object={3: [(0, 96, -0.25, 0.5, 0.1)]},
            total_frames=96,
            sample_rate=48000.0,
        )
        self.assertIn("audioTrackUID", axml)
        self.assertIn('UID="ATU_00000001"', axml)
        self.assertIn("audioTrackFormatIDRef", axml)
        self.assertIn("urn:ebu:metadata-schema:ebuCore_2016", axml)
        self.assertIn("ebuCoreMain", axml)
        self.assertIn("audioFormatExtended", axml)
        self.assertIn("AO_1001", axml)
        self.assertIn("AO_100b", axml)
        self.assertIn(b"AT_", chna)
        self.assertIn(b"AP_", chna)
        td = Path(tempfile.mkdtemp())
        src = td / "in.wav"
        sf.write(str(src), np.zeros((96, 3), dtype=np.float32), 48000, subtype="FLOAT")
        dst = td / "out.wav"
        embed_axml_chna_into_wav(src, dst, axml, chna)
        self.assertIn("AO_100b", read_axml(dst))
        # Dolby §7.1: PCM 24-bit in fmt
        fmt_body = next(c.data for c in __import__(
            "adm_player.bwf", fromlist=["iter_riff_chunks"]
        ).iter_riff_chunks(dst) if c.chunk_id == "fmt ")
        self.assertEqual(len(fmt_body) >= 16, True)
        audio_format = int.from_bytes(fmt_body[0:2], "little")
        bits = int.from_bytes(fmt_body[14:16], "little")
        self.assertEqual(audio_format, 1)
        self.assertEqual(bits, 24)
        m = read_chna_mapping(dst)
        self.assertEqual(m.get("ATU_00000001"), 0)
        self.assertEqual(m.get("ATU_00000003"), 2)


if __name__ == "__main__":
    unittest.main()
