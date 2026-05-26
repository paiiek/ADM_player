"""M5.4 (headless slice) — engine echo → recorder → BWF soak round-trip.

The real M5.4 soak needs the live spatial_engine binary + audio hardware; this is
the part we *can* drive in CI: the whole M5.2→M5.3 chain end to end, with a fake
engine and a synthetic silent master instead of real audio.

  fake engine (absorbs handshake/heartbeat)
        ▲ handshake/ping
        │
  start_engine_echo_ingest ── ingest socket ◄── echo burst (this test plays engine)
        │ on_xyz / on_meta → TimelineStore
        ▼
  events_to_object_blocks → finalize_bwf_session → BWF → read_axml/read_chna

Echoes are sent in two phases — **all meta first, await ingest, then positions** —
so that with a thread-safe monotonic frame clock every meta frame precedes every
position frame regardless of the ThreadingMixIn server's per-datagram threading.
That makes "every block carries the gain/width" deterministic, not racy.

Assertions: 0-drop (axml object blocks == positions sent), every block carries
gain+width, all object names survive, chna maps all 24 tracks. Headless: no
PySide6, no sounddevice (soundfile only, whose libsndfile ships in the wheel).
"""
from __future__ import annotations

import itertools
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf
from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher

from adm_player.bwf import read_axml, read_chna_mapping
from adm_recorder.bwf_atmos_writer import finalize_bwf_session
from adm_recorder.channel_config import ChannelMapState, ChannelRole
from adm_recorder.engine_echo import start_engine_echo_ingest
from adm_recorder.osc_udp_server import start_threading_osc_udp
from adm_recorder.timeline_store import TimelineStore, events_to_object_blocks

NS = "urn:ebu:metadata-schema:ebuCore_2016"

N_CHANNELS = 24
OBJECT_CHANNELS = list(range(3, 25))  # 22 objects; ch 1,2 = stereo bed
POINTS_PER_OBJECT = 8
GAIN = 0.4
WIDTH = 25.0
SR = 48000.0


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _wait_until(pred: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class TestEngineRecorderE2E(unittest.TestCase):
    def test_echo_to_bwf_zero_drop_with_gain_width_name(self) -> None:
        timeline = TimelineStore()

        # thread-safe monotonic frame clock (per-datagram threads call this)
        counter = itertools.count(0)
        lock = threading.Lock()

        def get_frame() -> int:
            with lock:
                return next(counter)

        # fake engine: just absorb /sys/handshake + /hb/ping so the subscriber
        # has a real destination (no ICMP-unreachable on loopback).
        absorb = Dispatcher()
        absorb.set_default_handler(lambda *_a: None)
        engine_srv, _engine_th = start_threading_osc_udp(("127.0.0.1", 0), absorb)
        self.addCleanup(engine_srv.shutdown)
        engine_port = engine_srv.server_address[1]

        # recorder ingest pipeline (on_xyz straight to timeline → exact 0-drop)
        session = start_engine_echo_ingest(
            bind_host="127.0.0.1",
            listen_port=0,
            engine_host="127.0.0.1",
            engine_port=engine_port,
            get_frame=get_frame,
            on_xyz=timeline.add_cartesian,
            on_meta=timeline.add_meta,
            heartbeat_sec=60.0,  # don't ping during the test
        )
        self.addCleanup(session.close)

        echo = udp_client.SimpleUDPClient("127.0.0.1", session.listen_port)

        # ── phase 1: all meta, then await ingest (→ meta frames < position frames)
        for ch in OBJECT_CHANNELS:
            echo.send_message(f"/adm/obj/{ch}/gain", GAIN)
            echo.send_message(f"/adm/obj/{ch}/width", WIDTH)
            echo.send_message(f"/adm/obj/{ch}/name", f"obj{ch}")
        n_meta = 3 * len(OBJECT_CHANNELS)
        self.assertTrue(
            _wait_until(lambda: len(timeline.snapshot_meta()) == n_meta),
            f"meta ingest short: {len(timeline.snapshot_meta())}/{n_meta}",
        )

        # ── phase 2: position trajectory across all objects
        for k in range(POINTS_PER_OBJECT):
            az = -90.0 + k * (180.0 / POINTS_PER_OBJECT)
            for ch in OBJECT_CHANNELS:
                echo.send_message(f"/adm/obj/{ch}/aed", [az, 0.0, 1.0])
        n_pos = POINTS_PER_OBJECT * len(OBJECT_CHANNELS)
        self.assertTrue(
            _wait_until(lambda: len(timeline.snapshot()) == n_pos),
            f"position ingest short (UDP drop?): {len(timeline.snapshot())}/{n_pos}",
        )
        session.close()  # stop the heartbeat before we leave the network phase

        # ── finalize: enriched blocks → synthetic 24ch master → BWF
        events = timeline.snapshot()
        metas = timeline.snapshot_meta()
        total_frames = max(e.frame for e in events) + 10
        blocks_by_ch, names_by_ch = events_to_object_blocks(events, metas, total_frames, SR)

        cm = ChannelMapState(n_channels=N_CHANNELS)
        cm.bed_layout_id = "stereo"
        cm.roles = [ChannelRole.BED, ChannelRole.BED] + [ChannelRole.OBJECT] * 22
        blocks: dict[int, list] = {}
        object_names: dict[int, str] = {}
        for r, role in enumerate(cm.roles):
            ch1 = r + 1
            if role != ChannelRole.OBJECT:
                continue
            blocks[ch1] = blocks_by_ch.get(ch1, [])
            if ch1 in names_by_ch:
                object_names[ch1] = names_by_ch[ch1]

        td = Path(tempfile.mkdtemp())
        src = td / "master.wav"
        sf.write(
            str(src),
            np.zeros((total_frames, N_CHANNELS), dtype=np.float32),
            int(SR),
            subtype="FLOAT",
        )
        dst = td / "master_adm.wav"
        finalize_bwf_session(
            temp_wav=src,
            out_bwf=dst,
            cmap=cm,
            blocks_per_object=blocks,
            total_frames=total_frames,
            sample_rate=SR,
            object_names=object_names,
        )

        # ── parse the embedded axml/chna back and assert the round-trip
        root = ET.fromstring(read_axml(dst).encode("utf-8"))
        obj_acfs = [
            acf
            for acf in root.iter(_q("audioChannelFormat"))
            if acf.get("typeDefinition") == "Objects"
        ]
        self.assertEqual(len(obj_acfs), len(OBJECT_CHANNELS))

        total_blocks = n_gain = n_width = 0
        for acf in obj_acfs:
            bfs = acf.findall(_q("audioBlockFormat"))
            total_blocks += len(bfs)
            for bf in bfs:
                if bf.find(_q("gain")) is not None:
                    n_gain += 1
                if bf.find(_q("width")) is not None:
                    n_width += 1

        # 0-drop: every echoed position became exactly one axml block …
        self.assertEqual(total_blocks, n_pos)
        # … and every block carries the gain/width the engine echoed
        self.assertEqual(n_gain, n_pos)
        self.assertEqual(n_width, n_pos)

        first_gain = obj_acfs[0].find(_q("audioBlockFormat")).find(_q("gain"))
        first_width = obj_acfs[0].find(_q("audioBlockFormat")).find(_q("width"))
        self.assertAlmostEqual(float(first_gain.text), GAIN, places=5)
        self.assertAlmostEqual(float(first_width.text), WIDTH, places=5)

        # names survive end to end
        names = {o.get("audioObjectName") for o in root.iter(_q("audioObject"))}
        for ch in OBJECT_CHANNELS:
            self.assertIn(f"obj{ch}", names)

        # chna maps all 24 tracks
        self.assertEqual(len(read_chna_mapping(dst)), N_CHANNELS)


if __name__ == "__main__":
    unittest.main()
