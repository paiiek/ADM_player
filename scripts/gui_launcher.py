#!/usr/bin/env python3
"""Cross-platform PyInstaller entry point for the ADM Player GUI.

PyInstaller runs the Analysis script as `__main__`, with no package context, so
`adm_player.gui_app` cannot be the entry directly — its top-level relative
imports (`from .interactive import ...`) would raise ImportError. This launcher
delegates to the GUI's `main()` using an absolute import, which works the same
way on macOS, Linux, and Windows builds.
"""

from __future__ import annotations


def main() -> int:
    from adm_player.gui_app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
