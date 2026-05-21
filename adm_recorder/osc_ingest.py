from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from pythonosc.dispatcher import Dispatcher

from .osc_udp_server import start_threading_osc_udp

FrameSupplier = Callable[[], int]

OscXYZCallback = Callable[[int, int, float, float, float], None]
RawOscCallback = Callable[[str, list[Any]], None]


def _clamp11(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))


def aed_deg_to_xyz(az: float, el: float, dist: float) -> tuple[float, float, float]:
    """Same normalization as adm_player adm_polar_to_osc_xyz."""
    d = float(dist if dist is not None else 1.0)
    if d > 1.0:
        d = min(1.0, max(0.0, d / 10.0))
    else:
        d = min(1.0, max(0.0, d))
    az_r = math.radians(float(az))
    el_r = math.radians(float(el))
    x = d * math.cos(el_r) * math.sin(az_r)
    y = d * math.cos(el_r) * math.cos(az_r)
    z = d * math.sin(el_r)
    return _clamp11(x), _clamp11(y), _clamp11(z)


def lisa_norm_to_az_deg(norm: float) -> float:
    lo_deg, hi_deg = -179.64, 180.0
    lo_n, hi_n = 0.001, 1.0
    t = (float(norm) - lo_n) / (hi_n - lo_n)
    t = max(0.0, min(1.0, t))
    return lo_deg + t * (hi_deg - lo_deg)


def lisa_pwdes_to_xyz(args: list[float]) -> tuple[float, float, float]:
    """L-ISA pwdes 5 floats → Cartesian (-1..1)."""
    if len(args) < 4:
        return 0.0, 0.0, 0.0
    az_n = float(args[0])
    dist_lisa = float(args[2])
    el_n = float(args[3])
    az_deg = lisa_norm_to_az_deg(az_n)
    el_deg = max(0.0, min(90.0, el_n * 90.0))
    d_adm = (dist_lisa - 0.1) / 0.45 - 1.0
    d_adm = max(-1.0, min(1.0, d_adm))
    d_sph = (d_adm + 1.0) * 0.5
    az_r = math.radians(az_deg)
    el_r = math.radians(el_deg)
    x = d_sph * math.cos(el_r) * math.sin(az_r)
    y = d_sph * math.cos(el_r) * math.cos(az_r)
    z = d_sph * math.sin(el_r)
    return _clamp11(x), _clamp11(y), _clamp11(z)


class OscIngestRouter:
    """
    Convert renderer / ADM Player OSC into ADM-normalized Cartesian (-1..1) for (ch, x, y, z).
    Presets invert the same scaling as PresetOscEmitter on the player side.
    preset: adm | spat_revolution | lisa | soundscape | adamson_fm | afc_image | custom
    """

    def __init__(
        self,
        preset: str,
        get_frame: FrameSupplier,
        on_xyz: OscXYZCallback,
        custom_patterns: dict[str, str] | None = None,
        on_raw: RawOscCallback | None = None,
    ) -> None:
        self._preset = preset
        self._get_frame = get_frame
        self._on_xyz = on_xyz
        self._custom = dict(custom_patterns) if custom_patterns else {}
        self._on_raw = on_raw

    def _emit(self, ch: int, x: float, y: float, z: float) -> None:
        if ch < 1:
            return
        self._on_xyz(ch, int(self._get_frame()), x, y, z)

    def register(self, d: Dispatcher) -> None:
        p = self._preset

        def adm_like(addr: str, args: list[Any]) -> None:
            m = re.search(r"/obj/(\d+)/(xyz|aed)$", addr)
            if not m:
                return
            ch = int(m.group(1))
            kind = m.group(2)
            if kind == "xyz" and len(args) >= 3:
                self._emit(ch, float(args[0]), float(args[1]), float(args[2]))
            elif kind == "aed" and len(args) >= 3:
                x, y, z = aed_deg_to_xyz(float(args[0]), float(args[1]), float(args[2]))
                self._emit(ch, x, y, z)

        def fm_xyz(addr: str, args: list[Any]) -> None:
            m = re.search(r"/fm/obj/pos/xyz/(\d+)$", addr)
            if m and len(args) >= 3:
                self._emit(int(m.group(1)), float(args[0]), float(args[1]), float(args[2]))

        def afc_phys(addr: str, args: list[Any]) -> None:
            m = re.search(r"/OBA/Object/PhysicalPosition/(\d+)$", addr)
            if m and len(args) >= 3:
                ch = int(m.group(1))
                x, y, z = float(args[0]) * 0.1, float(args[1]) * 0.1, float(args[2]) * 0.1
                self._emit(ch, x, y, z)

        def spat_src(addr: str, args: list[Any]) -> None:
            """Spat Revolution: undo player cart_extra_scale=10."""
            m = re.search(r"/source/(\d+)/xyz$", addr)
            if m and len(args) >= 3:
                s = 10.0
                self._emit(
                    int(m.group(1)),
                    float(args[0]) / s,
                    float(args[1]) / s,
                    float(args[2]) / s,
                )

        def dba_pos(addr: str, args: list[Any]) -> None:
            m = re.search(r"/source_position/(\d+)$", addr)
            if m and len(args) >= 3:
                x, y, z = float(args[0]) * 0.1, float(args[1]) * 0.1, float(args[2]) * 0.1
                self._emit(int(m.group(1)), x, y, z)

        def lisa_pwdes(addr: str, args: list[Any]) -> None:
            m = re.search(r"/ext/src/(\d+)/pwdes$", addr)
            if m and len(args) >= 4:
                x, y, z = lisa_pwdes_to_xyz([float(a) for a in args[:5]])
                self._emit(int(m.group(1)), x, y, z)

        def custom_handler(addr: str, args: list[Any]) -> None:
            tpl_cart = self._custom.get("cart", "/custom/obj/{i}/xyz")
            segs = tpl_cart.split("{i}", 1)
            if len(segs) != 2:
                return
            pat = "^" + re.escape(segs[0]) + r"(\d+)" + re.escape(segs[1]) + "$"
            m = re.match(pat, addr)
            if m and len(args) >= 3:
                self._emit(int(m.group(1)), float(args[0]), float(args[1]), float(args[2]))

        handlers: list[Callable[[str, list[Any]], None]] = []
        if p == "adm":
            handlers = [adm_like]
        elif p == "spat_revolution":
            handlers = [spat_src]
        elif p == "lisa":
            handlers = [lisa_pwdes]
        elif p == "soundscape":
            handlers = [dba_pos]
        elif p == "adamson_fm":
            handlers = [fm_xyz]
        elif p == "afc_image":
            handlers = [afc_phys]
        elif p == "custom":
            handlers = [custom_handler]
        else:
            handlers = [adm_like]

        def fallback(addr: str, *args: Any) -> None:
            args_l = list(args)
            if self._on_raw is not None:
                try:
                    self._on_raw(addr, args_l)
                except Exception:
                    pass
            for h in handlers:
                h(addr, args_l)

        d.set_default_handler(fallback)


def start_osc_server(
    host: str,
    port: int,
    router: OscIngestRouter,
):
    """
    Listen for position OSC on ``host:port``.
    Use host ``127.0.0.1`` for localhost-only (matches clients sending to 127.0.0.1).
    Use ``0.0.0.0`` for all interfaces.
    """
    d = Dispatcher()
    router.register(d)
    return start_threading_osc_udp((str(host), int(port)), d)
