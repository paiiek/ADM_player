"""
Regression tests for ADM Player ↔ spatial_engine integration contract.

Covers:
  - C6: distance normalization uses ADM_OSC_MAX_DIST (20.0) across every conversion path,
        with continuous behavior across the d=1.0 boundary when input is normalized.
  - C7: mode transition (polar ↔ cartesian) must invalidate the dedupe cache so the first
        post-transition payload is always emitted, even if its tuple coincidentally matches
        the previous mode's tuple.
  - C8: 7.1.4 top-channel labels (Ltf/Rtf/Ltr/Rtr) must map to distinct
        RoomCentricLeftTopFront / Rear pairs — no collapse to a single Top Surround pair.
  - spatial_engine preset: routed through the standard ADM-OSC emitter (verbatim addresses),
        with a sensible default endpoint exposed for the UI.

These tests run without a network socket — pythonosc's SimpleUDPClient.send_message is
exercised, but the `on_send` hook captures everything in-process, so no port is bound.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from adm_player.adm_model import AdmObject, ObjectBlock, ObjectPosition
from adm_player.osc_emit import (
    ADM_OSC_MAX_DIST,
    MAX_OSC_OBJECTS,
    AdmOscEmitter,
    adm_cart_to_polar_deg_distance_norm,
    adm_polar_to_osc_aed,
    adm_polar_to_osc_xyz,
)
from adm_recorder.osc_ingest import aed_deg_to_xyz
from adm_player.osc_presets import (
    PRESET_DEFAULT_ENDPOINTS,
    PRESET_ENTRIES,
    PresetOscEmitter,
    create_osc_emitter,
)
from adm_recorder.bwf_atmos_writer import _BED_SPEAKER_DOLBY, build_axml_ebu
from adm_recorder.channel_config import ChannelMapState, ChannelRole, BED_LAYOUTS_BY_ID


def _obj(idx: int = 1) -> AdmObject:
    """Minimal AdmObject stub — only osc_object_index is consulted by the emitter,
    but every dataclass field must be filled to satisfy construction."""
    return AdmObject(
        adm_index=idx,
        object_id=f"AO_{idx:04x}",
        label=f"obj_{idx}",
        type_definition="objects",
        track_uids=(),
        wav_channels=(idx,),
        blocks=(),
        osc_object_index=idx,
    )


def _capture() -> tuple[list[tuple[str, Any]], Any]:
    sent: list[tuple[str, Any]] = []
    return sent, lambda addr, value: sent.append((addr, value))


def _free_port() -> int:
    """Pick an unused UDP port so AdmOscEmitter's socket bind never races on a busy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ C6: distance contract

def test_c6_polar_to_aed_uses_max_dist_20_for_metric_inputs() -> None:
    # Inputs > 1.0 are treated as meters → divided by ADM_OSC_MAX_DIST (= 20).
    _, _, d = adm_polar_to_osc_aed(ObjectPosition(mode="polar", azimuth=0, elevation=0, distance=10.0), 0.0, False)
    assert d == pytest.approx(10.0 / ADM_OSC_MAX_DIST)
    _, _, d = adm_polar_to_osc_aed(ObjectPosition(mode="polar", azimuth=0, elevation=0, distance=20.0), 0.0, False)
    assert d == pytest.approx(1.0)
    # Clamp above the max range.
    _, _, d = adm_polar_to_osc_aed(ObjectPosition(mode="polar", azimuth=0, elevation=0, distance=40.0), 0.0, False)
    assert d == pytest.approx(1.0)


def test_c6_polar_to_aed_normalized_passthrough_unchanged() -> None:
    # Inputs in [0, 1] are already-normalized — must not be rescaled.
    for d_in in (0.0, 0.25, 0.5, 0.75, 1.0):
        _, _, d_out = adm_polar_to_osc_aed(
            ObjectPosition(mode="polar", azimuth=0, elevation=0, distance=d_in), 0.0, False
        )
        assert d_out == pytest.approx(d_in)


def test_c6_polar_to_xyz_consistent_with_aed_for_metric_inputs() -> None:
    pos = ObjectPosition(mode="polar", azimuth=90.0, elevation=0.0, distance=10.0)
    _, _, d_aed = adm_polar_to_osc_aed(pos, 0.0, False)
    x, _, _ = adm_polar_to_osc_xyz(pos, 0.0, False)
    # Same normalization base → matching magnitudes (az=90° → unit-x at d_aed).
    assert abs(x) == pytest.approx(d_aed, abs=1e-6)


def test_c6_cart_to_polar_distance_uses_max_dist_20() -> None:
    # Place a synthetic ADM cart at radius 10 along +Y → normalized distance 10/20 = 0.5.
    # The function clamps |x|,|y|,|z| to [-1, 1] before computing radius, so we need a
    # synthetic input whose computed radius > 1; achieved via three unit-axis components.
    pos = ObjectPosition(mode="cartesian", x=1.0, y=1.0, z=1.0)  # r = sqrt(3) > 1
    _, _, d = adm_cart_to_polar_deg_distance_norm(pos)
    assert d == pytest.approx((3.0 ** 0.5) / ADM_OSC_MAX_DIST)


# ------------------------------------------------------------------ C7: mode transition

def test_c7_polar_to_cart_transition_emits_xyz_payload() -> None:
    """The pre-fix bug: cart cfg goes out, but xyz payload is silently swallowed because
    the dedupe cache still holds the previous polar tuple that happens to equal (0,0,1)."""
    sent, on_send = _capture()
    em = AdmOscEmitter("127.0.0.1", _free_port(), prog=None, on_send=on_send)
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=1)))
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("cartesian", x=0, y=0, z=1)))
    addrs = [a for a, _ in sent]
    assert "/adm/obj/1/aed" in addrs
    assert "/adm/config/obj/1/cartesian" in addrs
    assert "/adm/obj/1/xyz" in addrs, f"xyz payload swallowed by stale cache; sent={addrs}"


def test_c7_cart_to_polar_transition_emits_aed_payload() -> None:
    """Symmetric case: cart → polar must not be swallowed by a coincidental tuple match."""
    sent, on_send = _capture()
    em = AdmOscEmitter("127.0.0.1", _free_port(), prog=None, on_send=on_send)
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("cartesian", x=0, y=0, z=1)))
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=1)))
    addrs = [a for a, _ in sent]
    assert "/adm/obj/1/xyz" in addrs
    assert addrs.count("/adm/config/obj/1/cartesian") >= 2  # cart→on, polar→off
    assert "/adm/obj/1/aed" in addrs, f"aed payload swallowed by stale cache; sent={addrs}"


def test_c7_preset_emitter_also_invalidates_cache() -> None:
    """PresetOscEmitter must apply the same cache-invalidation rule (covers adm_player/osc_presets.py).

    The built-in cart-only presets (spat_revolution, lisa, ...) can't expose this bug because
    they never switch to a 'polar' code path, so we drive the dual cfg/xyz/aed branch via a
    custom template that does both.
    """
    sent, on_send = _capture()
    em = PresetOscEmitter(
        "custom",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        scales={},
        on_send=on_send,
        tpl={"polar": "/x/{i}/aed", "cart": "/x/{i}/xyz", "cfg": "/x/{i}/cfg"},
    )
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=1)))
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("cartesian", x=0, y=0, z=1)))
    addrs = [a for a, _ in sent]
    assert "/x/1/xyz" in addrs, f"PresetOscEmitter swallowed cart payload after mode flip; sent={addrs}"


# ------------------------------------------------------------------ C8: Atmos 7.1.4 labels

def test_c8_seven_one_four_top_labels_distinct() -> None:
    """Top Front and Top Rear pairs must occupy different label/coordinate slots."""
    ltf = _BED_SPEAKER_DOLBY["Ltf"]
    rtf = _BED_SPEAKER_DOLBY["Rtf"]
    ltr = _BED_SPEAKER_DOLBY["Ltr"]
    rtr = _BED_SPEAKER_DOLBY["Rtr"]
    # Names must not collapse to a single "TopSurround" pair (pre-fix bug).
    assert ltf[0] == "RoomCentricLeftTopFront"
    assert rtf[0] == "RoomCentricRightTopFront"
    assert ltr[0] == "RoomCentricLeftTopRear"
    assert rtr[0] == "RoomCentricRightTopRear"
    # Y must distinguish Front (+) vs Rear (-); X distinguishes Left vs Right; Z all up.
    assert ltf[2][1] > 0 and rtf[2][1] > 0
    assert ltr[2][1] < 0 and rtr[2][1] < 0
    assert ltf[2][0] < 0 and ltr[2][0] < 0
    assert rtf[2][0] > 0 and rtr[2][0] > 0
    assert all(s[2][2] > 0 for s in (ltf, rtf, ltr, rtr))


def test_c8_axml_uses_layout_xyz_not_hardcoded_constants() -> None:
    """7.1.4 bed → axml must contain the active layout's coordinates for top channels,
    not the (-1,0,1)/(1,0,1) generic Top Surround placeholders."""
    cm = ChannelMapState(n_channels=12)
    cm.bed_layout_id = "7_1_4"
    cm.roles = [ChannelRole.BED] * 12
    bed = cm.bed_assignments_ordered()
    assert len(bed) == 12
    axml, _ = build_axml_ebu(
        channel_roles=cm.roles,
        bed_assignments=bed,
        blocks_per_object={},
        total_frames=48,
        sample_rate=48000.0,
    )
    layout = BED_LAYOUTS_BY_ID["7_1_4"]
    # Find the four top speakers in the layout and verify their unique X/Y/Z appear in axml.
    for label, x, y, z in layout.speakers:
        if label in ("Ltf", "Rtf", "Ltr", "Rtr"):
            assert f"{x:.10f}" in axml, f"layout X={x} for {label} not written into axml"
            assert f"{y:.10f}" in axml, f"layout Y={y} for {label} not written into axml"
            assert f"{z:.10f}" in axml, f"layout Z={z} for {label} not written into axml"
    # All 4 distinct top labels must appear; the pre-fix bug folded them all into a
    # single Top Surround pair (Lts/Rts), so those legacy names must NOT show up in
    # a 7.1.4 axml — Top Surround is reserved for the 7.1.2 single-pair layout.
    assert "RoomCentricLeftTopFront" in axml
    assert "RoomCentricRightTopFront" in axml
    assert "RoomCentricLeftTopRear" in axml
    assert "RoomCentricRightTopRear" in axml
    assert "RoomCentricLeftTopSurround" not in axml, "7.1.4 must not emit Top Surround labels"
    assert "RoomCentricRightTopSurround" not in axml, "7.1.4 must not emit Top Surround labels"


# ------------------------------------------------------------------ spatial_engine preset

def test_spatial_engine_preset_registered_with_default_endpoint() -> None:
    ids = [pid for pid, _ in PRESET_ENTRIES]
    assert "spatial_engine" in ids
    assert PRESET_DEFAULT_ENDPOINTS["spatial_engine"] == ("127.0.0.1", 9100)


def test_spatial_engine_emitter_uses_standard_adm_osc_addresses() -> None:
    """spatial_engine must speak ADM-OSC v1.0 verbatim — same wire format as the 'adm' preset."""
    sent, on_send = _capture()
    em = create_osc_emitter(
        "spatial_engine",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    assert isinstance(em, AdmOscEmitter)
    em.send_object_position(_obj(3), ObjectBlock(0, 1, ObjectPosition("polar", azimuth=45, elevation=10, distance=0.5)))
    addrs = [a for a, _ in sent]
    # Confirm the engine-facing address shape (spatial_engine CommandDecoder reads /adm/obj/N/aed).
    assert "/adm/obj/3/aed" in addrs


def test_spatial_engine_round_trips_meters_through_max_dist_contract() -> None:
    """vid2spatial writes dist_m in meters; the engine expects normalized [0,1] with 20m = 1.0.
    A round trip through the emitter must reflect that contract."""
    sent, on_send = _capture()
    em = create_osc_emitter(
        "spatial_engine",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    em.send_object_position(_obj(1), ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=10.0)))
    payload = next(v for a, v in sent if a == "/adm/obj/1/aed")
    assert payload[2] == pytest.approx(0.5, abs=1e-6)


# ------------------------------------------------------------------ MAX_OSC_OBJECTS guard

def test_obj_index_over_max_is_silently_dropped(caplog) -> None:
    """02.wav has 108 objects; spatial_engine slot range is [1, MAX_OSC_OBJECTS=64].
    Indices beyond that must be dropped at the emitter — no /adm/obj/65/* on the wire."""
    sent, on_send = _capture()
    em = AdmOscEmitter("127.0.0.1", _free_port(), prog=None, on_send=on_send)
    caplog.set_level("WARNING", logger="adm_player.osc_emit")
    em.send_object_position(
        _obj(MAX_OSC_OBJECTS + 1),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=0.5)),
    )
    em.send_object_position(
        _obj(MAX_OSC_OBJECTS + 1),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=10, elevation=0, distance=0.5)),
    )
    em.send_object_config_cartesian(MAX_OSC_OBJECTS + 1, True)
    assert sent == [], f"over-limit oi must not produce any OSC payload; got {sent}"
    # Single warning per oi, not one per call → log spam guard.
    warnings_for_oi = [r for r in caplog.records if "out of OSC slot range" in r.message]
    assert len(warnings_for_oi) == 1


def test_obj_index_at_max_is_allowed() -> None:
    """oi == MAX_OSC_OBJECTS must still be a valid slot (off-by-one regression)."""
    sent, on_send = _capture()
    em = AdmOscEmitter("127.0.0.1", _free_port(), prog=None, on_send=on_send)
    em.send_object_position(
        _obj(MAX_OSC_OBJECTS),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=0.5)),
    )
    addrs = [a for a, _ in sent]
    assert f"/adm/obj/{MAX_OSC_OBJECTS}/aed" in addrs


def test_preset_emitter_also_enforces_max_obj_guard() -> None:
    sent, on_send = _capture()
    em = create_osc_emitter(
        "spat_revolution",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    em.send_object_position(
        _obj(MAX_OSC_OBJECTS + 1),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0, elevation=0, distance=0.5)),
    )
    assert sent == []


# ------------------------------------------------------------------ preset round-trip regressions
# These exercise the three xyz-only presets that the standard ADM round-trip tests can't reach:
#   L-ISA  → /ext/src/{i}/pwdes  (5-float list, normalized az/el + lisa distance)
#   Spat Revolution → /source/{i}/xyz  (xyz scaled by cart_extra_scale=10)
#   Soundscape → /dbaudio1/positioning/source_position/{i}  (z forced to 0)

def test_lisa_preset_emits_pwdes_five_float_payload() -> None:
    sent, on_send = _capture()
    em = create_osc_emitter(
        "lisa",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    # az=90° (right), el=45° (up), normalized distance 0.5 → all five components must be in range.
    em.send_object_position(
        _obj(2),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=90.0, elevation=45.0, distance=0.5)),
    )
    addrs = [a for a, _ in sent]
    assert "/ext/src/2/pwdes" in addrs, f"L-ISA pwdes not emitted; sent={addrs}"
    payload = next(v for a, v in sent if a == "/ext/src/2/pwdes")
    assert isinstance(payload, list) and len(payload) == 5, f"pwdes must be 5-tuple; got {payload!r}"
    az_n, width, dist_lisa, el_n, aux = payload
    # L-ISA normalization invariants
    assert 0.001 <= az_n <= 1.0, f"az_n out of L-ISA range: {az_n}"
    assert 0.0 <= el_n <= 1.0, f"el_n out of L-ISA range: {el_n}"
    assert 0.1 <= dist_lisa <= 1.0, f"L-ISA distance out of pwdes range: {dist_lisa}"
    assert width == pytest.approx(0.3)
    assert aux == pytest.approx(0.0)


def test_lisa_preset_dedupes_identical_payloads() -> None:
    sent, on_send = _capture()
    em = create_osc_emitter(
        "lisa", "127.0.0.1", _free_port(),
        azimuth_offset=0.0, azimuth_flip=False, on_send=on_send, scales={},
    )
    blk = ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0.0, elevation=0.0, distance=0.5))
    em.send_object_position(_obj(1), blk)
    em.send_object_position(_obj(1), blk)
    pwdes = [a for a, _ in sent if a == "/ext/src/1/pwdes"]
    assert len(pwdes) == 1, f"identical pwdes must dedupe; got {len(pwdes)} emits"


def test_spat_revolution_preset_emits_xyz_scaled_by_ten() -> None:
    sent, on_send = _capture()
    em = create_osc_emitter(
        "spat_revolution",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    # cartesian unit X → xyz scaled by cart_extra_scale=10 → (10, 0, 0)
    em.send_object_position(
        _obj(3),
        ObjectBlock(0, 1, ObjectPosition("cartesian", x=1.0, y=0.0, z=0.0)),
    )
    addrs = [a for a, _ in sent]
    assert "/source/3/xyz" in addrs, f"spat_revolution xyz not emitted; sent={addrs}"
    payload = next(v for a, v in sent if a == "/source/3/xyz")
    assert isinstance(payload, list) and len(payload) == 3
    assert payload[0] == pytest.approx(10.0, abs=1e-9), f"x must be scaled by cart_extra_scale=10; got {payload}"
    assert payload[1] == pytest.approx(0.0)
    assert payload[2] == pytest.approx(0.0)
    # cfg address never goes out for xyz-only preset
    assert all(not a.endswith("/cartesian") for a, _ in sent), f"xyz-only preset must not emit cfg; sent={addrs}"


def test_spat_revolution_preset_polar_input_converted_to_xyz() -> None:
    """polar block fed to an xyz-only preset must take the polar→xyz code path
    (covers `xyz_only` branch in PresetOscEmitter)."""
    sent, on_send = _capture()
    em = create_osc_emitter(
        "spat_revolution", "127.0.0.1", _free_port(),
        azimuth_offset=0.0, azimuth_flip=False, on_send=on_send, scales={},
    )
    em.send_object_position(
        _obj(1),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0.0, elevation=0.0, distance=1.0)),
    )
    addrs = [a for a, _ in sent]
    assert "/source/1/xyz" in addrs, f"polar→xyz fallback missing; sent={addrs}"
    # az=0, el=0, dist=1 → unit forward (+Y), scaled by 10
    payload = next(v for a, v in sent if a == "/source/1/xyz")
    assert payload[1] == pytest.approx(10.0, abs=1e-6), f"polar→xyz forward axis wrong; got {payload}"


def test_soundscape_preset_emits_z_zero_xyz() -> None:
    sent, on_send = _capture()
    em = create_osc_emitter(
        "soundscape",
        "127.0.0.1",
        _free_port(),
        azimuth_offset=0.0,
        azimuth_flip=False,
        on_send=on_send,
        scales={},
    )
    # Even with non-zero z input, soundscape_z0 must force z to 0 (no ceiling speakers).
    em.send_object_position(
        _obj(5),
        ObjectBlock(0, 1, ObjectPosition("cartesian", x=0.4, y=0.6, z=0.8)),
    )
    addrs = [a for a, _ in sent]
    assert "/dbaudio1/positioning/source_position/5" in addrs, f"soundscape address missing; sent={addrs}"
    payload = next(v for a, v in sent if a == "/dbaudio1/positioning/source_position/5")
    assert isinstance(payload, list) and len(payload) == 3
    assert payload[0] == pytest.approx(4.0, abs=1e-6), f"x must be scaled by 10; got {payload}"
    assert payload[1] == pytest.approx(6.0, abs=1e-6), f"y must be scaled by 10; got {payload}"
    assert payload[2] == 0.0, f"Soundscape z must be forced to 0; got {payload[2]}"


def test_soundscape_preset_polar_with_elevation_zeroes_z() -> None:
    """Polar block with non-zero elevation → polar→xyz produces non-zero z, but soundscape_z0 zeroes it."""
    sent, on_send = _capture()
    em = create_osc_emitter(
        "soundscape", "127.0.0.1", _free_port(),
        azimuth_offset=0.0, azimuth_flip=False, on_send=on_send, scales={},
    )
    em.send_object_position(
        _obj(1),
        ObjectBlock(0, 1, ObjectPosition("polar", azimuth=0.0, elevation=60.0, distance=1.0)),
    )
    payload = next(v for a, v in sent if a == "/dbaudio1/positioning/source_position/1")
    assert payload[2] == 0.0, f"polar elevation must be zeroed for soundscape; got {payload}"


def test_lisa_spat_revolution_soundscape_all_share_xyz_only_caching() -> None:
    """All three xyz-only presets must dedupe a repeated payload."""
    for preset_id, addr_pattern in [
        ("lisa", "/ext/src/1/pwdes"),
        ("spat_revolution", "/source/1/xyz"),
        ("soundscape", "/dbaudio1/positioning/source_position/1"),
    ]:
        sent, on_send = _capture()
        em = create_osc_emitter(
            preset_id, "127.0.0.1", _free_port(),
            azimuth_offset=0.0, azimuth_flip=False, on_send=on_send, scales={},
        )
        blk = ObjectBlock(0, 1, ObjectPosition("cartesian", x=0.3, y=0.4, z=0.5))
        em.send_object_position(_obj(1), blk)
        em.send_object_position(_obj(1), blk)
        emits = [a for a, _ in sent if a == addr_pattern]
        assert len(emits) == 1, f"{preset_id}: dedupe failed, got {len(emits)} emits at {addr_pattern}"


# ------------------------------------------------------------------ C_DIST_INGEST: recorder ingest round-trip

def test_ingest_aed_10m_normalizes_to_half() -> None:
    """Emitter encodes 10 m as 10/20 = 0.5; recorder ingest must decode the wire value 0.5
    back as-is (already normalized) AND when given a raw metric value > 1 must also
    divide by ADM_OSC_MAX_DIST (20) not by 10.

    Round-trip: emitter polar_to_osc_aed(dist=10m) → wire_d=0.5 → aed_deg_to_xyz(dist=0.5)
    → radius of resulting unit vector == 0.5 (the normalized distance).
    This test FAILS on the old /10 code (which produced 1.0 for dist=10) and PASSES after
    the /ADM_OSC_MAX_DIST fix.
    """
    # Emitter side: 10 m source at az=0, el=0
    pos_10m = ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=10.0)
    _, _, wire_d = adm_polar_to_osc_aed(pos_10m, 0.0, False)
    assert wire_d == pytest.approx(0.5, abs=1e-9), f"emitter must encode 10m as 0.5; got {wire_d}"

    # Ingest side: wire_d=0.5 is already normalized (≤1.0) → must pass straight through
    x, y, z = aed_deg_to_xyz(0.0, 0.0, wire_d)
    r = (x * x + y * y + z * z) ** 0.5
    assert r == pytest.approx(0.5, abs=1e-6), (
        f"ingest of already-normalized wire_d=0.5 must give radius 0.5; got {r}"
    )

    # Ingest side: raw metric value 10 (> 1.0) must divide by ADM_OSC_MAX_DIST (20), not 10
    x2, y2, z2 = aed_deg_to_xyz(0.0, 0.0, 10.0)
    r2 = (x2 * x2 + y2 * y2 + z2 * z2) ** 0.5
    assert r2 == pytest.approx(10.0 / ADM_OSC_MAX_DIST, abs=1e-6), (
        f"ingest of raw 10m must give radius 10/20=0.5; got {r2} (old /10 bug gives 1.0)"
    )


def test_ingest_aed_20m_normalizes_to_one() -> None:
    """Emitter encodes 20 m as 20/20 = 1.0; ingest must recover radius 1.0."""
    pos_20m = ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=20.0)
    _, _, wire_d = adm_polar_to_osc_aed(pos_20m, 0.0, False)
    assert wire_d == pytest.approx(1.0, abs=1e-9)

    # wire_d == 1.0 is ≤ 1.0 → normalized passthrough
    x, y, z = aed_deg_to_xyz(0.0, 0.0, wire_d)
    r = (x * x + y * y + z * z) ** 0.5
    assert r == pytest.approx(1.0, abs=1e-6)

    # raw metric 20 (> 1.0) → 20/20 = 1.0
    x2, y2, z2 = aed_deg_to_xyz(0.0, 0.0, 20.0)
    r2 = (x2 * x2 + y2 * y2 + z2 * z2) ** 0.5
    assert r2 == pytest.approx(1.0, abs=1e-6)
