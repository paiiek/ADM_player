# -*- mode: python ; coding: utf-8 -*-
"""macOS ADM Player.app — 프로젝트 루트에서: python3 -m PyInstaller packaging/adm_player_mac.spec"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPEC).resolve().parent.parent
if not (PROJECT_ROOT / "adm_player").is_dir():
    PROJECT_ROOT = Path.cwd()

logo = PROJECT_ROOT / "adm_player" / "resources" / "LOGO_White.png"
datas: list[tuple[str, str]] = []
if logo.is_file():
    datas.append((str(logo), "adm_player/resources"))

# collect_all(PySide6) 은 Qt 프레임워크 symlink 충돌을 유발할 수 있어 훅에만 맡김
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
    "psutil._psosx",
    "psutil._psposix",
]

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "mac_gui_launcher.py")],
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
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ADM Player",
)

icns_path = PROJECT_ROOT / "packaging" / "ADMPlayer.icns"
icon_arg = str(icns_path) if icns_path.is_file() else None

app = BUNDLE(
    coll,
    name="ADM Player.app",
    icon=icon_arg,
    bundle_identifier="kr.dream-scape.admplayer",
    info_plist={
        "NSPrincipalClass": "NSApplication",
        "CFBundleName": "ADM Player",
        "CFBundleDisplayName": "ADM Player",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
