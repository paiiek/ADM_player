"""
OSC 출력 프리셋: 타깃 앱별 주소 템플릿과 스케일(방위·고도·거리·XYZ)을 적용합니다.

실제 믹스 룸/버전에 따라 주소가 다를 수 있어, 필요 시 템플릿만 맞추면 됩니다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pythonosc import udp_client

from .adm_model import AdmObject, ObjectBlock
from .osc_emit import (
    MAX_OSC_OBJECTS,
    AdmOscEmitter,
    adm_cart_to_osc_xyz,
    adm_position_to_lisa_pwdes_payload,
    adm_polar_to_osc_aed,
    adm_polar_to_osc_xyz,
)

_log = logging.getLogger(__name__)

# (id, UI label)
PRESET_ENTRIES: list[tuple[str, str]] = [
    ("adm", "ADM (default)"),
    ("spatial_engine", "Spatial Engine (DreamScape)"),
    ("spat_revolution", "Spat Revolution"),
    ("lisa", "L-ISA"),
    ("soundscape", "Soundscape"),
    ("adamson_fm", "Adamson Fletcher Machine"),
    ("afc_image", "AFC Image"),
    ("custom", "Custom"),
]

DEFAULT_CUSTOM_TEMPLATES: dict[str, str] = {
    "polar": "/custom/obj/{i}/aed",
    "cart": "/custom/obj/{i}/xyz",
    "cfg": "/custom/obj/{i}/cartesian",
}


def preset_display_title(preset_id: str) -> str:
    for pid, title in PRESET_ENTRIES:
        if pid == preset_id:
            return title
    return preset_id


# polar: None → polar 블록도 Cartesian으로 변환해 cart 주소로만 송신
# cfg: None → Cartesian 모드 전환 OSC 없음
# cart_extra_scale: ADM 기본(대략 -1..1) 대비 좌표 배율 (Soundscape = 10)
# soundscape_z0: Z를 항상 0 (천장 스피커 없음)
_PRESET_TEMPLATES: dict[str, dict[str, Any]] = {
    "spat_revolution": {
        "cart": "/source/{i}/xyz",
        "polar": None,
        "cfg": None,
        "cart_extra_scale": 10.0,
    },
    "lisa": {
        "cart": "/ext/src/{i}/pwdes",
        "polar": None,
        "cfg": None,
        "lisa_pwdes": True,
    },
    "soundscape": {
        "cart": "/dbaudio1/positioning/source_position/{i}",
        "polar": None,
        "cfg": None,
        "cart_extra_scale": 10.0,
        "soundscape_z0": True,
    },
    "adamson_fm": {
        "cart": "/fm/obj/pos/xyz/{i}",
        "polar": None,
        "cfg": None,
    },
    "afc_image": {
        "cart": (
            "/yosc:req/set/PROC:Component/40000/OBA/Object/PhysicalPosition/{i}"
        ),
        "polar": None,
        "cfg": None,
        "cart_extra_scale": 10.0,
    },
}


def _scale_dict_to_tuples(d: dict[str, float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    sp = (d.get("sa", 1.0), d.get("se", 1.0), d.get("sd", 1.0))
    sc = (d.get("sx", 1.0), d.get("sy", 1.0), d.get("sz", 1.0))
    return sp, sc


def _normalize_emitter_template(preset_id: str, tpl: dict[str, Any] | None) -> dict[str, Any]:
    if tpl is not None:
        return dict(tpl)
    if preset_id in _PRESET_TEMPLATES:
        return dict(_PRESET_TEMPLATES[preset_id])
    raise ValueError(f"unknown preset: {preset_id}")


class PresetOscEmitter:
    """Non-default OSC paths (template + scales)."""

    def __init__(
        self,
        preset_id: str,
        host: str,
        port: int,
        azimuth_offset: float,
        azimuth_flip: bool,
        scales: dict[str, float],
        on_send: Callable[[str, Any], None] | None,
        *,
        tpl: dict[str, Any] | None = None,
    ) -> None:
        merged = _normalize_emitter_template(preset_id, tpl)
        self._tpl_cart = str(merged["cart"])
        pol = merged.get("polar")
        self._tpl_polar = str(pol) if pol else None
        cfg = merged.get("cfg")
        self._cfg = str(cfg) if cfg else None
        self._soundscape_z0 = bool(merged.get("soundscape_z0", False))
        self._cart_extra_scale = float(merged.get("cart_extra_scale", 1.0))
        self._xyz_only = self._tpl_polar is None

        self._client = udp_client.SimpleUDPClient(host, port)
        self._azimuth_offset = azimuth_offset
        self._azimuth_flip = azimuth_flip
        self._scales = scales
        sp, sc = _scale_dict_to_tuples(scales)
        self._scale_polar = sp
        self._scale_cart = sc
        self._on_send = on_send
        self._last_mode: dict[int, str] = {}
        self._last_payload: dict[int, tuple[float, float, float]] = {}
        self._lisa_pwdes = bool(merged.get("lisa_pwdes", False))
        self._last_lisa_pwdes: dict[int, tuple[float, float, float, float, float]] = {}
        self._overflow_warned: set[int] = set()

    def _emit(self, address: str, value: Any) -> None:
        if self._on_send is not None:
            self._on_send(address, value)
        self._client.send_message(address, value)

    def _check_obj_index(self, oi: int) -> bool:
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

    def send_object_config_cartesian(self, obj_index: int, use_cartesian: bool) -> None:
        if not self._check_obj_index(obj_index):
            return
        if self._cfg:
            self._emit(self._cfg.format(i=obj_index), int(1 if use_cartesian else 0))

    def send_object_position(self, obj: AdmObject, block: ObjectBlock | None) -> None:
        if block is None:
            return
        pos = block.position
        oi = obj.osc_object_index
        if not self._check_obj_index(oi):
            return

        if self._lisa_pwdes:
            pl = adm_position_to_lisa_pwdes_payload(
                pos,
                self._azimuth_offset,
                self._azimuth_flip,
                scale_polar=self._scale_polar,
            )
            key = (pl[0], pl[1], pl[2], pl[3], pl[4])
            if self._last_lisa_pwdes.get(oi) == key:
                return
            self._last_lisa_pwdes[oi] = key
            self._emit(self._tpl_cart.format(i=oi), pl)
            return

        if self._xyz_only:
            if pos.mode == "cartesian":
                xyz = adm_cart_to_osc_xyz(pos)
            else:
                xyz = adm_polar_to_osc_xyz(pos, self._azimuth_offset, self._azimuth_flip)
            xyz = tuple(
                xyz[i] * self._scale_cart[i] * self._cart_extra_scale for i in range(3)
            )
            if self._soundscape_z0:
                xyz = (xyz[0], xyz[1], 0.0)
            if self._last_payload.get(oi) == xyz and self._last_mode.get(oi) == "xyz":
                return
            self._last_payload[oi] = xyz
            self._last_mode[oi] = "xyz"
            self._emit(self._tpl_cart.format(i=oi), list(xyz))
            return

        if pos.mode == "cartesian":
            if self._last_mode.get(oi) != "cart":
                self.send_object_config_cartesian(oi, True)
                self._last_mode[oi] = "cart"
                self._last_payload.pop(oi, None)  # mode 전환 시 캐시 무효화 (C7)
            xyz = adm_cart_to_osc_xyz(pos)
            xyz = tuple(xyz[i] * self._scale_cart[i] for i in range(3))
            if self._last_payload.get(oi) == xyz:
                return
            self._last_payload[oi] = xyz
            self._emit(self._tpl_cart.format(i=oi), list(xyz))
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
            assert self._tpl_polar is not None
            self._emit(self._tpl_polar.format(i=oi), list(aed))


# Suggested default endpoints per preset (UI may use these to prefill host/port fields).
# spatial_engine binds 127.0.0.1:9100 by default (see spatial_engine ADR 0006).
PRESET_DEFAULT_ENDPOINTS: dict[str, tuple[str, int]] = {
    "spatial_engine": ("127.0.0.1", 9100),
}


def create_osc_emitter(
    preset: str,
    host: str,
    port: int,
    *,
    azimuth_offset: float,
    azimuth_flip: bool,
    on_send: Callable[[str, Any], None] | None,
    scales: dict[str, float],
    custom_templates: dict[str, str] | None = None,
) -> AdmOscEmitter | PresetOscEmitter:
    """Instantiate the emitter for the given preset name."""
    sp, sc = _scale_dict_to_tuples(scales)
    # spatial_engine speaks ADM-OSC v1.0 verbatim → reuse the AdmOscEmitter path.
    # The only difference vs the default 'adm' preset is the suggested endpoint
    # exposed via PRESET_DEFAULT_ENDPOINTS.
    if preset in ("adm", "spatial_engine"):
        return AdmOscEmitter(
            host,
            port,
            None,
            azimuth_offset,
            azimuth_flip,
            on_send,
            scale_polar=sp,
            scale_cart=sc,
        )
    if preset == "custom":
        tpl = dict(custom_templates) if custom_templates else dict(DEFAULT_CUSTOM_TEMPLATES)
        return PresetOscEmitter(
            "custom",
            host,
            port,
            azimuth_offset,
            azimuth_flip,
            scales,
            on_send,
            tpl=tpl,
        )
    return PresetOscEmitter(
        preset,
        host,
        port,
        azimuth_offset,
        azimuth_flip,
        scales,
        on_send,
    )
