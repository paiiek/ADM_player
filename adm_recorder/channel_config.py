from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class ChannelRole(str, Enum):
    OBJECT = "object"
    BED = "bed"


@dataclass(frozen=True)
class BedLayout:
    """Fixed speaker positions (ADM Cartesian −1..1, front +Y, same as adm_player)."""

    id: str
    label: str
    # (label, x, y, z)
    speakers: tuple[tuple[str, float, float, float], ...]


# ── Lane F — canonical ADM/Atmos bed sets (SHARED with the live engine) ───────
# These five formats mirror core/src/render/BedLayouts.{h,cpp} BYTE-FOR-BYTE (a
# parity gate, test_bed_table_parity, binds them: labels + order + x/y/z @1e-4).
# Author both sides from the SAME (azimuth°, elevation°) Dolby Home / DAMF +
# ITU-R BS.775 specs and derive the ADM-Cartesian x/y/z with the identical
# formula (front +Y, right +X, up +Z):
#     x = sin(az)·cos(el),  y = cos(az)·cos(el),  z = sin(el)
# The LFE carries a nominal front-center placeholder (az0/el0 → (0,1,0)); it is
# NEVER panned — the engine routes it via an equal-power omni fan-out (§5).
# NOTE: the LEGACY object-cube tables (5_1 / 7_1) are intentionally left
# untouched (different, older positions); only the bed sets below are DAMF-exact.
def _bed_speaker(label: str, az_deg: float, el_deg: float) -> tuple[str, float, float, float]:
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    return (label, math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el))


# (label, az°, el°) in Dolby channel order. LFE spec = (0, 0) → (0,1,0) placeholder.
_BED_SPECS: dict[str, tuple[tuple[str, float, float], ...]] = {
    "5_1_2": (
        ("L", -30, 0), ("R", 30, 0), ("C", 0, 0), ("LFE", 0, 0),
        ("Ls", -110, 0), ("Rs", 110, 0), ("Ltf", -45, 45), ("Rtf", 45, 45),
    ),
    "5_1_4": (
        ("L", -30, 0), ("R", 30, 0), ("C", 0, 0), ("LFE", 0, 0),
        ("Ls", -110, 0), ("Rs", 110, 0), ("Ltf", -45, 45), ("Rtf", 45, 45),
        ("Ltr", -135, 45), ("Rtr", 135, 45),
    ),
    "7_1_2": (
        ("L", -30, 0), ("R", 30, 0), ("C", 0, 0), ("LFE", 0, 0),
        ("Lss", -90, 0), ("Rss", 90, 0), ("Lrs", -135, 0), ("Rrs", 135, 0),
        ("Ltf", -45, 45), ("Rtf", 45, 45),
    ),
    "7_1_4": (
        ("L", -30, 0), ("R", 30, 0), ("C", 0, 0), ("LFE", 0, 0),
        ("Lss", -90, 0), ("Rss", 90, 0), ("Lrs", -135, 0), ("Rrs", 135, 0),
        ("Ltf", -45, 45), ("Rtf", 45, 45), ("Ltr", -135, 45), ("Rtr", 135, 45),
    ),
    # 9.1.6 channel ORDER is the single source of truth for the ADM fixture +
    # the C++ table (test_bed_table_parity pins C++ ↔ this order).
    "9_1_6": (
        ("L", -30, 0), ("R", 30, 0), ("C", 0, 0), ("LFE", 0, 0),
        ("Lss", -90, 0), ("Rss", 90, 0), ("Lrs", -135, 0), ("Rrs", 135, 0),
        ("Lw", -60, 0), ("Rw", 60, 0), ("Ltf", -45, 45), ("Rtf", 45, 45),
        ("Ltm", -90, 45), ("Rtm", 90, 45), ("Ltr", -135, 45), ("Rtr", 135, 45),
    ),
}

_BED_LABELS: dict[str, str] = {
    "5_1_2": "5.1.2 (8)", "5_1_4": "5.1.4 (10)", "7_1_2": "7.1.2 (10)",
    "7_1_4": "7.1.4 (12)", "9_1_6": "9.1.6 (16)",
}


def _bed_layout(bid: str) -> BedLayout:
    specs = _BED_SPECS[bid]
    return BedLayout(bid, _BED_LABELS[bid],
                     tuple(_bed_speaker(lbl, az, el) for (lbl, az, el) in specs))


# Dolby/ITU-normalized positions (aligned with in-app OSC/playback axes)
_LAYOUTS: tuple[BedLayout, ...] = (
    BedLayout(
        "stereo",
        "Stereo (2)",
        (("L", -0.9, 0.9, 0.0), ("R", 0.9, 0.9, 0.0)),
    ),
    BedLayout(
        "5_1",
        "5.1 (6)",
        (
            ("L", -0.9, 0.9, 0.0),
            ("R", 0.9, 0.9, 0.0),
            ("C", 0.0, 1.0, 0.0),
            ("LFE", 0.0, 0.5, -0.2),
            ("Ls", -0.9, -0.9, 0.0),
            ("Rs", 0.9, -0.9, 0.0),
        ),
    ),
    BedLayout(
        "7_1",
        "7.1 (8)",
        (
            ("L", -0.9, 0.9, 0.0),
            ("R", 0.9, 0.9, 0.0),
            ("C", 0.0, 1.0, 0.0),
            ("LFE", 0.0, 0.5, -0.2),
            ("Lss", -0.95, 0.0, 0.0),
            ("Rss", 0.95, 0.0, 0.0),
            ("Lsr", -0.9, -0.9, 0.0),
            ("Rsr", 0.9, -0.9, 0.0),
        ),
    ),
    # Lane F — DAMF-exact bed sets (mirror core/src/render/BedLayouts; parity-gated).
    _bed_layout("5_1_2"),
    _bed_layout("5_1_4"),
    _bed_layout("7_1_2"),
    _bed_layout("7_1_4"),
    _bed_layout("9_1_6"),
    BedLayout(
        "objects_only",
        "Objects only (0 bed)",
        (),
    ),
)

BED_LAYOUTS_BY_ID: dict[str, BedLayout] = {x.id: x for x in _LAYOUTS}


@dataclass
class ChannelMapState:
    n_channels: int = 24
    bed_layout_id: str = "7_1_4"
    roles: list[ChannelRole] = field(default_factory=list)

    MAX_CHANNELS: ClassVar[int] = 128

    def __post_init__(self) -> None:
        self._ensure_roles()

    def _ensure_roles(self) -> None:
        n = max(1, min(self.MAX_CHANNELS, int(self.n_channels)))
        self.n_channels = n
        if len(self.roles) != n:
            old = list(self.roles)
            self.roles = [ChannelRole.OBJECT for _ in range(n)]
            for i in range(min(len(old), n)):
                self.roles[i] = old[i]

    def set_n_channels(self, n: int) -> None:
        self.n_channels = n
        self._ensure_roles()

    def bed_layout(self) -> BedLayout:
        return BED_LAYOUTS_BY_ID.get(self.bed_layout_id, BED_LAYOUTS_BY_ID["7_1_4"])

    def bed_channel_indices_1based(self) -> list[int]:
        return [i + 1 for i, r in enumerate(self.roles) if r == ChannelRole.BED]

    def validate_bed_count(self) -> tuple[bool, str]:
        layout = self.bed_layout()
        n_bed = sum(1 for r in self.roles if r == ChannelRole.BED)
        need = len(layout.speakers)
        if n_bed != need:
            return (
                False,
                f"Bed track count must be {need} for {layout.label}. Current: {n_bed}.",
            )
        return True, ""

    def bed_assignments_ordered(self) -> list[tuple[int, tuple[str, float, float, float]]]:
        """Pairs (1-based WAV channel, (label,x,y,z)) in channel order ↔ layout speakers."""
        layout = self.bed_layout()
        idx = sorted(self.bed_channel_indices_1based())
        if len(idx) != len(layout.speakers):
            return []
        return list(zip(idx, layout.speakers))
