#!/usr/bin/env python3
"""PyInstaller 진입점: ADM Player GUI (macOS .app 번들)."""

from __future__ import annotations

import sys


def main() -> int:
    from adm_player.gui_app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
