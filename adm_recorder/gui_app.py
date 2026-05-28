"""
ADM Recorder — PySide6 GUI.
Multichannel audio + time-aligned OSC → WAV with embedded ADM metadata.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import sounddevice as sd
from PySide6.QtCore import Qt, QSettings, QTimer, Signal, QObject, QSize, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QMouseEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from adm_player.gui_app import resolve_app_logo_path
from adm_player.osc_presets import DEFAULT_CUSTOM_TEMPLATES
from adm_player.playback import coerce_audio_device

from .audio_recorder import AudioCapture
from .bed_layout_dialog import BedLayoutDialog
from .bwf_atmos_writer import finalize_bwf_session
from .channel_map_dialog import ChannelMapDialog
from .channel_config import BED_LAYOUTS_BY_ID, ChannelMapState, ChannelRole
from .input_matrix_dialog import InputMatrixDialog
from .input_routing import (
    load_route_from_settings,
    resize_route,
    save_route_to_settings,
)
from .engine_echo import start_engine_echo_ingest
from .osc_control import OscControlBridge, start_osc_control_server
from .osc_ingest import OscIngestRouter, start_osc_server
from .osc_record_throttle import OSC_POSITION_RECORD_HZ, make_position_callback
from .timeline_store import ObjectBlock, TimelineStore, events_to_object_blocks

# Dark UI aligned with ADM Player
APP_STYLESHEET = """
QWidget { background-color: #1a1a1a; color: #e8e8e8; font-size: 11pt; }
QMainWindow { background-color: #1a1a1a; }
QGroupBox { font-size: 13pt; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #c8c8c8; }
QGroupBox {
    border: 1px solid #3d3d3d; border-radius: 8px; margin-top: 10px; padding: 12px;
}
QTableWidget {
    gridline-color: #3a3a3a; background: #141414; alternate-background-color: #1c1c1c;
    border: 1px solid #3d3d3d; border-radius: 6px;
}
QHeaderView::section { background: #2a2a2a; color: #ddd; padding: 6px; border: none; }
QLineEdit, QSpinBox, QComboBox {
    background: #242424; border: 1px solid #4a4a4a; border-radius: 4px;
    padding: 4px 8px; min-height: 22px;
}
QPushButton {
    background: #3d5a88; border: none; border-radius: 6px;
    padding: 8px 16px; color: #fff; font-weight: 600;
}
QPushButton:hover { background: #4a6fa0; }
QPushButton:pressed { background: #2f4a6f; }
QPushButton#stopBtn { background: #8f4d3d; }
QPushButton#stopBtn:hover { background: #a65c4a; }
QPlainTextEdit { background: #141414; border: 1px solid #3d3d3d; font-family: Menlo; font-size: 9pt; }
#recTimeLabel {
    font-family: Menlo; font-size: 18pt; font-weight: 700; color: #f5f5f5;
    padding: 8px; background: #121212; border: 1px solid palette(mid); border-radius: 8px;
}
"""

RECORD_PRESET_ENTRIES: list[tuple[str, str]] = [
    ("adm", "ADM"),
    ("spat_revolution", "Spat /10"),
    ("lisa", "L-ISA"),
    ("soundscape", "Soundscape ×0.1"),
    ("adamson_fm", "Fletcher"),
    ("afc_image", "AFC ×0.1"),
    ("custom", "Custom"),
]


class ClickableLogoLabel(QLabel):
    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(url)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl(self._url))
        super().mouseReleaseEvent(event)


def _unique_output_path(path: Path) -> Path:
    """If ``path`` already exists, return ``stem_1``, ``stem_2``, … with the same suffix."""
    path = path.expanduser()
    try:
        if not path.exists():
            return path
    except OSError:
        return path
    parent = path.parent
    stem = path.stem
    suf = path.suffix if path.suffix else ".wav"
    n = 1
    while n < 1_000_000:
        cand = parent / f"{stem}_{n}{suf}"
        try:
            if not cand.exists():
                return cand
        except OSError:
            return cand
        n += 1
    return parent / f"{stem}_{n}{suf}"


class _OscLogBridge(QObject):
    line = Signal(str)


class _LevelsBridge(QObject):
    peaks = Signal(list)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ADM Recorder")
        self.setMinimumSize(QSize(1000, 620))
        self.resize(1200, 820)
        self._settings = QSettings()
        self._cmap = ChannelMapState()
        self._timeline = TimelineStore()
        self._recording = False
        self._capture: AudioCapture | None = None
        self._osc_srv = None
        self._osc_thread = None
        self._echo_session = None  # set in engine-echo source mode
        self._temp_wav: Path | None = None
        self._sr = 48000
        self._osc_log_bridge = _OscLogBridge()
        self._osc_log_bridge.line.connect(self._append_osc_log)
        self._levels_bridge = _LevelsBridge()
        self._ctrl_bridge = OscControlBridge()
        self._ctrl_srv = None
        self._ctrl_thread = None
        self._ctrl_listen_port: int | None = None
        self._ctrl_listen_host: str | None = None
        self._route: np.ndarray | None = None
        self._osc_ctrl_record_pending = False
        self._osc_ctrl_stop_pending = False

        _logo_path = resolve_app_logo_path()
        if _logo_path is not None and _logo_path.is_file():
            self.setWindowIcon(QIcon(str(_logo_path)))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        left_inner = QWidget()
        left_inner.setMinimumWidth(420)
        ll = QVBoxLayout(left_inner)
        ll.setSpacing(12)
        left_scroll.setWidget(left_inner)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        right_inner = QWidget()
        right_inner.setMinimumWidth(420)
        rl = QVBoxLayout(right_inner)
        rl.setSpacing(12)
        right_scroll.setWidget(right_inner)

        split.addWidget(left_scroll)
        split.addWidget(right_scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([580, 580])
        root.addWidget(split, 1)

        # Left: I/O
        io = QGroupBox("I/O")
        io.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        ig = QGridLayout(io)
        ig.setColumnStretch(1, 1)
        ig.setHorizontalSpacing(10)
        ig.setVerticalSpacing(8)
        ig.addWidget(QLabel("SR"), 0, 0)
        sr_lbl = QLabel("48000 Hz")
        sr_lbl.setStyleSheet("color: #a0a0a0;")
        ig.addWidget(sr_lbl, 0, 1)
        ig.addWidget(QLabel("Tracks"), 1, 0)
        self._ch_spin = QSpinBox()
        self._ch_spin.setRange(1, 128)
        self._ch_spin.setValue(24)
        self._ch_spin.valueChanged.connect(self._on_ch_count_changed)
        ig.addWidget(self._ch_spin, 1, 1)
        ig.addWidget(QLabel("Device"), 2, 0)
        dev_row = QHBoxLayout()
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(320)
        self._btn_audio_refresh = QPushButton("Refresh")
        self._btn_audio_refresh.setFixedWidth(88)
        self._btn_audio_refresh.clicked.connect(self._fill_input_devices)
        dev_row.addWidget(self._device_combo, 1)
        dev_row.addWidget(self._btn_audio_refresh)
        ig.addLayout(dev_row, 2, 1)
        ig.addWidget(QLabel("Patch"), 3, 0)
        imap_row = QHBoxLayout()
        self._btn_input_matrix = QPushButton("Matrix…")
        self._btn_input_matrix.clicked.connect(self._open_input_matrix)
        imap_row.addWidget(self._btn_input_matrix)
        imap_row.addStretch(1)
        ig.addLayout(imap_row, 3, 1)
        ig.addWidget(
            QLabel("OSC in"),
            4,
            0,
        )
        self._osc_port = QSpinBox()
        self._osc_port.setRange(1, 65535)
        self._osc_port.setValue(9010)
        osc_row = QHBoxLayout()
        osc_row.addWidget(self._osc_port)
        osc_row.addWidget(QLabel("·"))
        osc_row.addWidget(QLabel("pos/s"))
        self._osc_hz = QSpinBox()
        self._osc_hz.setRange(30, 1200)
        self._osc_hz.setToolTip(
            "Target OSC position writes per second (per object channel). "
            "Uses real audio frame times (no grid snap). "
            "600–960 ≈ very smooth; higher = denser ADM points."
        )
        _ohz = self._settings.value("recorder/osc_record_hz", OSC_POSITION_RECORD_HZ)
        try:
            self._osc_hz.setValue(int(_ohz))
        except (TypeError, ValueError):
            self._osc_hz.setValue(OSC_POSITION_RECORD_HZ)
        self._osc_hz.valueChanged.connect(
            lambda v: self._settings.setValue("recorder/osc_record_hz", int(v))
        )
        osc_row.addWidget(self._osc_hz)
        osc_row.addStretch(1)
        ig.addLayout(osc_row, 4, 1)
        ig.addWidget(QLabel("UDP bind"), 5, 0)
        self._osc_bind_combo = QComboBox()
        self._osc_bind_combo.addItem("127.0.0.1 — localhost", "127.0.0.1")
        self._osc_bind_combo.addItem("0.0.0.0 — all interfaces", "0.0.0.0")
        _bh = self._settings.value("recorder/osc_bind_host", "127.0.0.1")
        _bhs = str(_bh) if _bh is not None else "127.0.0.1"
        if _bhs not in ("127.0.0.1", "0.0.0.0"):
            _bhs = "127.0.0.1"
        _bidx = self._osc_bind_combo.findData(_bhs)
        if _bidx >= 0:
            self._osc_bind_combo.setCurrentIndex(_bidx)
        ig.addWidget(self._osc_bind_combo, 5, 1)
        ig.addWidget(QLabel("OSC ctrl"), 6, 0)
        self._ctrl_port = QSpinBox()
        self._ctrl_port.setRange(1, 65535)
        v_ctrl = self._settings.value("recorder/osc_control_port", 9990)
        try:
            self._ctrl_port.setValue(int(v_ctrl))
        except (TypeError, ValueError):
            self._ctrl_port.setValue(9990)
        ig.addWidget(self._ctrl_port, 6, 1)
        ig.addWidget(QLabel("OSC source"), 7, 0)
        self._osc_source_combo = QComboBox()
        self._osc_source_combo.addItem("Player — listen here", "player")
        self._osc_source_combo.addItem("Engine echo — subscribe", "engine")
        self._osc_source_combo.setToolTip(
            "Player: bind 'OSC in' and record whatever a player/tool streams to it.\n"
            "Engine echo: subscribe to spatial_engine's echo plane so every source "
            "the engine sees (player, VST3, WebGUI, scene loads) is captured. "
            "'OSC in' then becomes the local port the engine echoes back to "
            "(advertised as the handshake reply_port); the preset is fixed to ADM."
        )
        _src = self._settings.value("recorder/osc_source", "player")
        _src = _src if _src in ("player", "engine") else "player"
        _si = self._osc_source_combo.findData(_src)
        if _si >= 0:
            self._osc_source_combo.setCurrentIndex(_si)
        ig.addWidget(self._osc_source_combo, 7, 1)
        ig.addWidget(QLabel("Engine"), 8, 0)
        eng_row = QHBoxLayout()
        _eh = self._settings.value("recorder/engine_host", "127.0.0.1")
        self._engine_host = QLineEdit(str(_eh) if _eh else "127.0.0.1")
        self._engine_host.setToolTip(
            "spatial_engine host. The handshake/heartbeat go to its inbound OSC "
            "socket; it replies the echo stream to us."
        )
        self._engine_port = QSpinBox()
        self._engine_port.setRange(1, 65535)
        try:
            self._engine_port.setValue(int(self._settings.value("recorder/engine_port", 9100)))
        except (TypeError, ValueError):
            self._engine_port.setValue(9100)
        self._engine_port.setToolTip("Engine inbound OSC port (default 9100).")
        eng_row.addWidget(self._engine_host, 1)
        eng_row.addWidget(QLabel(":"))
        eng_row.addWidget(self._engine_port)
        ig.addLayout(eng_row, 8, 1)
        ig.addWidget(QLabel("Preset"), 9, 0)
        self._preset_combo = QComboBox()
        for pid, title in RECORD_PRESET_ENTRIES:
            self._preset_combo.addItem(title, pid)
        ig.addWidget(self._preset_combo, 9, 1)
        ig.addWidget(QLabel("Custom"), 10, 0)
        self._custom_tpl = QLineEdit(DEFAULT_CUSTOM_TEMPLATES["cart"])
        ig.addWidget(self._custom_tpl, 10, 1)
        ll.addWidget(io)
        ll.addStretch(1)

        # Right: Bed · roles · output
        bed = QGroupBox("Bed")
        bed.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        bl = QVBoxLayout(bed)
        bed_row = QHBoxLayout()
        self._bed_summary = QLabel()
        self._bed_summary.setWordWrap(True)
        self._bed_summary.setStyleSheet("color: #b0b0b0;")
        bed_row.addWidget(self._bed_summary, 1)
        self._btn_bed_layout = QPushButton("Layout…")
        self._btn_bed_layout.clicked.connect(self._open_bed_layout)
        bed_row.addWidget(self._btn_bed_layout)
        bl.addLayout(bed_row)
        rl.addWidget(bed)

        map_box = QGroupBox("ADM roles")
        map_l = QVBoxLayout(map_box)
        self._map_summary = QLabel()
        self._map_summary.setWordWrap(True)
        self._map_summary.setStyleSheet("color: #b0b0b0;")
        map_btn_row = QHBoxLayout()
        self._btn_channel_map = QPushButton("Map…")
        self._btn_channel_map.clicked.connect(self._open_channel_map)
        map_btn_row.addWidget(self._btn_channel_map)
        map_btn_row.addStretch(1)
        map_l.addWidget(self._map_summary)
        map_l.addLayout(map_btn_row)
        rl.addWidget(map_box)

        out = QGroupBox("Output")
        og = QGridLayout(out)
        og.addWidget(QLabel("WAV"), 0, 0)
        self._out_path = QLineEdit(str(Path.home() / "Desktop" / "adm_recording.wav"))
        self._btn_browse = QPushButton("Browse…")
        self._btn_browse.clicked.connect(self._browse_out)
        oh = QHBoxLayout()
        oh.addWidget(self._out_path, 1)
        oh.addWidget(self._btn_browse)
        og.addLayout(oh, 0, 1)
        btns = QHBoxLayout()
        self._btn_rec = QPushButton("Record")
        self._btn_rec.clicked.connect(self._toggle_record)
        self._btn_rec.setObjectName("recBtn")
        self._btn_stop = QPushButton("Stop & save")
        self._btn_stop.setObjectName("stopBtn")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_and_finalize)
        btns.addWidget(self._btn_rec)
        btns.addWidget(self._btn_stop)
        og.addLayout(btns, 1, 0, 1, 2)
        rl.addWidget(out)
        rl.addStretch(1)

        logg = QGroupBox("Time · OSC log")
        lg = QVBoxLayout(logg)
        lg.setContentsMargins(8, 8, 8, 6)
        self._rec_time_lbl = QLabel("00:00:00.00")
        self._rec_time_lbl.setObjectName("recTimeLabel")
        self._rec_time_lbl.setMinimumWidth(200)
        lg.addWidget(self._rec_time_lbl)
        self._osc_log = QPlainTextEdit()
        self._osc_log.setReadOnly(True)
        self._osc_log.setMaximumBlockCount(800)
        self._osc_log.setMinimumHeight(64)
        self._osc_log.setMaximumHeight(100)
        self._osc_log.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        lg.addWidget(self._osc_log)
        root.addWidget(logg, 0)

        footer = QWidget()
        footer_outer = QHBoxLayout(footer)
        footer_outer.setContentsMargins(8, 6, 8, 8)
        footer_outer.addStretch(1)
        footer_col = QVBoxLayout()
        footer_col.setSpacing(8)
        footer_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        _copy = QLabel("© 2026 DREAM SCAPE Immersive Contents Lab. All rights reserved.")
        _copy.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        _copy.setStyleSheet("color: #8f8f8f; font-size: 9pt;")
        footer_col.addWidget(_copy, 0, Qt.AlignmentFlag.AlignHCenter)
        _lp = resolve_app_logo_path()
        if _lp is not None and _lp.is_file():
            _lpix = QPixmap(str(_lp))
            if not _lpix.isNull():
                _logo_lbl = ClickableLogoLabel("https://dream-scape.kr")
                _logo_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                _app_inst = QApplication.instance()
                _scr = _app_inst.primaryScreen() if _app_inst else None
                _dpr = max(1.0, float(_scr.devicePixelRatio()) if _scr else 1.0)
                _logical_h = 56
                _phys_h = max(1, int(round(_logical_h * _dpr)))
                _phys_w = max(
                    1,
                    int(round(_lpix.width() * _phys_h / max(1, _lpix.height()))),
                )
                _pm = _lpix.scaled(
                    _phys_w,
                    _phys_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                _pm.setDevicePixelRatio(_dpr)
                _logo_lbl.setPixmap(_pm)
                footer_col.addWidget(_logo_lbl, 0, Qt.AlignmentFlag.AlignHCenter)
        footer_outer.addLayout(footer_col)
        footer_outer.addStretch(1)
        root.addWidget(footer)

        self._status = QStatusBar()
        self.setStatusBar(self._status)

        bar = self.menuBar()
        f = bar.addMenu("File")
        q = QAction("Quit", self)
        q.triggered.connect(self.close)
        f.addAction(q)
        h = bar.addMenu("Help")
        ab = QAction("About ADM Recorder", self)
        ab.triggered.connect(self._about)
        h.addAction(ab)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._tick_ui)
        self._ui_timer.start()

        self._ctrl_bridge.record_requested.connect(
            self._osc_control_record, Qt.ConnectionType.QueuedConnection
        )
        self._ctrl_bridge.stop_requested.connect(
            self._osc_control_stop, Qt.ConnectionType.QueuedConnection
        )

        self._fill_input_devices()
        self._restore_audio_device_selection()
        self._device_combo.currentIndexChanged.connect(self._on_audio_device_changed)

        bid = self._settings.value("recorder/bed_layout_id", "7_1_4")
        if isinstance(bid, str) and bid in BED_LAYOUTS_BY_ID:
            self._cmap.bed_layout_id = bid
        self._route = load_route_from_settings(self._settings)
        self._ensure_route_shape()

        self._on_ch_count_changed()
        self._osc_bind_combo.currentIndexChanged.connect(self._on_osc_bind_changed)
        self._ctrl_port.valueChanged.connect(self._on_ctrl_port_changed)
        self._osc_source_combo.currentIndexChanged.connect(self._on_osc_source_changed)
        self._engine_host.editingFinished.connect(
            lambda: self._settings.setValue(
                "recorder/engine_host", self._engine_host.text().strip()
            )
        )
        self._engine_port.valueChanged.connect(
            lambda v: self._settings.setValue("recorder/engine_port", int(v))
        )
        self._apply_osc_source_ui()
        self._restart_control_server()

        self.setStyleSheet(APP_STYLESHEET)

    def _sync_map_summary(self) -> None:
        n = self._cmap.n_channels
        n_bed = sum(1 for r in self._cmap.roles if r == ChannelRole.BED)
        n_obj = n - n_bed
        lay = self._cmap.bed_layout()
        if len(lay.speakers) == 0:
            self._bed_summary.setText(f"{lay.label} — all tracks are objects")
        else:
            self._bed_summary.setText(f"{lay.label} · first {len(lay.speakers)} → bed")
        ok, hint = self._cmap.validate_bed_count()
        status = "OK" if ok else hint
        self._map_summary.setText(
            f"{n} tr · bed {n_bed} · obj {n_obj} · {lay.label}\n{status}"
        )

    def _open_channel_map(self) -> None:
        dlg = ChannelMapDialog(self._cmap, self._levels_bridge, self)
        dlg.exec()
        self._sync_map_summary()

    def _tick_ui(self) -> None:
        if self._recording and self._capture is not None:
            fr = self._capture.current_frame()
            sr = max(1, self._sr)
            t = fr / float(sr)
            self._rec_time_lbl.setText(self._fmt_rec_time(t))
        else:
            self._rec_time_lbl.setText("00:00:00.00")

    def _fmt_rec_time(self, seconds: float) -> str:
        if seconds < 0:
            seconds = 0.0
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - h * 3600 - m * 60
        if h > 0:
            return f"{h:d}:{m:02d}:{s:06.3f}"
        return f"{m:02d}:{s:06.3f}"

    def _apply_bed_layout_to_channels(self) -> None:
        layout = self._cmap.bed_layout()
        n_bed = len(layout.speakers)
        n = self._cmap.n_channels
        for r in range(n):
            self._cmap.roles[r] = ChannelRole.BED if r < n_bed else ChannelRole.OBJECT

    def _on_ch_count_changed(self) -> None:
        self._cmap.set_n_channels(int(self._ch_spin.value()))
        self._apply_bed_layout_to_channels()
        self._ensure_route_shape()
        self._sync_map_summary()

    def _device_input_channels(self) -> int:
        did = self._device_combo.currentData()
        try:
            if did is None:
                di = sd.default.device[0]
                info = sd.query_devices(int(di))
            else:
                info = sd.query_devices(int(did))
            n = int(info.get("max_input_channels") or 1)
        except (TypeError, ValueError, OSError):
            n = 32
        return max(1, min(128, n))

    def _ensure_route_shape(self) -> None:
        n_in = self._device_input_channels()
        n_out = int(self._ch_spin.value())
        self._route = resize_route(self._route, n_in, n_out)

    def _on_audio_device_changed(self, _index: int) -> None:
        self._save_audio_device_selection()
        self._ensure_route_shape()

    def _open_input_matrix(self) -> None:
        self._ensure_route_shape()
        assert self._route is not None
        n_in = self._device_input_channels()
        n_out = int(self._ch_spin.value())
        dlg = InputMatrixDialog(n_in, n_out, self._route, self)
        if dlg.exec():
            self._route = dlg.route_matrix()
            save_route_to_settings(self._settings, self._route)

    def _open_bed_layout(self) -> None:
        dlg = BedLayoutDialog(self._cmap, self)
        if dlg.exec():
            self._settings.setValue("recorder/bed_layout_id", self._cmap.bed_layout_id)
            self._apply_bed_layout_to_channels()
            self._sync_map_summary()

    def _shutdown_control_server(self) -> None:
        if self._ctrl_srv is not None:
            try:
                self._ctrl_srv.shutdown()
                self._ctrl_srv.server_close()
            except Exception:
                pass
        self._ctrl_srv = None
        self._ctrl_thread = None
        self._ctrl_listen_port = None
        self._ctrl_listen_host = None

    def _osc_bind_host(self) -> str:
        d = self._osc_bind_combo.currentData()
        if isinstance(d, str) and d in ("127.0.0.1", "0.0.0.0"):
            return d
        return "127.0.0.1"

    def _restart_control_server(self) -> None:
        port = int(self._ctrl_port.value())
        host = self._osc_bind_host()
        if (
            self._ctrl_srv is not None
            and self._ctrl_listen_port == port
            and self._ctrl_listen_host == host
        ):
            return
        self._shutdown_control_server()
        self._settings.setValue("recorder/osc_control_port", port)
        try:
            self._ctrl_srv, self._ctrl_thread = start_osc_control_server(
                host, port, self._ctrl_bridge
            )
            self._ctrl_listen_port = port
            self._ctrl_listen_host = host
        except OSError as e:
            self._ctrl_listen_port = None
            self._ctrl_listen_host = None
            self._log_ui(
                "ERROR",
                f"OSC ctrl {host}:{port} bind failed: {e}. "
                "Use a free port; ensure it differs from OSC in.",
            )
            self._status.showMessage("OSC ctrl bind failed — see log", 10000)

    def _on_osc_bind_changed(self, _index: int) -> None:
        self._settings.setValue("recorder/osc_bind_host", self._osc_bind_host())
        self._restart_control_server()

    def _on_ctrl_port_changed(self, _v: int) -> None:
        self._restart_control_server()

    def _osc_source(self) -> str:
        d = self._osc_source_combo.currentData()
        return d if d in ("player", "engine") else "player"

    def _on_osc_source_changed(self, _index: int) -> None:
        self._settings.setValue("recorder/osc_source", self._osc_source())
        self._apply_osc_source_ui()

    def _apply_osc_source_ui(self) -> None:
        """Engine-echo fields are live only in engine mode; the preset is then
        fixed to ADM (the only thing the engine echo plane emits)."""
        engine = self._osc_source() == "engine"
        self._engine_host.setEnabled(engine)
        self._engine_port.setEnabled(engine)
        self._preset_combo.setEnabled(not engine)
        self._custom_tpl.setEnabled(not engine)

    def _abort_capture_with_error(self, log_msg: str, status_msg: str) -> None:
        """Tear down a just-started capture + temp file after a startup failure."""
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        self._log_ui("ERROR", log_msg)
        self._status.showMessage(status_msg, 8000)
        if self._temp_wav is not None:
            try:
                self._temp_wav.unlink(missing_ok=True)
            except OSError:
                pass
            self._temp_wav = None

    def _osc_control_record(self) -> None:
        if self._recording or self._osc_ctrl_record_pending:
            return
        self._osc_ctrl_record_pending = True
        QTimer.singleShot(0, self._run_osc_control_record)

    def _run_osc_control_record(self) -> None:
        self._osc_ctrl_record_pending = False
        if not self._recording:
            self._toggle_record()

    def _osc_control_stop(self) -> None:
        if not self._recording or self._osc_ctrl_stop_pending:
            return
        self._osc_ctrl_stop_pending = True
        QTimer.singleShot(0, self._run_osc_control_stop)

    def _run_osc_control_stop(self) -> None:
        self._osc_ctrl_stop_pending = False
        if self._recording:
            self._stop_and_finalize()

    def _fill_input_devices(self) -> None:
        cur = self._device_combo.currentData() if self._device_combo.count() else None
        self._device_combo.clear()
        self._device_combo.addItem("System default input", None)
        try:
            default_in = sd.default.device[0]
        except (TypeError, IndexError, KeyError):
            default_in = None
        for i, d in enumerate(sd.query_devices()):
            n_in = int(d.get("max_input_channels") or 0)
            if n_in < 1:
                continue
            name = d.get("name", "?")
            label = f"[{i}] {name} (in max {n_in}ch)"
            if default_in is not None and i == default_in:
                label += "  · default"
            self._device_combo.addItem(label, i)
        if cur is not None:
            for j in range(self._device_combo.count()):
                if self._device_combo.itemData(j) == cur:
                    self._device_combo.setCurrentIndex(j)
                    break

    def _save_audio_device_selection(self) -> None:
        self._settings.setValue("recorder/audio_device", self._device_combo.currentData())

    def _restore_audio_device_selection(self) -> None:
        did = self._settings.value("recorder/audio_device")
        for j in range(self._device_combo.count()):
            if self._device_combo.itemData(j) == did:
                self._device_combo.setCurrentIndex(j)
                return

    def _browse_out(self) -> None:
        p, _f = QFileDialog.getSaveFileName(
            self, "Save WAV", self._out_path.text(), "WAV (*.wav);;All (*.*)"
        )
        if p:
            self._out_path.setText(p)

    def _append_osc_log(self, line: str) -> None:
        self._osc_log.appendPlainText(line)

    def _log_ui(self, level: str, message: str) -> None:
        """Append to the bottom log; use instead of modal boxes for runtime errors."""
        self._osc_log.appendPlainText(f"[{level}] {message}")

    def _custom_pat(self) -> dict[str, str]:
        return {"cart": self._custom_tpl.text().strip() or DEFAULT_CUSTOM_TEMPLATES["cart"]}

    def _current_input_device(self) -> int | str | None:
        return coerce_audio_device(self._device_combo.currentData())

    def _on_osc_raw(self, addr: str, args: list) -> None:
        self._osc_log_bridge.line.emit(f"{addr}  {args!r}")

    def _toggle_record(self) -> None:
        if self._recording:
            return
        in_p = int(self._osc_port.value())
        ctrl_p = int(self._ctrl_port.value())
        if in_p == ctrl_p:
            self._log_ui(
                "ERROR",
                "OSC in and OSC ctrl must use different ports (e.g. 9010 vs 9990).",
            )
            self._status.showMessage("OSC port conflict — see log", 8000)
            return
        ok, msg = self._cmap.validate_bed_count()
        if not ok:
            self._log_ui("WARN", f"Channel layout: {msg}")
            self._status.showMessage("Channel layout invalid — check Bed / Map", 8000)
            return
        self._timeline.clear()
        self._osc_log.clear()
        self._temp_wav = Path(tempfile.mkstemp(suffix="_adm_rec_temp.wav")[1])
        self._sr = 48000
        dev = self._current_input_device()

        def on_levels(peaks: list[float]) -> None:
            self._levels_bridge.peaks.emit(peaks)

        self._ensure_route_shape()
        assert self._route is not None
        n_in = self._device_input_channels()
        n_out = int(self._cmap.n_channels)
        try:
            self._capture = AudioCapture(
                dev,
                self._sr,
                n_in,
                n_out,
                self._temp_wav,
                self._route,
                on_levels=on_levels,
            )
            self._capture.start()
        except Exception as e:
            self._log_ui("ERROR", f"Audio: could not open input: {e}")
            self._status.showMessage("Audio input failed — see log", 8000)
            if self._temp_wav is not None:
                try:
                    self._temp_wav.unlink(missing_ok=True)
                except OSError:
                    pass
                self._temp_wav = None
            self._capture = None
            return

        on_xyz = make_position_callback(
            self._timeline,
            sample_rate=self._sr,
            hz=int(self._osc_hz.value()),
        )

        def get_frame() -> int:
            return self._capture.current_frame() if self._capture else 0

        bind_h = self._osc_bind_host()
        in_port = int(self._osc_port.value())

        if self._osc_source() == "engine":
            # The engine echo plane re-emits every source it sees (player, VST3,
            # WebGUI, scene loads) as ADM-OSC — so the preset is fixed to ADM and
            # 'OSC in' is the local port the engine echoes back to.
            engine_host = self._engine_host.text().strip() or "127.0.0.1"
            engine_port = int(self._engine_port.value())
            self._settings.setValue("recorder/engine_host", engine_host)
            try:
                self._echo_session = start_engine_echo_ingest(
                    bind_host=bind_h,
                    listen_port=in_port,
                    engine_host=engine_host,
                    engine_port=engine_port,
                    get_frame=get_frame,
                    on_xyz=on_xyz,
                    on_meta=self._timeline.add_meta,
                    on_raw=self._on_osc_raw,
                    preset="adm",
                )
            except OSError as e:
                self._abort_capture_with_error(
                    f"Engine echo: bind on {bind_h}:{in_port} failed: {e}. "
                    "Free the port or pick another, or switch the UDP bind mode.",
                    "Echo bind failed — see log",
                )
                return
            self._osc_srv = self._echo_session.server
            self._osc_thread = self._echo_session.thread
            listen_log = (
                f"Engine echo: listening on {bind_h}:{in_port}, subscribed to "
                f"engine {engine_host}:{engine_port} (ADM preset; capturing all "
                "engine sources)."
            )
        else:
            pid = self._preset_combo.currentData()
            if not isinstance(pid, str):
                pid = "adm"
            router = OscIngestRouter(
                pid,
                get_frame,
                on_xyz,
                self._custom_pat() if pid == "custom" else None,
                on_raw=self._on_osc_raw,
                on_meta=self._timeline.add_meta,
            )
            try:
                self._osc_srv, self._osc_thread = start_osc_server(bind_h, in_port, router)
            except OSError as e:
                self._abort_capture_with_error(
                    f"OSC position {bind_h}:{in_port} bind failed: {e}. "
                    "Another process may hold the port, or try the other UDP bind mode / another port.",
                    "OSC bind failed — see log",
                )
                return
            listen_log = f"OSC position listening on {bind_h}:{in_port} (send from 127.0.0.1 ok)."

        self._recording = True
        self._btn_rec.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._log_ui("INFO", listen_log)
        self._status.showMessage("Recording…", 0)

    def _stop_and_finalize(self) -> None:
        if not self._recording:
            return
        self._recording = False
        cap = self._capture
        self._capture = None
        if cap is not None:
            cap.stop()
        if self._echo_session is not None:
            # Stops the heartbeat thread *and* the ingest server it owns.
            try:
                self._echo_session.close()
            except Exception:
                pass
            self._echo_session = None
            self._osc_srv = None  # already torn down by the session
        if self._osc_srv is not None:
            try:
                self._osc_srv.shutdown()
                self._osc_srv.server_close()
            except Exception:
                pass
            self._osc_srv = None
        self._osc_thread = None

        self._btn_rec.setEnabled(True)
        self._btn_stop.setEnabled(False)

        tmp = self._temp_wav
        self._temp_wav = None
        if tmp is None or not tmp.is_file():
            self._log_ui("WARN", "No temp recording file to finalize.")
            self._status.showMessage("No temp file", 5000)
            return

        try:
            import soundfile as sf

            info = sf.info(str(tmp))
            total_frames = info.frames
            sr = float(info.samplerate)
        except Exception as e:
            self._log_ui("ERROR", f"Temp WAV: could not read: {e}")
            self._status.showMessage("Temp file read failed — see log", 8000)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return

        if total_frames <= 0:
            # Empty capture — typically a Record-Stop with no audio reaching the
            # callback (device fault, immediate stop). Skip writing a 0-byte
            # BWF master and drop the temp WAV.
            self._log_ui("WARN", "No audio frames captured — nothing to save.")
            self._status.showMessage("Empty recording — not saved", 8000)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return

        events = self._timeline.snapshot()
        metas = self._timeline.snapshot_meta()
        blocks_raw, names_raw = events_to_object_blocks(events, metas, total_frames, sr)
        blocks: dict[int, list[ObjectBlock]] = {}
        object_names: dict[int, str] = {}
        for r, role in enumerate(self._cmap.roles):
            ch1 = r + 1
            if role != ChannelRole.OBJECT:
                continue
            blocks[ch1] = blocks_raw.get(ch1, [])
            if ch1 in names_raw:
                object_names[ch1] = names_raw[ch1]

        raw_out = Path(self._out_path.text().strip())
        out = _unique_output_path(raw_out)
        if out.resolve() != raw_out.expanduser().resolve():
            self._log_ui("INFO", f"Output file exists — saving as {out.name}")
        try:
            finalize_bwf_session(
                temp_wav=tmp,
                out_bwf=out,
                cmap=self._cmap,
                blocks_per_object=blocks,
                total_frames=total_frames,
                sample_rate=sr,
                object_names=object_names,
            )
        except Exception as e:
            self._log_ui("ERROR", f"Save: failed to write WAV/metadata: {e}")
            self._status.showMessage("Save failed — see log", 8000)
            return
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        self._log_ui("INFO", f"Saved to {out}")
        self._status.showMessage(f"Saved: {out}", 8000)

    def _about(self) -> None:
        QMessageBox.information(
            self,
            "ADM Recorder",
            "Records multichannel audio with time-aligned OSC into 48 kHz WAV "
            "with ADM axml and chna.\n"
            "Default bed layout is 7.1.4; change it under Bed → Layout.\n"
            "OSC control port: /record to start, /stop to stop and save.\n"
            "UDP bind 127.0.0.1 matches tools that send to localhost; use two different "
            "ports for position vs control.",
        )

    def closeEvent(self, event) -> None:
        if self._recording:
            self._stop_and_finalize()
        if self._route is not None:
            save_route_to_settings(self._settings, self._route)
        self._shutdown_control_server()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ADM Recorder")
    app.setOrganizationName("DREAM SCAPE")
    _ilp = resolve_app_logo_path()
    if _ilp is not None and _ilp.is_file():
        app.setWindowIcon(QIcon(str(_ilp)))
    app.setStyleSheet(APP_STYLESHEET)
    w = MainWindow()
    w.show()
    return int(app.exec())
