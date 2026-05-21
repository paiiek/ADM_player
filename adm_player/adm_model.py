from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Literal

from .bwf import normalize_track_uid


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall(parent: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in parent if _local(e.tag) == name]


def _find1(parent: ET.Element, name: str) -> ET.Element | None:
    for e in parent:
        if _local(e.tag) == name:
            return e
    return None


_PT_RE = re.compile(
    r"^PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?$",
    re.I,
)


def parse_adm_time(value: str | None, sample_rate: float) -> float | None:
    """Return time in seconds from ADM time string, or None if unknown."""
    if value is None or value == "":
        return None
    v = value.strip()
    if not v:
        return None
    m = _PT_RE.match(v)
    if m:
        h = int(m.group("h") or 0)
        mi = int(m.group("m") or 0)
        s = float(m.group("s") or 0)
        return h * 3600 + mi * 60 + s
    if ":" in v:
        parts = v.split(":")
        try:
            if len(parts) == 3:
                hh, mm, ss = parts
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
            if len(parts) == 2:
                mm, ss = parts
                return int(mm) * 60 + float(ss)
        except ValueError:
            return None
        return None
    if v.isdigit():
        return int(v) / sample_rate
    try:
        return float(v)
    except ValueError:
        return None


CoordMode = Literal["polar", "cartesian"]


@dataclass
class ObjectPosition:
    mode: CoordMode
    azimuth: float | None = None
    elevation: float | None = None
    distance: float | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None


@dataclass
class ObjectBlock:
    start_sec: float
    end_sec: float
    position: ObjectPosition


@dataclass
class AdmObject:
    adm_index: int
    object_id: str
    label: str
    type_definition: str
    track_uids: list[str] = field(default_factory=list)
    wav_channels: list[int] = field(default_factory=list)
    blocks: list[ObjectBlock] = field(default_factory=list)
    #: WAV 인터리브 채널 번호(1-based)와 동일하게 /adm/obj/N 에 사용 (chna로 알 수 있을 때)
    osc_object_index: int = 0


def _parse_position(block_el: ET.Element) -> ObjectPosition | None:
    # Dolby / common ADM: multiple <position coordinate="X|Y|Z">value</position> under audioBlockFormat
    x = y = z = None
    for sub in block_el:
        if _local(sub.tag) != "position":
            continue
        coord = (sub.get("coordinate") or sub.get("Coordinate") or "").strip()
        if not coord or sub.text is None:
            continue
        try:
            v = float(sub.text.strip())
        except ValueError:
            continue
        u = coord.upper()
        if u == "X":
            x = v
        elif u == "Y":
            y = v
        elif u == "Z":
            z = v
    if x is not None or y is not None or z is not None:
        return ObjectPosition(
            mode="cartesian",
            x=float(x if x is not None else 0.0),
            y=float(y if y is not None else 0.0),
            z=float(z if z is not None else 0.0),
        )

    pos_el = _find1(block_el, "position")
    if pos_el is None:
        for sub in block_el:
            if _local(sub.tag) == "position":
                pos_el = sub
                break
    if pos_el is None:
        return None
    cart = _find1(pos_el, "Cartesian") or _find1(pos_el, "cartesian")
    if cart is not None:
        xf = cart.get("X") or cart.get("x")
        yf = cart.get("Y") or cart.get("y")
        zf = cart.get("Z") or cart.get("z")
        try:
            return ObjectPosition(
                mode="cartesian",
                x=float(xf) if xf is not None else 0.0,
                y=float(yf) if yf is not None else 0.0,
                z=float(zf) if zf is not None else 0.0,
            )
        except (TypeError, ValueError):
            return ObjectPosition(mode="cartesian", x=0.0, y=0.0, z=0.0)
    sph = _find1(pos_el, "Spherical") or _find1(pos_el, "spherical")
    if sph is not None:
        az = sph.get("azimuth") or sph.get("Azimuth")
        el = sph.get("elevation") or sph.get("Elevation")
        dist = sph.get("distance") or sph.get("Distance")
        try:
            return ObjectPosition(
                mode="polar",
                azimuth=float(az) if az is not None else 0.0,
                elevation=float(el) if el is not None else 0.0,
                distance=float(dist) if dist is not None else 1.0,
            )
        except (TypeError, ValueError):
            return ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=1.0)
    az = pos_el.get("azimuth") or pos_el.get("Azimuth")
    el = pos_el.get("elevation") or pos_el.get("Elevation")
    dist = pos_el.get("distance") or pos_el.get("Distance")
    xf = pos_el.get("X") or pos_el.get("x")
    if xf is not None:
        yf = pos_el.get("Y") or pos_el.get("y")
        zf = pos_el.get("Z") or pos_el.get("z")
        try:
            return ObjectPosition(
                mode="cartesian",
                x=float(xf),
                y=float(yf) if yf is not None else 0.0,
                z=float(zf) if zf is not None else 0.0,
            )
        except (TypeError, ValueError):
            pass
    if az is not None or el is not None or dist is not None:
        try:
            return ObjectPosition(
                mode="polar",
                azimuth=float(az) if az is not None else 0.0,
                elevation=float(el) if el is not None else 0.0,
                distance=float(dist) if dist is not None else 1.0,
            )
        except (TypeError, ValueError):
            return ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=1.0)
    return None


def _channel_blocks(root: ET.Element, channel_format_id: str, sample_rate: float) -> list[ObjectBlock]:
    blocks: list[ObjectBlock] = []
    for chf in root.iter():
        if _local(chf.tag) != "audioChannelFormat":
            continue
        if chf.get("audioChannelFormatID") != channel_format_id:
            continue
        for bf in _findall(chf, "audioBlockFormat"):
            rtime = bf.get("rtime")
            duration = bf.get("duration")
            t0 = parse_adm_time(rtime, sample_rate)
            t1 = parse_adm_time(duration, sample_rate)
            if t0 is None:
                t0 = 0.0
            if t1 is None:
                t1 = 1e9
            else:
                t1 = t0 + t1
            pos = _parse_position(bf)
            if pos is None:
                pos = ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=1.0)
            blocks.append(ObjectBlock(start_sec=t0, end_sec=t1, position=pos))
    blocks.sort(key=lambda b: b.start_sec)
    return blocks


def _resolve_channel_format_for_object(root: ET.Element, pack_id: str) -> str | None:
    for apf in root.iter():
        if _local(apf.tag) != "audioPackFormat":
            continue
        if apf.get("audioPackFormatID") != pack_id:
            continue
        for ref in apf:
            if _local(ref.tag) != "audioChannelFormatIDRef":
                continue
            cid = ref.text
            if cid:
                return cid.strip()
    return None


def _pack_type_definition(root: ET.Element, pack_id: str) -> str | None:
    """Dolby Atmos 마스터 등은 typeDefinition이 audioObject가 아니라 audioPackFormat에 둘 수 있음."""
    if not pack_id:
        return None
    for apf in root.iter():
        if _local(apf.tag) != "audioPackFormat":
            continue
        if apf.get("audioPackFormatID") != pack_id:
            continue
        td = (apf.get("typeDefinition") or apf.get("TypeDefinition") or "").strip()
        return td or None
    return None


def _track_uid_refs(obj_el: ET.Element) -> list[str]:
    uids: list[str] = []
    for ref in obj_el:
        if _local(ref.tag) == "audioTrackUIDRef" and ref.text:
            uids.append(ref.text.strip())
    return uids


def parse_track_uid_metadata(axml: str) -> dict[str, tuple[str, str]]:
    """
    audioTrackUID -> (표시 이름, typeDefinition 문자열).
    채널(chna)과 매칭해 베드/오브젝트 구분 표시에 사용합니다.
    """
    root = ET.fromstring(axml)
    out: dict[str, tuple[str, str]] = {}
    for obj_el in root.iter():
        if _local(obj_el.tag) != "audioObject":
            continue
        pack_ref = _find1(obj_el, "audioPackFormatIDRef")
        pack_id = pack_ref.text.strip() if pack_ref is not None and pack_ref.text else ""
        type_def = (obj_el.get("typeDefinition") or obj_el.get("TypeDefinition") or "").strip()
        if not type_def:
            type_def = (_pack_type_definition(root, pack_id) or "").strip() or "?"
        oid = obj_el.get("audioObjectID") or obj_el.get("audioObjectId") or "?"
        label_el = _find1(obj_el, "audioObjectLabel")
        if label_el is not None and label_el.text:
            label = label_el.text.strip()
        else:
            label = (obj_el.get("audioObjectName") or oid).strip()
        for uid in _track_uid_refs(obj_el):
            out[normalize_track_uid(uid)] = (label, type_def)
    return out


def parse_adm_objects(axml: str, sample_rate: float, chna_uid_to_channel: dict[str, int]) -> list[AdmObject]:
    root = ET.fromstring(axml)
    objects: list[AdmObject] = []
    idx = 0
    for obj_el in root.iter():
        if _local(obj_el.tag) != "audioObject":
            continue
        pack_ref = _find1(obj_el, "audioPackFormatIDRef")
        pack_id = pack_ref.text.strip() if pack_ref is not None and pack_ref.text else ""
        type_def = (obj_el.get("typeDefinition") or obj_el.get("TypeDefinition") or "").strip()
        if not type_def:
            type_def = (_pack_type_definition(root, pack_id) or "").strip()
        if type_def.lower() != "objects":
            continue
        idx += 1
        oid = obj_el.get("audioObjectID") or obj_el.get("audioObjectId") or f"AO_{idx}"
        label_el = _find1(obj_el, "audioObjectLabel")
        if label_el is not None and label_el.text:
            label = label_el.text.strip()
        else:
            label = (obj_el.get("audioObjectName") or oid).strip()
        ch_format_id = _resolve_channel_format_for_object(root, pack_id) if pack_id else None
        blocks: list[ObjectBlock] = []
        if ch_format_id:
            blocks = _channel_blocks(root, ch_format_id, sample_rate)
        if not blocks:
            blocks = [
                ObjectBlock(
                    start_sec=0.0,
                    end_sec=1e9,
                    position=ObjectPosition(mode="polar", azimuth=0.0, elevation=0.0, distance=1.0),
                )
            ]
        uids = _track_uid_refs(obj_el)
        wav_ch = [
            chna_uid_to_channel[nu]
            for u in uids
            if (nu := normalize_track_uid(u)) in chna_uid_to_channel
        ]
        # OSC /adm/obj/N ← WAV 파일 인터리브 채널 번호(1-based) = 0-based 인덱스 + 1
        osc_oi = (min(wav_ch) + 1) if wav_ch else idx
        objects.append(
            AdmObject(
                adm_index=idx,
                object_id=oid,
                label=label,
                type_definition=type_def,
                track_uids=uids,
                wav_channels=wav_ch,
                blocks=blocks,
                osc_object_index=osc_oi,
            )
        )
    return objects


def active_block(blocks: list[ObjectBlock], t_sec: float) -> ObjectBlock | None:
    best: ObjectBlock | None = None
    for b in blocks:
        if b.start_sec <= t_sec < b.end_sec:
            if best is None or b.start_sec >= best.start_sec:
                best = b
    return best
