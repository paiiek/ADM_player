"""TRACK A3 (ADR 0029) — U5 tests for `MetaRecordBuilder`.

Covers (a) coordinate/gain/width PARITY: for the same block, the sidecar
record's a0/a1/a2/gain/width equal the `AdmOscEmitter` payload bit-for-bit
(AC6, ADR0006 az-flip/offset + distance ÷20); (b) the dedup rule mirrors the
OSC emitter (only changed state emits a record).
"""

from __future__ import annotations

from adm_player.adm_meta import MetaRecordBuilder
from adm_player.adm_model import AdmObject, ObjectBlock, ObjectPosition
from adm_player.meta_sink import META_COORD_CART, META_COORD_POLAR, META_FLAG_ACTIVE, META_FLAG_MUTE
from adm_player.osc_emit import AdmOscEmitter


def _polar_obj(oi: int, az: float, el: float, dist: float, gain=None, width=None) -> AdmObject:
    pos = ObjectPosition(mode="polar", azimuth=az, elevation=el, distance=dist)
    blk = ObjectBlock(start_sec=0.0, end_sec=1e9, position=pos, gain=gain, width=width)
    return AdmObject(
        adm_index=oi,
        object_id=f"AO_{oi}",
        label=f"obj{oi}",
        type_definition="Objects",
        blocks=[blk],
        osc_object_index=oi,
    )


def _cart_obj(oi: int, x: float, y: float, z: float, gain=None, width=None) -> AdmObject:
    pos = ObjectPosition(mode="cartesian", x=x, y=y, z=z)
    blk = ObjectBlock(start_sec=0.0, end_sec=1e9, position=pos, gain=gain, width=width)
    return AdmObject(
        adm_index=oi,
        object_id=f"AO_{oi}",
        label=f"obj{oi}",
        type_definition="Objects",
        blocks=[blk],
        osc_object_index=oi,
    )


def _capture_emitter(azimuth_offset: float = 0.0, azimuth_flip: bool = False):
    sends: dict[str, object] = {}
    emitter = AdmOscEmitter(
        "127.0.0.1",
        9000,
        prog=None,
        azimuth_offset=azimuth_offset,
        azimuth_flip=azimuth_flip,
        on_send=lambda addr, val: sends.__setitem__(addr, val),
    )
    return emitter, sends


def _rec_by_obj(records, oi: int):
    for r in records:
        if r.obj_id == oi:
            return r
    return None


# ═════════════════════════════════════════════════════════════════════════════
# U5 — coordinate/gain/width parity vs AdmOscEmitter (AC6)
# ═════════════════════════════════════════════════════════════════════════════


def test_parity_polar_and_cart_first_block() -> None:
    objs = [
        _polar_obj(1, az=-90.0, el=12.5, dist=0.5, gain=0.75, width=0.3),
        _cart_obj(2, x=0.4, y=-0.2, z=0.1, gain=1.0, width=0.0),
    ]
    emitter, sends = _capture_emitter()
    builder = MetaRecordBuilder()

    # Same input to BOTH paths for the same block (t0=0).
    from adm_player.adm_model import active_block

    for obj in objs:
        emitter.send_object_position(obj, active_block(obj.blocks, 0.0))
    records = builder.build(objs, t0=0.0, frame_index=0)

    # obj 1 (polar) — record a0/a1/a2 == OSC aed; gain/width == OSC gain/width.
    r1 = _rec_by_obj(records, 1)
    assert r1 is not None and r1.coord_mode == META_COORD_POLAR
    aed = sends["/adm/obj/1/aed"]
    assert [r1.a0, r1.a1, r1.a2] == list(aed), "polar a0/a1/a2 must equal OSC aed payload"
    assert r1.gain_lin == sends["/adm/obj/1/gain"]
    assert r1.width_rad == sends["/adm/obj/1/width"]
    assert r1.flags == META_FLAG_ACTIVE

    # obj 2 (cartesian) — record a0/a1/a2 == OSC xyz.
    r2 = _rec_by_obj(records, 2)
    assert r2 is not None and r2.coord_mode == META_COORD_CART
    xyz = sends["/adm/obj/2/xyz"]
    assert [r2.a0, r2.a1, r2.a2] == list(xyz), "cart a0/a1/a2 must equal OSC xyz payload"


def test_parity_with_azimuth_offset_and_flip() -> None:
    obj = _polar_obj(1, az=30.0, el=5.0, dist=2.0, gain=0.9, width=0.2)
    emitter, sends = _capture_emitter(azimuth_offset=15.0, azimuth_flip=True)
    builder = MetaRecordBuilder(azimuth_offset=15.0, azimuth_flip=True)

    from adm_player.adm_model import active_block

    emitter.send_object_position(obj, active_block(obj.blocks, 0.0))
    records = builder.build([obj], t0=0.0, frame_index=0)

    r1 = _rec_by_obj(records, 1)
    aed = sends["/adm/obj/1/aed"]
    assert [r1.a0, r1.a1, r1.a2] == list(aed), "az-flip/offset must match OSC bit-for-bit"


def test_mute_flag_on_zero_gain() -> None:
    obj = _polar_obj(1, az=0.0, el=0.0, dist=1.0, gain=0.0, width=0.0)
    builder = MetaRecordBuilder()
    records = builder.build([obj], t0=0.0, frame_index=0)
    r1 = _rec_by_obj(records, 1)
    assert r1.gain_lin == 0.0
    assert r1.flags == (META_FLAG_ACTIVE | META_FLAG_MUTE)


def test_default_gain_width_when_unauthored() -> None:
    obj = _polar_obj(1, az=0.0, el=0.0, dist=1.0)  # no gain/width authored
    builder = MetaRecordBuilder()
    records = builder.build([obj], t0=0.0, frame_index=0)
    r1 = _rec_by_obj(records, 1)
    assert r1.gain_lin == 1.0, "unauthored gain defaults to unity"
    assert r1.width_rad == 0.0, "unauthored width defaults to zero"


# ═════════════════════════════════════════════════════════════════════════════
# U5 — dedup rule mirrors AdmOscEmitter
# ═════════════════════════════════════════════════════════════════════════════


def test_unchanged_block_yields_no_record() -> None:
    obj = _polar_obj(1, az=45.0, el=0.0, dist=1.0, gain=1.0, width=0.1)
    builder = MetaRecordBuilder()
    first = builder.build([obj], t0=0.0, frame_index=0)
    assert len(first) == 1, "first (silent→active) block emits a record"
    second = builder.build([obj], t0=1.0, frame_index=512)
    assert second == [], "unchanged state must dedup to no record"


def test_position_change_emits_record() -> None:
    pos1 = ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=1.0)
    pos2 = ObjectPosition(mode="polar", azimuth=90.0, elevation=0.0, distance=1.0)
    obj = AdmObject(
        adm_index=1,
        object_id="AO_1",
        label="o",
        type_definition="Objects",
        blocks=[
            ObjectBlock(start_sec=0.0, end_sec=1.0, position=pos1, gain=1.0, width=0.1),
            ObjectBlock(start_sec=1.0, end_sec=2.0, position=pos2, gain=1.0, width=0.1),
        ],
        osc_object_index=1,
    )
    builder = MetaRecordBuilder()
    builder.build([obj], t0=0.5, frame_index=0)  # active edge
    rec = builder.build([obj], t0=1.5, frame_index=48000)
    assert len(rec) == 1, "position change must emit a record"
    assert rec[0].a0 == 90.0


def test_gain_only_change_holds_position() -> None:
    pos = ObjectPosition(mode="polar", azimuth=10.0, elevation=0.0, distance=1.0)
    obj = AdmObject(
        adm_index=1,
        object_id="AO_1",
        label="o",
        type_definition="Objects",
        blocks=[
            ObjectBlock(start_sec=0.0, end_sec=1.0, position=pos, gain=1.0, width=0.2),
            # same position, gain changed only
            ObjectBlock(start_sec=1.0, end_sec=2.0, position=pos, gain=0.5, width=0.2),
        ],
        osc_object_index=1,
    )
    builder = MetaRecordBuilder()
    builder.build([obj], t0=0.5, frame_index=0)
    rec = builder.build([obj], t0=1.5, frame_index=48000)
    assert len(rec) == 1, "gain change must emit a record"
    r = rec[0]
    assert r.gain_lin == 0.5
    assert r.a0 == 10.0, "position is held (current full state) even on gain-only change"


def test_out_of_range_index_skipped() -> None:
    builder = MetaRecordBuilder(max_objects=64)
    obj = _polar_obj(200, az=0.0, el=0.0, dist=1.0, gain=1.0, width=0.0)
    records = builder.build([obj], t0=0.0, frame_index=0)
    assert records == [], "index beyond max_objects must be skipped (mirror OSC gate)"


def test_silence_falling_edge_record() -> None:
    pos = ObjectPosition(mode="polar", azimuth=20.0, elevation=0.0, distance=1.0)
    obj = AdmObject(
        adm_index=1,
        object_id="AO_1",
        label="o",
        type_definition="Objects",
        blocks=[ObjectBlock(start_sec=0.0, end_sec=1.0, position=pos, gain=1.0, width=0.0)],
        osc_object_index=1,
    )
    builder = MetaRecordBuilder()
    builder.build([obj], t0=0.5, frame_index=0)  # active
    rec = builder.build([obj], t0=2.0, frame_index=96000)  # past end → silence
    assert len(rec) == 1, "active→inactive edge emits one falling-edge record"
    assert rec[0].flags & META_FLAG_ACTIVE == 0, "falling-edge record clears the active bit"
    assert rec[0].a0 == 20.0, "falling-edge holds last known position"
    # A subsequent silent block emits nothing.
    assert builder.build([obj], t0=3.0, frame_index=144000) == []
