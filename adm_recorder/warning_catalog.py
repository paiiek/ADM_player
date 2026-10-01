"""Operator titles for the engine's ``/sys/warning`` codes — P-175.

After the engine's P-172 every ``/sys/warning`` carries a catalog code
(``,iiss <i> <i> "<code>" "<detail>"``), and the recorder printed
``f"{category} ({detail})"`` — the raw code, which an operator cannot read.
The engine's single source of operator strings is its
``ui/warning_catalog.json`` (spatial_engine repo). This repo is separate, so it
carries a VENDORED SNAPSHOT of just the titles (``warning_titles.json`` next to
this file), the same way ``adm_player/ipc_sink.py`` mirrors the engine's
``RingHeader.h`` constants — plus a sync check the mirror never had:

    python scripts/sync_warning_titles.py --engine-root <spatial_engine checkout>          # check
    python scripts/sync_warning_titles.py --engine-root <spatial_engine checkout> --write  # refresh

A code missing from the snapshot (or a missing/unreadable snapshot) falls back
to the raw code, so the recorder never shows LESS than it did before.

Pure Python (no PySide6 / PortAudio) so it is unit-tested headless, like
``sys_warning.py`` (P-171).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).with_name("warning_titles.json")
SNAPSHOT_SCHEMA = 1


def titles_from_engine_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, str]]:
    """``{code: {"title": KR, "title_en": EN?}}`` from the engine catalog's
    parsed JSON (``{"schema": 1, "codes": {...}}``) — exactly what the snapshot
    stores, so the sync check compares like with like."""
    if catalog.get("schema") != 1:
        raise ValueError(f"unexpected engine catalog schema {catalog.get('schema')!r}")
    out: dict[str, dict[str, str]] = {}
    for code, entry in sorted(catalog["codes"].items()):
        t = {"title": str(entry["title"])}
        if entry.get("title_en"):
            t["title_en"] = str(entry["title_en"])
        out[code] = t
    return out


def snapshot_drift(snapshot_codes: dict[str, dict[str, str]],
                   engine_codes: dict[str, dict[str, str]]) -> list[str]:
    """Human-readable differences, empty when the snapshot is in sync."""
    drift = []
    for code in sorted(set(engine_codes) - set(snapshot_codes)):
        drift.append(f"missing from snapshot: {code}")
    for code in sorted(set(snapshot_codes) - set(engine_codes)):
        drift.append(f"no longer in the engine catalog: {code}")
    for code in sorted(set(snapshot_codes) & set(engine_codes)):
        if snapshot_codes[code] != engine_codes[code]:
            drift.append(f"title changed: {code}")
    return drift


@lru_cache(maxsize=1)
def load_titles() -> dict[str, dict[str, str]]:
    """The vendored titles; ``{}`` (every code falls back to raw) if the
    snapshot is absent or unreadable — never an exception into the GUI."""
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("schema") != SNAPSHOT_SCHEMA or not isinstance(data.get("codes"), dict):
        return {}
    return data["codes"]


def warning_title(code: str, locale: str = "ko") -> str | None:
    """Catalog title for ``code``: Korean by default; ``locale="en"`` gives the
    English title, falling back to Korean when the catalog has none. ``None``
    for a code the snapshot does not know."""
    entry = load_titles().get(code)
    if entry is None:
        return None
    if locale == "en" and entry.get("title_en"):
        return entry["title_en"]
    return entry.get("title") or None


def format_engine_warning(category: str, detail: str, locale: str = "ko") -> str:
    """The operator line for one ``/sys/warning``:
    ``<title> (<code> <detail>)`` when the code is catalogued — the raw code and
    the engine's detail are KEPT, they are the only exact facts — else the
    pre-P-175 ``<code> (<detail>)``."""
    title = warning_title(category, locale)
    if title is None:
        return f"{category} ({detail})" if detail else category
    tail = f"{category} {detail}".strip()
    return f"{title} ({tail})"
