"""M5.3 — gain / width / mute / name land in the axml audioBlockFormat.

These build the axml directly (no WAV embed needed) and parse it namespace-aware,
asserting the non-positional ADM fields the engine echoes (M5.2) are written into
``audioBlockFormat`` / ``audioObjectName``. Headless: no PySide6, no sounddevice.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from adm_recorder.bwf_atmos_writer import build_axml_ebu
from adm_recorder.channel_config import ChannelMapState, ChannelRole
from adm_recorder.timeline_store import (
    MetaEvent,
    ObjectBlock,
    PositionEvent,
    events_to_object_blocks,
)

NS = "urn:ebu:metadata-schema:ebuCore_2016"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


class TestAxmlMeta(unittest.TestCase):
    def _build(self, blocks, object_names=None) -> ET.Element:
        """3 tracks: stereo bed + 1 object on ch 3; return parsed axml root."""
        cm = ChannelMapState(n_channels=3)
        cm.bed_layout_id = "stereo"
        cm.roles = [ChannelRole.BED, ChannelRole.BED, ChannelRole.OBJECT]
        axml, _chna = build_axml_ebu(
            channel_roles=cm.roles,
            bed_assignments=cm.bed_assignments_ordered(),
            blocks_per_object=blocks,
            total_frames=200,
            sample_rate=48000.0,
            object_names=object_names,
        )
        # bytes input: the axml carries an encoding declaration, which ET rejects
        # from a str.
        return ET.fromstring(axml.encode("utf-8"))

    def _object_block_formats(self, root: ET.Element) -> list[ET.Element]:
        for acf in root.iter(_q("audioChannelFormat")):
            if acf.get("typeDefinition") == "Objects":
                return acf.findall(_q("audioBlockFormat"))
        return []

    def test_gain_emitted_per_block(self) -> None:
        blocks = {
            3: [
                ObjectBlock(0, 100, 0.1, 0.2, 0.3, gain=0.5, width=None),
                ObjectBlock(100, 200, 0.4, 0.5, 0.6, gain=0.25, width=None),
            ]
        }
        bfs = self._object_block_formats(self._build(blocks))
        self.assertEqual(len(bfs), 2)
        gains = [float(bf.find(_q("gain")).text) for bf in bfs]
        self.assertEqual(gains, [0.5, 0.25])
        # width omitted when None (no empty element)
        self.assertTrue(all(bf.find(_q("width")) is None for bf in bfs))

    def test_width_emitted_and_gain_omitted_when_absent(self) -> None:
        blocks = {3: [ObjectBlock(0, 200, 0.0, 1.0, 0.0, gain=None, width=30.0)]}
        bf = self._object_block_formats(self._build(blocks))[0]
        w = bf.find(_q("width"))
        self.assertIsNotNone(w)
        self.assertAlmostEqual(float(w.text), 30.0, places=5)
        self.assertIsNone(bf.find(_q("gain")))

    def test_mute_becomes_gain_zero(self) -> None:
        # mute at frame 50 → the block that starts after it renders gain 0.0,
        # while the earlier block keeps the explicit 0.8.
        pos = [
            PositionEvent(3, 0, 0.0, 1.0, 0.0),
            PositionEvent(3, 100, 0.1, 0.9, 0.0),
        ]
        meta = [MetaEvent(3, 0, "gain", 0.8), MetaEvent(3, 50, "mute", 1)]
        blocks_by_ch, _names = events_to_object_blocks(pos, meta, 200, 48000.0)
        bfs = self._object_block_formats(self._build({3: blocks_by_ch[3]}))
        gains = [float(bf.find(_q("gain")).text) for bf in bfs]
        self.assertEqual(gains, [0.8, 0.0])

    def test_object_name_emitted_once(self) -> None:
        root = self._build(
            {3: [ObjectBlock(0, 200, 0.0, 1.0, 0.0)]}, object_names={3: "dog"}
        )
        names = [o.get("audioObjectName") for o in root.iter(_q("audioObject"))]
        self.assertEqual(names.count("dog"), 1)  # exactly once, on the object
        self.assertIn("Atmos_Bed_1", names)  # bed object name untouched

    def test_no_meta_is_position_only(self) -> None:
        """Legacy 5-tuple path stays byte-clean: no gain/width elements."""
        bf = self._object_block_formats(self._build({3: [(0, 200, 0.0, 1.0, 0.0)]}))[0]
        self.assertIsNone(bf.find(_q("gain")))
        self.assertIsNone(bf.find(_q("width")))
        self.assertIsNotNone(bf.find(_q("position")))


if __name__ == "__main__":
    unittest.main()
