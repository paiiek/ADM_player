from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from pythonosc import udp_client

from .adm_model import AdmObject, ObjectBlock, ObjectPosition

# ADM-OSC distance 정규화 최대값 (meters).
# spatial_engine AdmOscConstants.h:12 의 ADM_OSC_MAX_DIST 와 정렬 — ADR 0006 참조.
# 이 정규화는 spatial_engine 의 ADM_OSC_MAX_DIST 와 정렬되어야 함.
ADM_OSC_MAX_DIST: float = 20.0

# spatial_engine MAX_OBJECTS=64 와 정렬. 64를 초과하는 osc_object_index는
# engine 측에서 무시되므로 송신 단계에서 미리 차단해 OS UDP 큐 낭비를 막는다.
# 02.wav 처럼 108 객체를 가진 마스터는 첫 64개만 emit.
MAX_OSC_OBJECTS: int = 64

_log = logging.getLogger(__name__)


def adm_polar_to_osc_aed(pos: ObjectPosition, azimuth_offset: float, azimuth_flip: bool) -> tuple[float, float, float]:
    az = float(pos.azimuth or 0.0) + azimuth_offset
    if azimuth_flip:
        az = -az
    el = float(pos.elevation or 0.0)
    dist = float(pos.distance if pos.distance is not None else 1.0)
    if dist > 1.0:
        dist = min(1.0, max(0.0, dist / ADM_OSC_MAX_DIST))
    else:
        dist = min(1.0, max(0.0, dist))
    return az, el, dist


def _apply_cart_azimuth(
    x: float, y: float, azimuth_offset: float, azimuth_flip: bool
) -> tuple[float, float]:
    """Apply --azimuth-offset / --flip-azimuth to a Cartesian (x, y) pair.

    Mirrors the polar convention (``az = atan2(x, y)``, ``x = sin(az)``,
    ``y = cos(az)``) so flip/offset behave identically whether the source ADM
    block is polar or Cartesian — Dolby Atmos masters author objects in
    Cartesian, so without this the flags would be silent no-ops on real content.
    Order matches the polar path: rotate by ``+offset`` first, then mirror for
    flip (``az -> -(az + offset)``). ``z`` (elevation) is unaffected.
    """
    if azimuth_offset:
        a = math.radians(azimuth_offset)
        ca, sa = math.cos(a), math.sin(a)
        x, y = x * ca + y * sa, -x * sa + y * ca
    if azimuth_flip:
        x = -x
    return x, y


def adm_cart_to_osc_xyz(
    pos: ObjectPosition, azimuth_offset: float = 0.0, azimuth_flip: bool = False
) -> tuple[float, float, float]:
    x = float(pos.x if pos.x is not None else 0.0)
    y = float(pos.y if pos.y is not None else 0.0)
    z = float(pos.z if pos.z is not None else 0.0)
    x, y = _apply_cart_azimuth(x, y, azimuth_offset, azimuth_flip)
    return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)), max(-1.0, min(1.0, z))


def adm_polar_to_osc_xyz(
    pos: ObjectPosition, azimuth_offset: float, azimuth_flip: bool
) -> tuple[float, float, float]:
    """Polar ADM block → normalized Cartesian (-1..1) for targets that only accept xyz OSC."""
    az = float(pos.azimuth or 0.0) + azimuth_offset
    if azimuth_flip:
        az = -az
    el = float(pos.elevation or 0.0)
    dist = float(pos.distance if pos.distance is not None else 1.0)
    if dist > 1.0:
        dist = min(1.0, max(0.0, dist / ADM_OSC_MAX_DIST))
    else:
        dist = min(1.0, max(0.0, dist))
    az_r = math.radians(az)
    el_r = math.radians(el)
    x = dist * math.cos(el_r) * math.sin(az_r)
    y = dist * math.cos(el_r) * math.cos(az_r)
    z = dist * math.sin(el_r)
    return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)), max(-1.0, min(1.0, z))


def adm_cart_to_polar_deg_distance_norm(
    pos: ObjectPosition, azimuth_offset: float = 0.0, azimuth_flip: bool = False
) -> tuple[float, float, float]:
    """
    Cartesian ADM 블록 → (방위° , 고도° , 거리 0..1).
    `adm_polar_to_osc_xyz` 역변환: 전방 +Y, az = atan2(x,y), 거리 정규화는 polar과 동일(>1이면 /10).
    """
    x = float(pos.x if pos.x is not None else 0.0)
    y = float(pos.y if pos.y is not None else 0.0)
    x, y = _apply_cart_azimuth(x, y, azimuth_offset, azimuth_flip)
    x = max(-1.0, min(1.0, x))
    y = max(-1.0, min(1.0, y))
    z = max(-1.0, min(1.0, float(pos.z if pos.z is not None else 0.0)))
    h = math.hypot(x, y)
    dist = math.sqrt(x * x + y * y + z * z)
    if dist < 1e-20:
        return 0.0, 0.0, 0.0
    az_deg = math.degrees(math.atan2(x, y))
    el_deg = math.degrees(math.atan2(z, h))
    if dist > 1.0:
        dnorm = min(1.0, max(0.0, dist / ADM_OSC_MAX_DIST))
    else:
        dnorm = min(1.0, max(0.0, dist))
    return az_deg, el_deg, dnorm


def adm_position_to_polar_deg_distance_norm(
    pos: ObjectPosition, azimuth_offset: float, azimuth_flip: bool
) -> tuple[float, float, float]:
    """ADM 블록이 polar이면 그대로 정규화, cartesian이면 구면 역변환."""
    if pos.mode == "cartesian":
        return adm_cart_to_polar_deg_distance_norm(pos, azimuth_offset, azimuth_flip)
    return adm_polar_to_osc_aed(pos, azimuth_offset, azimuth_flip)


def adm_polar_to_az_el_distance_adm(
    pos: ObjectPosition, azimuth_offset: float, azimuth_flip: bool
) -> tuple[float, float, float]:
    """Polar ADM: 방위·고도(°), distance는 메타데이터 값을 [-1, 1]로 클램프."""
    az = float(pos.azimuth or 0.0) + azimuth_offset
    if azimuth_flip:
        az = -az
    el = float(pos.elevation or 0.0)
    d = float(pos.distance if pos.distance is not None else 1.0)
    d_adm = max(-1.0, min(1.0, d))
    return az, el, d_adm


def adm_cart_to_az_el_distance_adm(
    pos: ObjectPosition, azimuth_offset: float = 0.0, azimuth_flip: bool = False
) -> tuple[float, float, float]:
    """
    Cartesian ADM: 구면 방위·고도(°), distance는 [-1, 1] (원점 r=0 → -1, 단위 큐브 대각 r=√3 → +1).
    """
    x, y, z = adm_cart_to_osc_xyz(pos, azimuth_offset, azimuth_flip)
    h = math.hypot(x, y)
    r = math.sqrt(x * x + y * y + z * z)
    if r < 1e-20:
        return 0.0, 0.0, -1.0
    az_deg = math.degrees(math.atan2(x, y))
    el_deg = math.degrees(math.atan2(z, h))
    rmx = math.sqrt(3.0)
    d_adm = -1.0 + 2.0 * (r / rmx)
    d_adm = max(-1.0, min(1.0, d_adm))
    return az_deg, el_deg, d_adm


def adm_position_to_az_el_distance_adm(
    pos: ObjectPosition, azimuth_offset: float, azimuth_flip: bool
) -> tuple[float, float, float]:
    if pos.mode == "cartesian":
        return adm_cart_to_az_el_distance_adm(pos, azimuth_offset, azimuth_flip)
    return adm_polar_to_az_el_distance_adm(pos, azimuth_offset, azimuth_flip)


def adm_distance_adm_to_lisa(dist_adm: float) -> float:
    """ADM distance [-1, 1] → L-ISA pwdes distance [0.1, 1.0]."""
    d = max(-1.0, min(1.0, float(dist_adm)))
    return 0.1 + (d + 1.0) * 0.45


def lisa_azimuth_deg_to_normalized(az_deg: float) -> float:
    """L-ISA pwdes: 0.001 → -179.64°, 1.0 → +180° 선형 매핑."""
    a = (float(az_deg) + 180.0) % 360.0 - 180.0
    # (-180, 180] 표준화에서 +180°는 -180과 동일 메리디언; 아래 구간의 오른쪽 끝은 +180°로 둠
    if abs(a + 180.0) < 1e-9:
        a = 180.0
    lo_deg, hi_deg = -179.64, 180.0
    lo_n, hi_n = 0.001, 1.0
    span_deg = hi_deg - lo_deg
    t = (a - lo_deg) / span_deg
    v = lo_n + t * (hi_n - lo_n)
    return max(lo_n, min(hi_n, v))


def lisa_elevation_deg_to_normalized(el_deg: float) -> float:
    """L-ISA pwdes: 고도 0°~90° → 0.0~1.0."""
    e = max(0.0, min(90.0, float(el_deg)))
    return e / 90.0


def adm_position_to_lisa_pwdes_payload(
    pos: ObjectPosition,
    azimuth_offset: float,
    azimuth_flip: bool,
    *,
    scale_polar: tuple[float, float, float] = (1.0, 1.0, 1.0),
    width: float = 0.3,
    aux_send: float = 0.0,
) -> list[float]:
    """
    /ext/src/<id>/pwdes 인자: az_norm, width, distance, elevation_norm, aux_send
    distance: ADM [-1, 1] (polar은 메타 distance, cartesian은 원점 기준 정규) →
        L-ISA [0.1, 1.0].
    """
    az_deg, el_deg, d_adm = adm_position_to_az_el_distance_adm(pos, azimuth_offset, azimuth_flip)
    az_deg *= scale_polar[0]
    el_deg *= scale_polar[1]
    d_adm = max(-1.0, min(1.0, d_adm * scale_polar[2]))
    dist_lisa = adm_distance_adm_to_lisa(d_adm)
    az_n = lisa_azimuth_deg_to_normalized(az_deg)
    el_n = lisa_elevation_deg_to_normalized(el_deg)
    return [az_n, width, dist_lisa, el_n, aux_send]


class AdmOscEmitter:
    def __init__(
        self,
        host: str,
        port: int,
        prog: int | None,
        azimuth_offset: float = 0.0,
        azimuth_flip: bool = False,
        on_send: Callable[[str, Any], None] | None = None,
        *,
        scale_polar: tuple[float, float, float] = (1.0, 1.0, 1.0),
        scale_cart: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> None:
        self._client = udp_client.SimpleUDPClient(host, port)
        self._prog = prog
        self._azimuth_offset = azimuth_offset
        self._azimuth_flip = azimuth_flip
        self._on_send = on_send
        self._scale_polar = scale_polar
        self._scale_cart = scale_cart
        self._last_mode: dict[int, str] = {}
        self._last_payload: dict[int, tuple[float, float, float]] = {}
        self._overflow_warned: set[int] = set()

    def _send(self, address: str, value: Any) -> None:
        if self._on_send is not None:
            self._on_send(address, value)
        self._client.send_message(address, value)

    def _check_obj_index(self, oi: int) -> bool:
        """Return False (and warn once per oi) if oi is outside the engine's MAX_OSC_OBJECTS slot range."""
        if oi < 1 or oi > MAX_OSC_OBJECTS:
            if oi not in self._overflow_warned:
                self._overflow_warned.add(oi)
                _log.warning(
                    "ADM object index %d out of OSC slot range [1, %d]; payloads will be dropped.",
                    oi,
                    MAX_OSC_OBJECTS,
                )
            return False
        return True

    def _addr(self, *parts: str | int) -> str:
        base = "/adm"
        if self._prog is not None:
            base += f"/prog/{self._prog}"
        for p in parts:
            base += f"/{p}"
        return base

    def send_object_config_cartesian(self, obj_index: int, use_cartesian: bool) -> None:
        if not self._check_obj_index(obj_index):
            return
        self._send(self._addr("config", "obj", obj_index, "cartesian"), int(1 if use_cartesian else 0))

    def send_object_position(self, obj: AdmObject, block: ObjectBlock | None) -> None:
        if block is None:
            return
        pos = block.position
        # WAV 인터리브 채널 번호(1-based)와 동일한 N → /adm/obj/N
        oi = obj.osc_object_index
        if not self._check_obj_index(oi):
            return
        if pos.mode == "cartesian":
            if self._last_mode.get(oi) != "cart":
                self.send_object_config_cartesian(oi, True)
                self._last_mode[oi] = "cart"
                self._last_payload.pop(oi, None)  # mode 전환 시 캐시 무효화 (C7)
            xyz = adm_cart_to_osc_xyz(pos, self._azimuth_offset, self._azimuth_flip)
            xyz = tuple(xyz[i] * self._scale_cart[i] for i in range(3))
            if self._last_payload.get(oi) == xyz:
                return
            self._last_payload[oi] = xyz
            self._send(self._addr("obj", oi, "xyz"), list(xyz))
        else:
            if self._last_mode.get(oi) != "polar":
                self.send_object_config_cartesian(oi, False)
                self._last_mode[oi] = "polar"
                self._last_payload.pop(oi, None)  # mode 전환 시 캐시 무효화 (C7)
            aed = adm_polar_to_osc_aed(pos, self._azimuth_offset, self._azimuth_flip)
            aed = (
                aed[0] * self._scale_polar[0],
                aed[1] * self._scale_polar[1],
                aed[2] * self._scale_polar[2],
            )
            if self._last_payload.get(oi) == aed:
                return
            self._last_payload[oi] = aed
            self._send(self._addr("obj", oi, "aed"), list(aed))
