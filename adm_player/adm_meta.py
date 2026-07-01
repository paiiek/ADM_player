"""TRACK A3 (ADR 0029) — build frame-keyed `MetaRecord`s from ADM objects.

`class MetaRecordBuilder` produces one `MetaRecord` per object whose
position / gain / width / active state CHANGED at a block — the sidecar
counterpart to `AdmOscEmitter.send_object_position`. It reuses the EXACT
coordinate conversions (`adm_polar_to_osc_aed` / `adm_cart_to_osc_xyz`,
including the ADR 0006 az-flip/offset + distance ÷20 normalisation and the
per-axis scale) AND mirrors `AdmOscEmitter`'s per-object dedup caches so the
sidecar wire semantics are BIT-CONSISTENT with the OSC payload for the same
block. Records carry FULL fields (a0/a1/a2 position + gain_lin + width_rad +
flags active/mute) per plan Amendment 4 — never position-only.

Dedup state spans blocks, so this is a stateful builder (a pure free function
could not detect "changed since last block"); construct once per playback and
call `.build(objects, t0, frame_index)` per block.
"""

from __future__ import annotations

from .adm_model import AdmObject, active_block
from .meta_sink import (
    META_COORD_CART,
    META_COORD_POLAR,
    META_FLAG_ACTIVE,
    META_FLAG_MUTE,
    MetaRecord,
)
from .osc_emit import (
    MAX_OSC_OBJECTS,
    adm_cart_to_osc_xyz,
    adm_polar_to_osc_aed,
)

# Default full-field values for objects that never authored gain/width (the OSC
# path simply never emits those fields; the sidecar carries a coherent full
# state, so it substitutes unity gain / zero width — the engine defaults).
_DEFAULT_GAIN_LIN: float = 1.0
_DEFAULT_WIDTH_RAD: float = 0.0


class MetaRecordBuilder:
    """Stateful sidecar-record builder mirroring `AdmOscEmitter`'s dedup + coords.

    Constructor mirrors `AdmOscEmitter`: same ``azimuth_offset`` / ``azimuth_flip``
    / ``scale_polar`` / ``scale_cart`` so the produced ``a0/a1/a2`` equal the OSC
    aed/xyz payload for the same block.
    """

    def __init__(
        self,
        azimuth_offset: float = 0.0,
        azimuth_flip: bool = False,
        *,
        scale_polar: tuple[float, float, float] = (1.0, 1.0, 1.0),
        scale_cart: tuple[float, float, float] = (1.0, 1.0, 1.0),
        max_objects: int = MAX_OSC_OBJECTS,
    ) -> None:
        self._azimuth_offset = azimuth_offset
        self._azimuth_flip = azimuth_flip
        self._scale_polar = scale_polar
        self._scale_cart = scale_cart
        self._max_objects = max_objects
        # Per-object dedup caches — EXACT mirror of AdmOscEmitter's fields.
        self._last_mode: dict[int, str] = {}
        self._last_payload: dict[int, tuple[float, float, float]] = {}
        self._last_gain: dict[int, float] = {}
        self._last_width: dict[int, float] = {}
        self._active: dict[int, bool] = {}

    def build(self, objects: list[AdmObject], t0: float, frame_index: int) -> list[MetaRecord]:
        """Return the records for every object whose state changed at this block.

        ``t0`` is the block's start time in seconds (``pos / sr`` — same value
        the OSC loop passes to ``active_block``). ``frame_index`` is the block's
        absolute first PCM frame (the sink re-stamps it authoritatively).
        """
        records: list[MetaRecord] = []
        for obj in objects:
            rec = self._build_one(obj, t0, frame_index)
            if rec is not None:
                records.append(rec)
        return records

    def _build_one(self, obj: AdmObject, t0: float, frame_index: int) -> MetaRecord | None:
        oi = obj.osc_object_index  # read before the None check for the active flag (R6)
        blk = active_block(obj.blocks, t0)

        if blk is None:
            # Silence: mirror AdmOscEmitter (record inactive, emit nothing over
            # OSC). Emit ONE falling-edge record so the engine can deactivate,
            # holding the last known position/gain/width (active bit cleared).
            was_active = self._active.get(oi, False)
            self._active[oi] = False
            if was_active and oi in self._last_payload:
                a0, a1, a2 = self._last_payload[oi]
                coord_mode = META_COORD_CART if self._last_mode.get(oi) == "cart" else META_COORD_POLAR
                gain_lin = self._last_gain.get(oi, _DEFAULT_GAIN_LIN)
                width_rad = self._last_width.get(oi, _DEFAULT_WIDTH_RAD)
                flags = META_FLAG_MUTE if gain_lin == 0.0 else 0  # active bit cleared
                return MetaRecord(
                    frame_index=frame_index,
                    obj_id=oi,
                    coord_mode=coord_mode,
                    a0=a0,
                    a1=a1,
                    a2=a2,
                    gain_lin=gain_lin,
                    width_rad=width_rad,
                    flags=flags,
                )
            return None

        # Index gating mirrors AdmOscEmitter._check_obj_index (silent skip here;
        # the OSC path already warns once per out-of-range index).
        if oi < 1 or oi > self._max_objects:
            return None

        changed = False

        # silent→active edge: drop caches so the re-entering block re-asserts
        # position AND gain/width (mirror AdmOscEmitter, R6).
        if not self._active.get(oi, False):
            self._last_payload.pop(oi, None)
            self._last_gain.pop(oi, None)
            self._last_width.pop(oi, None)
            self._active[oi] = True
            changed = True

        pos = blk.position
        if pos.mode == "cartesian":
            coord_mode = META_COORD_CART
            if self._last_mode.get(oi) != "cart":
                self._last_mode[oi] = "cart"
                # mode switch invalidates caches (mirror AdmOscEmitter C7/R6).
                self._last_payload.pop(oi, None)
                self._last_gain.pop(oi, None)
                self._last_width.pop(oi, None)
                changed = True
            xyz = adm_cart_to_osc_xyz(pos, self._azimuth_offset, self._azimuth_flip)
            payload = tuple(xyz[i] * self._scale_cart[i] for i in range(3))
        else:
            coord_mode = META_COORD_POLAR
            if self._last_mode.get(oi) != "polar":
                self._last_mode[oi] = "polar"
                self._last_payload.pop(oi, None)
                self._last_gain.pop(oi, None)
                self._last_width.pop(oi, None)
                changed = True
            aed = adm_polar_to_osc_aed(pos, self._azimuth_offset, self._azimuth_flip)
            payload = (
                aed[0] * self._scale_polar[0],
                aed[1] * self._scale_polar[1],
                aed[2] * self._scale_polar[2],
            )

        if self._last_payload.get(oi) != payload:
            self._last_payload[oi] = payload
            changed = True

        # gain/width per-field dedup (``is not None`` so gain==0.0 mute counts).
        gain = blk.gain
        if gain is not None and float(gain) != self._last_gain.get(oi):
            self._last_gain[oi] = float(gain)
            changed = True
        width = blk.width
        if width is not None and float(width) != self._last_width.get(oi):
            self._last_width[oi] = float(width)
            changed = True

        if not changed:
            return None

        a0, a1, a2 = self._last_payload[oi]
        gain_lin = self._last_gain.get(oi, _DEFAULT_GAIN_LIN)
        width_rad = self._last_width.get(oi, _DEFAULT_WIDTH_RAD)
        flags = META_FLAG_ACTIVE | (META_FLAG_MUTE if gain_lin == 0.0 else 0)
        return MetaRecord(
            frame_index=frame_index,
            obj_id=oi,
            coord_mode=coord_mode,
            a0=a0,
            a1=a1,
            a2=a2,
            gain_lin=gain_lin,
            width_rad=width_rad,
            flags=flags,
        )
