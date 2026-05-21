from __future__ import annotations

import sys
from pathlib import Path


def resolve_app_logo_path() -> Path | None:
    """Find logo under repo or adm_player resources."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            root = Path(meipass)
            candidates.append(root / "adm_player" / "resources" / "LOGO_White.png")
    here = Path(__file__).resolve().parent
    repo = here.parent
    candidates.append(here / "resources" / "LOGO_White.png")
    candidates.append(repo / "adm_player" / "resources" / "LOGO_White.png")
    candidates.append(repo / "LOGO_White.png")
    for c in candidates:
        if c.is_file():
            return c
    return None
