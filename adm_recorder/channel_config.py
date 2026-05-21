from __future__ import annotations

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
    BedLayout(
        "7_1_2",
        "7.1.2 (10)",
        (
            ("L", -0.9, 0.9, 0.0),
            ("R", 0.9, 0.9, 0.0),
            ("C", 0.0, 1.0, 0.0),
            ("LFE", 0.0, 0.5, -0.2),
            ("Lss", -0.95, 0.0, 0.0),
            ("Rss", 0.95, 0.0, 0.0),
            ("Lsr", -0.9, -0.9, 0.0),
            ("Rsr", 0.9, -0.9, 0.0),
            ("Tfl", -0.5, 0.7, 0.75),
            ("Tfr", 0.5, 0.7, 0.75),
        ),
    ),
    BedLayout(
        "7_1_4",
        "7.1.4 (12)",
        (
            ("L", -0.9, 0.9, 0.0),
            ("R", 0.9, 0.9, 0.0),
            ("C", 0.0, 1.0, 0.0),
            ("LFE", 0.0, 0.5, -0.2),
            ("Lss", -0.95, 0.0, 0.0),
            ("Rss", 0.95, 0.0, 0.0),
            ("Lsr", -0.9, -0.9, 0.0),
            ("Rsr", 0.9, -0.9, 0.0),
            ("Ltf", -0.7, 0.5, 0.75),
            ("Rtf", 0.7, 0.5, 0.75),
            ("Ltr", -0.7, -0.5, 0.75),
            ("Rtr", 0.7, -0.5, 0.75),
        ),
    ),
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
