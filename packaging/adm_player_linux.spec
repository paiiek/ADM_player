# -*- mode: python ; coding: utf-8 -*-
"""
Linux ADM Player — dist/adm-player/ COLLECT bundle.

From the project root:
    python -m PyInstaller packaging/adm_player_linux.spec

To wrap into AppImage afterwards, use linuxdeploy or appimagetool against
dist/adm-player/. The COLLECT directory is what you should ship.
"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPEC).resolve().parent.parent
if not (PROJECT_ROOT / "adm_player").is_dir():
    PROJECT_ROOT = Path.cwd()

logo = PROJECT_ROOT / "adm_player" / "resources" / "LOGO_White.png"
datas: list[tuple[str, str]] = []
if logo.is_file():
    datas.append((str(logo), "adm_player/resources"))

# Match the macOS spec — Linux has no extra hidden imports to add over the
# common set; PySide6 hooks pull Qt frameworks themselves.
hiddenimports = [
    "adm_player",
    "adm_player.adm_model",
    "adm_player.bwf",
    "adm_player.gui_app",
    "adm_player.interactive",
    "adm_player.osc_emit",
    "adm_player.osc_presets",
    "adm_player.playback",
    "numpy",
    "cffi",
    "_cffi_backend",
    "soundfile",
    "sounddevice",
    "pythonosc",
    "pythonosc.dispatcher",
    "pythonosc.osc_server",
    "pythonosc.udp_client",
    "psutil",
    "psutil._psposix",
    "psutil._pslinux",
]

# gui_app.py uses relative imports — PyInstaller's __main__ context cannot resolve
# them, so route through the cross-platform launcher that performs an absolute import.
entry_script = PROJECT_ROOT / "scripts" / "gui_launcher.py"

a = Analysis(
    [str(entry_script)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="adm-player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="adm-player",
)
