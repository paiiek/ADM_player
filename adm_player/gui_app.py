"""
ADM BWF desktop GUI (PySide6).
Run: adm-player-gui  or  pip install 'adm-player[gui]'
"""

from __future__ import annotations

import json
import random
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


def resolve_app_logo_path() -> Path | None:
    """번들(PyInstaller)·개발 트리 모두에서 LOGO_White.png 검색."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            root = Path(meipass)
            candidates.append(root / "adm_player" / "resources" / "LOGO_White.png")
            candidates.append(root / "LOGO_White.png")
    pkg = Path(__file__).resolve().parent
    candidates.append(pkg / "resources" / "LOGO_White.png")
    candidates.append(pkg.parent / "LOGO_White.png")
    for c in candidates:
        if c.is_file():
            return c
    return None


def main() -> int:
    try:
        from PySide6.QtCore import (
            QObject,
            Qt,
            QSettings,
            QThread,
            QTimer,
            QUrl,
            Signal,
            QSize,
        )
        from PySide6.QtGui import (
            QAction,
            QDesktopServices,
            QDragEnterEvent,
            QDropEvent,
            QFont,
            QMouseEvent,
            QPixmap,
        )
        from PySide6.QtWidgets import (
            QApplication,
            QAbstractItemView,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QDoubleSpinBox,
            QFileDialog,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QInputDialog,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QFrame,
            QGraphicsOpacityEffect,
            QProgressBar,
            QPushButton,
            QSizePolicy,
            QScrollArea,
            QSlider,
            QSpinBox,
            QSplitter,
            QStatusBar,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )

        class StartupSplash(QWidget):
            """시작 시 로고·로딩 문구 표시."""

            def __init__(self, logo_path: Path | None) -> None:
                super().__init__()
                self.setWindowFlags(
                    Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.Tool
                )
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                self.setFixedSize(460, 360)
                outer = QVBoxLayout(self)
                outer.setContentsMargins(0, 0, 0, 0)
                panel = QFrame()
                panel.setObjectName("splashPanel")
                panel.setStyleSheet(
                    "#splashPanel {"
                    "  background-color: #121212;"
                    "  border: 1px solid #3d3d3d;"
                    "  border-radius: 14px;"
                    "}"
                )
                lay = QVBoxLayout(panel)
                lay.setSpacing(14)
                lay.setContentsMargins(28, 28, 28, 22)
                self._logo = QLabel()
                self._logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._logo.setMinimumHeight(200)
                if logo_path and logo_path.is_file():
                    pm = QPixmap(str(logo_path))
                    if not pm.isNull():
                        app_inst = QApplication.instance()
                        scr = app_inst.primaryScreen() if app_inst else None
                        dpr = max(1.0, float(scr.devicePixelRatio()) if scr else 1.0)
                        logical_h = 200
                        phys_h = max(1, int(round(logical_h * dpr)))
                        phys_w = max(
                            1,
                            int(round(pm.width() * phys_h / max(1, pm.height()))),
                        )
                        pm = pm.scaled(
                            phys_w,
                            phys_h,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        pm.setDevicePixelRatio(dpr)
                        self._logo.setPixmap(pm)
                lay.addWidget(self._logo)
                self._status = QLabel("시작하는 중…")
                self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._status.setStyleSheet("color: #c8c8c8; font-size: 11pt;")
                self._status.setWordWrap(True)
                lay.addWidget(self._status)
                self._bar = QProgressBar()
                self._bar.setRange(0, 0)
                self._bar.setTextVisible(False)
                self._bar.setFixedHeight(5)
                self._bar.setStyleSheet(
                    "QProgressBar { border: 0; background: #2a2a2a; border-radius: 2px; }"
                    "QProgressBar::chunk { background: #5c8fd6; border-radius: 2px; }"
                )
                lay.addWidget(self._bar)
                outer.addWidget(panel)

            def set_status(self, msg: str) -> None:
                self._status.setText(msg)

            def place_center(self) -> None:
                scr = QApplication.primaryScreen()
                if scr is None:
                    return
                g = self.frameGeometry()
                g.moveCenter(scr.availableGeometry().center())
                self.move(g.topLeft())

    except ImportError:
        print(
            "PySide6 is required for the GUI:\n"
            "  pip install 'adm-player[gui]'\n"
            "  or  pip install PySide6",
            file=sys.stderr,
        )
        return 1

    import os

    try:
        import psutil
    except ImportError:
        psutil = None

    import sounddevice as sd
    import soundfile as sf
    from pythonosc.dispatcher import Dispatcher
    from pythonosc import osc_server

    from .adm_model import AdmObject, active_block, parse_adm_objects, parse_track_uid_metadata
    from .bwf import read_axml, read_chna_mapping
    from .interactive import channel_layout_rows
    from .osc_presets import (
        DEFAULT_CUSTOM_TEMPLATES,
        PRESET_ENTRIES,
        create_osc_emitter,
        preset_display_title,
    )
    from .playback import ChannelMixState, OscPlaybackRef, coerce_audio_device, play_adm_wav

    def extract_osc_channel_from_address(address: str) -> int | None:
        """OSC 주소에서 객체 인덱스(보통 WAV 채널 1-based) 추출. 알 수 없으면 None."""
        if (m := re.search(r"/OBA/Object/PhysicalPosition/(\d+)$", address)):
            return int(m.group(1))
        if (m := re.search(r"/ext/src/(\d+)/pwdes$", address)):
            return int(m.group(1))
        if (m := re.search(r"/fm/obj/pos/xyz/(\d+)$", address)):
            return int(m.group(1))
        if (m := re.search(r"/(\d+)/(aed|xyz)$", address)):
            return int(m.group(1))
        if (m := re.search(r"/source/(\d+)/xyz$", address)):
            return int(m.group(1))
        if (m := re.search(r"/source_position/(\d+)$", address)):
            return int(m.group(1))
        if (m := re.search(r"/(\d+)/cartesian$", address)):
            return int(m.group(1))
        if (m := re.search(r"/(\d+)/config/cartesian$", address)):
            return int(m.group(1))
        if (m := re.search(r"/config/obj/(\d+)/cartesian$", address)):
            return int(m.group(1))
        return None

    class SeekTimeLabel(QLabel):
        """Time display; double-click to seek (DAW-style)."""

        seekRequested = Signal()

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(
                "Double-click to set time. Drag the bar or use this to set the start position before Play; "
                "while playing, the bar seeks."
            )
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            f = QFont("Menlo", 26)
            f.setStyleHint(QFont.StyleHint.Monospace)
            self.setFont(f)
            self.setMinimumHeight(52)

        def mouseDoubleClickEvent(self, event) -> None:
            self.seekRequested.emit()
            super().mouseDoubleClickEvent(event)

    class CustomOscTemplateDialog(QDialog):
        """Edit polar / Cartesian / config OSC address templates for Custom preset."""

        def __init__(self, parent: QWidget, tpl: dict[str, str]) -> None:
            super().__init__(parent)
            self.setWindowTitle("Custom OSC addresses")
            self.setModal(True)
            lay = QVBoxLayout(self)
            lay.addWidget(
                QLabel(
                    "Enter OSC address patterns. Use {i} as the placeholder for the object index "
                    "(1-based WAV channel)."
                )
            )
            form = QGridLayout()
            form.addWidget(QLabel("Polar (azimuth, elevation, distance)"), 0, 0)
            self._polar = QLineEdit(tpl.get("polar", DEFAULT_CUSTOM_TEMPLATES["polar"]))
            form.addWidget(self._polar, 0, 1)
            form.addWidget(QLabel("Cartesian (X, Y, Z)"), 1, 0)
            self._cart = QLineEdit(tpl.get("cart", DEFAULT_CUSTOM_TEMPLATES["cart"]))
            form.addWidget(self._cart, 1, 1)
            form.addWidget(QLabel("Cartesian mode (optional)"), 2, 0)
            self._cfg = QLineEdit(tpl.get("cfg", DEFAULT_CUSTOM_TEMPLATES["cfg"]))
            self._cfg.setPlaceholderText("Leave empty to skip mode messages")
            form.addWidget(self._cfg, 2, 1)
            lay.addLayout(form)
            bb = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            bb.accepted.connect(self._try_accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def _try_accept(self) -> None:
            polar = self._polar.text().strip()
            cart = self._cart.text().strip()
            if "{i}" not in polar or "{i}" not in cart:
                QMessageBox.warning(
                    self,
                    "Custom OSC addresses",
                    "Polar and Cartesian addresses must contain the placeholder {i}.",
                )
                return
            self.accept()

        def result_templates(self) -> dict[str, str]:
            cfg = self._cfg.text().strip()
            return {
                "polar": self._polar.text().strip(),
                "cart": self._cart.text().strip(),
                "cfg": cfg,
            }

    class ExtOscSetDialog(QDialog):
        """OSC Setting — preset and scale adjustment."""

        def __init__(
            self,
            parent: QWidget,
            preset_id: str,
            scales: dict[str, float],
            custom_templates: dict[str, str],
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle("OSC Setting")
            self.setModal(True)
            self._custom_tpl = dict(custom_templates)
            lay = QVBoxLayout(self)
            form = QGridLayout()
            self._combo = QComboBox()
            for pid, ptitle in PRESET_ENTRIES:
                self._combo.addItem(ptitle, pid)
            ix = self._combo.findData(preset_id)
            if ix >= 0:
                self._combo.setCurrentIndex(ix)
            self._combo.activated.connect(self._on_preset_activated)
            form.addWidget(QLabel("Preset"), 0, 0)
            form.addWidget(self._combo, 0, 1)
            self._spins: dict[str, QDoubleSpinBox] = {}
            spin_rows = [
                ("sa", "Azimuth scale"),
                ("se", "Elevation scale"),
                ("sd", "Distance scale"),
                ("sx", "X scale"),
                ("sy", "Y scale"),
                ("sz", "Z scale"),
            ]
            for r, (key, label) in enumerate(spin_rows, start=1):
                sp = QDoubleSpinBox()
                sp.setRange(0.001, 1000.0)
                sp.setDecimals(3)
                sp.setValue(float(scales.get(key, 1.0)))
                sp.setMinimumWidth(120)
                self._spins[key] = sp
                form.addWidget(QLabel(label), r, 0)
                form.addWidget(sp, r, 1)
            lay.addLayout(form)
            bb = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            bb.accepted.connect(self._confirm_ok)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def _confirm_ok(self) -> None:
            if str(self._combo.currentData()) == "custom":
                polar = self._custom_tpl.get("polar", "")
                cart = self._custom_tpl.get("cart", "")
                if "{i}" not in polar or "{i}" not in cart:
                    QMessageBox.warning(
                        self,
                        "Custom OSC addresses",
                        "Polar and Cartesian templates must include {i}. "
                        "Choose Custom and edit addresses, or pick another preset.",
                    )
                    return
            self.accept()

        def _on_preset_activated(self, index: int) -> None:
            pid = str(self._combo.itemData(index) or "")
            if pid != "custom":
                return
            dlg = CustomOscTemplateDialog(self, self._custom_tpl)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self._custom_tpl = dlg.result_templates()

        def result_values(self) -> tuple[str, dict[str, float], dict[str, str]]:
            pid = str(self._combo.currentData() or "adm")
            sc = {k: float(w.value()) for k, w in self._spins.items()}
            return pid, sc, dict(self._custom_tpl)

    @dataclass
    class PreviewData:
        """Cached ADM BWF metadata shared by playlist, preview, and playback."""

        path: Path
        objects: list[AdmObject]
        sample_rate: float
        total_frames: int
        n_ch: int
        finfo: str
        uid_meta: dict[str, tuple[str, str]]
        rows: list[tuple[int, str, str]]

    def build_preview_data(p: Path) -> PreviewData:
        p = p.resolve()
        axml = read_axml(p)
        chna = read_chna_mapping(p)
        with sf.SoundFile(str(p)) as f:
            sr = float(f.samplerate)
            n_ch = f.channels
            n_frames = int(f.frames)
            finfo = f"{f.format}/{f.subtype}" if f.subtype else str(f.format)
        objects = parse_adm_objects(axml, sr, chna)
        uid_meta = parse_track_uid_metadata(axml)
        rows = channel_layout_rows(n_ch, chna, uid_meta)
        return PreviewData(
            path=p,
            objects=objects,
            sample_rate=sr,
            total_frames=n_frames,
            n_ch=n_ch,
            finfo=finfo,
            uid_meta=uid_meta,
            rows=rows,
        )

    class OscControlSignals(QObject):
        """Thread-safe bridge: OSC UDP handlers emit these; slots run on the GUI thread."""

        play = Signal()
        pause = Signal()
        stop = Signal()
        next_track = Signal()
        prev_track = Signal()
        playmode = Signal(int)
        playlist_play = Signal(int)

    class MetadataLoadWorker(QThread):
        """Parse axml/chna/ADM and build channel table rows in the background."""

        finished_ok = Signal(str, object)
        failed = Signal(str, str)

        def __init__(self, path: Path, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._path = path

        def run(self) -> None:
            key = str(self._path.resolve())
            try:
                data = build_preview_data(self._path)
                self.finished_ok.emit(key, data)
            except Exception as e:
                self.failed.emit(key, str(e))

    class PlaybackWorker(QThread):
        progress = Signal(int, int, int)
        audio_levels = Signal(list)
        failed = Signal(str)
        playback_ended = Signal(bool)

        def __init__(
            self,
            wav_path: Path,
            objects: list[AdmObject],
            osc_ref: OscPlaybackRef,
            block_frames: int,
            out_channels: int | None,
            device: int | str | None,
            start_frame: int = 0,
            channel_mix: ChannelMixState | None = None,
        ) -> None:
            super().__init__()
            self._wav_path = wav_path
            self._objects = objects
            self._osc_ref = osc_ref
            self._block_frames = block_frames
            self._out_channels = out_channels
            self._device = device
            self._start_frame = start_frame
            self._channel_mix = channel_mix
            self._stop = threading.Event()
            self._pause = threading.Event()

        def request_pause(self) -> None:
            self._pause.set()

        def request_resume(self) -> None:
            self._pause.clear()

        def request_stop(self) -> None:
            self._pause.clear()
            self._stop.set()

        def is_paused(self) -> bool:
            return self._pause.is_set()

        def run(self) -> None:
            try:
                play_adm_wav(
                    self._wav_path,
                    self._objects,
                    osc=self._osc_ref,
                    block_frames=self._block_frames,
                    out_channels=self._out_channels,
                    device=self._device,
                    stop_event=self._stop,
                    pause_event=self._pause,
                    start_frame=self._start_frame,
                    on_progress=lambda p, t, sr: self.progress.emit(p, t, sr),
                    on_levels=lambda peaks: self.audio_levels.emit(peaks),
                    channel_mix=self._channel_mix,
                    quiet_truncation=True,
                    progress_emit_interval_s=0.05,
                    levels_emit_interval_s=0.05,
                )
            except Exception as e:
                self.failed.emit(str(e))
                return
            natural_eof = not self._stop.is_set()
            self.playback_ended.emit(natural_eof)

    class ClickableLogoLabel(QLabel):
        """Footer logo: opens company site in the default browser."""

        def __init__(self, url: str, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._url = url
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(url)

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                QDesktopServices.openUrl(QUrl(self._url))
            super().mouseReleaseEvent(event)

    class SystemStatusDialog(QDialog):
        """Live CPU/RAM view for this process (requires psutil)."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            assert psutil is not None
            self.setWindowTitle("System Status")
            self.setMinimumWidth(440)
            self._proc = psutil.Process(os.getpid())
            self._proc.cpu_percent(interval=None)

            lay = QVBoxLayout(self)
            title = QLabel("<b>ADM Player — this process</b>")
            lay.addWidget(title)

            self._pid_lbl = QLabel()
            self._cpu_app_lbl = QLabel()
            self._cpu_sys_lbl = QLabel()
            self._ram_rss_lbl = QLabel()
            self._ram_pct_lbl = QLabel()
            self._threads_lbl = QLabel()
            for w in (
                self._pid_lbl,
                self._cpu_app_lbl,
                self._cpu_sys_lbl,
                self._ram_rss_lbl,
                self._ram_pct_lbl,
                self._threads_lbl,
            ):
                w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                lay.addWidget(w)

            hint = QLabel(
                "<i>Updates every second. "
                "“CPU (this app)” can exceed 100% on multi-core systems. "
                "RSS is physical RAM used by this process.</i>"
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: palette(mid); font-size: 9pt;")
            lay.addWidget(hint)

            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

            self._timer = QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self._refresh)
            self._timer.start()
            self._refresh()

        def closeEvent(self, event) -> None:
            self._timer.stop()
            super().closeEvent(event)

        def _refresh(self) -> None:
            assert psutil is not None
            try:
                pid = self._proc.pid
                cpu_app = self._proc.cpu_percent(interval=None)
                nthr = self._proc.num_threads()
                mem = self._proc.memory_info()
                rss_mb = mem.rss / (1024.0 * 1024.0)
                vm = psutil.virtual_memory()
                rss_pct = (mem.rss / vm.total) * 100.0 if vm.total else 0.0
                cpu_sys = psutil.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                return

            self._pid_lbl.setText(f"PID: {pid}")
            self._cpu_app_lbl.setText(f"CPU (this app): {cpu_app:.1f}%")
            self._cpu_sys_lbl.setText(f"CPU (system overall): {cpu_sys:.1f}%")
            self._ram_rss_lbl.setText(f"RAM (RSS): {rss_mb:.1f} MB")
            self._ram_pct_lbl.setText(f"RAM (share of system): {rss_pct:.2f}%")
            self._threads_lbl.setText(f"Threads: {nthr}")

    class MainWindow(QMainWindow):
        osc_log_line = Signal(str)
        ch_osc_log_line = Signal(int, str)

        def __init__(self, startup_splash: QWidget | None = None) -> None:
            super().__init__()
            self._startup_splash_ref: QWidget | None = startup_splash

            def _splash(msg: str) -> None:
                if startup_splash is not None and hasattr(startup_splash, "set_status"):
                    startup_splash.set_status(msg)

            self.setWindowTitle("ADM Player")
            self.setMinimumSize(QSize(920, 620))
            self.setAcceptDrops(True)
            _splash("초기화하는 중…")

            self._wav_path: Path | None = None
            self._objects: list[AdmObject] = []
            self._sample_rate: float = 48000.0
            self._total_frames: int = 0
            self._worker: PlaybackWorker | None = None
            self._playing_path: Path | None = None
            self._channel_mix = ChannelMixState()
            self._ch_monitor_rows: list[dict[str, object]] = []

            self._preview_cache: dict[str, PreviewData] = {}
            self._preview_load_id: int = 0
            self._prefetch_queue: list[str] = []
            self._prefetch_worker: MetadataLoadWorker | None = None
            self._selection_metadata_worker: MetadataLoadWorker | None = None
            self._active_metadata_workers: list[MetadataLoadWorker] = []

            self._debounce_sel = QTimer(self)
            self._debounce_sel.setSingleShot(True)
            self._debounce_sel.setInterval(45)
            self._debounce_sel.timeout.connect(self._debounced_load_selection_preview)

            self._prefetch_timer = QTimer(self)
            self._prefetch_timer.setSingleShot(True)
            self._prefetch_timer.setInterval(280)
            self._prefetch_timer.timeout.connect(self._do_prefetch_neighbors)

            self._seek_user_dragging: bool = False
            self._repeat_mode: int = 0
            self._live_mode: bool = False
            self._settings = QSettings()
            self._osc_preset_id: str = "adm"
            self._scale_sa = 1.0
            self._scale_se = 1.0
            self._scale_sd = 1.0
            self._scale_sx = 1.0
            self._scale_sy = 1.0
            self._scale_sz = 1.0
            self._osc_custom_templates: dict[str, str] = dict(DEFAULT_CUSTOM_TEMPLATES)

            self._sum_sample = QLabel("—")
            self._sum_ch = QLabel("—")
            self._sum_dur = QLabel("—")
            self._sum_fmt = QLabel("—")
            self._sum_obj = QLabel("—")

            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            _splash("인터페이스 레이아웃을 구성하는 중…")

            self._osc_playback_ref = OscPlaybackRef(None)
            self._osc_ctrl = OscControlSignals()
            self._control_osc_server: osc_server.ThreadingOSCUDPServer | None = None
            self._control_osc_server_thread: threading.Thread | None = None

            self.setStyleSheet(
                "QGroupBox { font-size: 13pt; font-weight: 600; } "
                "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }"
                "#transportStrip {"
                "  background-color: palette(base);"
                "  border: 1px solid palette(mid);"
                "  border-radius: 8px;"
                "}"
                "#transportPlay {"
                "  background-color: #1f7a3d; color: #ffffff; font-weight: 700;"
                "  border: 2px solid rgba(255,255,255,0.35); border-radius: 6px; padding: 6px 10px;"
                "}"
                "#transportPlay:hover { background-color: #259045; border-color: rgba(255,255,255,0.55); }"
                "#transportPlay:pressed {"
                "  background-color: #0a3d1c; color: #ffffff; font-weight: 800;"
                "  border: 3px solid #ffffff; padding-top: 7px; padding-bottom: 5px;"
                "}"
                "#transportPause {"
                "  background-color: #c27f00; color: #ffffff; font-weight: 700;"
                "  border: 2px solid rgba(255,255,255,0.35); border-radius: 6px; padding: 6px 10px;"
                "}"
                "#transportPause:hover { background-color: #d49200; border-color: rgba(255,255,255,0.55); }"
                "#transportPause:pressed {"
                "  background-color: #7a4d00; color: #ffffff; font-weight: 800;"
                "  border: 3px solid #ffffff; padding-top: 7px; padding-bottom: 5px;"
                "}"
                "#transportStop {"
                "  background-color: #b32d2d; color: #ffffff; font-weight: 700;"
                "  border: 2px solid rgba(255,255,255,0.35); border-radius: 6px; padding: 6px 10px;"
                "}"
                "#transportStop:hover { background-color: #c93a3a; border-color: rgba(255,255,255,0.55); }"
                "#transportStop:pressed {"
                "  background-color: #6e1818; color: #ffffff; font-weight: 800;"
                "  border: 3px solid #ffffff; padding-top: 7px; padding-bottom: 5px;"
                "}"
                "#transportPrev, #transportNext {"
                "  background-color: palette(button); color: palette(button-text); font-weight: 700;"
                "  border: 2px solid palette(mid); border-radius: 6px; padding: 6px 10px;"
                "}"
                "#transportPrev:hover, #transportNext:hover {"
                "  background-color: palette(midlight); border-color: palette(dark);"
                "}"
                "#transportPrev:pressed, #transportNext:pressed {"
                "  background-color: palette(dark); color: palette(bright-text); font-weight: 800;"
                "  border: 3px solid #5a9fff; padding-top: 7px; padding-bottom: 5px;"
                "}"
                "#transportPlay:disabled, #transportPause:disabled, #transportStop:disabled,"
                "#transportPrev:disabled, #transportNext:disabled {"
                "  opacity: 0.42;"
                "}"
                "#nowPlayingStrip {"
                "  background-color: #121212;"
                "  border: 1px solid palette(mid);"
                "  border-radius: 8px;"
                "}"
                "#nowPlayingTitle {"
                "  font-size: 20pt;"
                "  font-weight: 700;"
                "  padding: 12px 16px;"
                "  color: #f5f5f5;"
                "}"
                "#transportLive {"
                "  background-color: palette(button); color: palette(button-text);"
                "  font-weight: 700; border: 2px solid palette(mid); border-radius: 6px;"
                "  padding: 6px 10px;"
                "}"
                "#transportLive:checked {"
                "  background-color: #b71c1c; color: #ffffff; border-color: #ff8a80;"
                "}"
                "#chMuteBtn {"
                "  background-color: rgba(211, 47, 47, 0.28); color: #ffcccc;"
                "  border: 1px solid rgba(239, 83, 80, 0.45); border-radius: 4px;"
                "  font-weight: 700; padding: 2px 6px;"
                "}"
                "#chMuteBtn:checked {"
                "  background-color: rgba(211, 47, 47, 0.96); color: #ffffff;"
                "  border: 1px solid #ffcdd2;"
                "}"
                "#chSoloBtn {"
                "  background-color: rgba(255, 193, 7, 0.28); color: #fff8e1;"
                "  border: 1px solid rgba(255, 193, 7, 0.5); border-radius: 4px;"
                "  font-weight: 700; padding: 2px 6px;"
                "}"
                "#chSoloBtn:checked {"
                "  background-color: rgba(255, 193, 7, 0.96); color: #3e2723;"
                "  border: 1px solid #ffe082;"
                "}"
            )
            _splash("스타일 적용 완료 · 재생·트랜스포트 패널 구성 중…")

            transport_frame = QFrame()
            transport_frame.setObjectName("transportStrip")
            transport_outer = QHBoxLayout(transport_frame)
            transport_outer.setContentsMargins(10, 10, 10, 10)
            transport_outer.addStretch(1)
            transport_center = QWidget()
            tc_lay = QVBoxLayout(transport_center)
            tc_lay.setContentsMargins(8, 8, 8, 8)
            tc_lay.setSpacing(10)
            self._time_label = SeekTimeLabel("00:00:00.00 / 00:00:00.00")
            self._time_label.setMinimumWidth(520)
            self._time_label.seekRequested.connect(self._on_time_label_seek)
            tc_lay.addWidget(self._time_label)
            self._seek_slider = QSlider(Qt.Orientation.Horizontal)
            self._seek_slider.setRange(0, 1000)
            self._seek_slider.setValue(0)
            self._seek_slider.setEnabled(False)
            self._seek_slider.setMinimumHeight(28)
            self._seek_slider.setMinimumWidth(520)
            self._seek_slider.sliderPressed.connect(self._on_seek_slider_pressed)
            self._seek_slider.sliderReleased.connect(self._on_seek_slider_released)
            self._seek_slider.valueChanged.connect(self._on_seek_value_changed)
            tc_lay.addWidget(self._seek_slider)
            btn_row = QHBoxLayout()
            btn_row.setSpacing(10)
            self._btn_prev = QPushButton("Previous")
            self._btn_play = QPushButton("Play")
            self._btn_pause = QPushButton("Pause")
            self._btn_stop = QPushButton("Stop")
            self._btn_next = QPushButton("Next")
            self._btn_repeat_mode = QPushButton()
            self._btn_prev.setObjectName("transportPrev")
            self._btn_play.setObjectName("transportPlay")
            self._btn_pause.setObjectName("transportPause")
            self._btn_stop.setObjectName("transportStop")
            self._btn_next.setObjectName("transportNext")
            for b in (self._btn_prev, self._btn_play, self._btn_pause, self._btn_stop, self._btn_next):
                b.setMinimumHeight(40)
                b.setMinimumWidth(92)
            self._btn_repeat_mode.setMinimumHeight(40)
            self._btn_repeat_mode.setMinimumWidth(260)
            self._btn_prev.clicked.connect(self._on_prev_track)
            self._btn_play.clicked.connect(self._on_play)
            self._btn_pause.clicked.connect(self._on_pause)
            self._btn_stop.clicked.connect(self._on_stop)
            self._btn_next.clicked.connect(self._on_next_track)
            self._btn_repeat_mode.clicked.connect(self._cycle_repeat_mode)
            self._btn_live = QPushButton("Live")
            self._btn_live.setObjectName("transportLive")
            self._btn_live.setCheckable(True)
            self._btn_live.setToolTip(
                "Live: 출력 장치·OSC 설정·채널 M/S를 잠급니다. 공연 중 실수로 바꾸지 않을 때 사용합니다."
            )
            self._btn_live.setMinimumHeight(40)
            self._btn_live.setMinimumWidth(92)
            self._btn_live.toggled.connect(self._on_live_toggled)
            self._live_blink_timer = QTimer(self)
            self._live_blink_timer.setInterval(450)
            self._live_blink_timer.timeout.connect(self._on_live_blink_tick)
            self._live_opacity_effect = QGraphicsOpacityEffect(self._btn_live)
            self._live_blink_phase = False
            btn_row.addWidget(self._btn_prev)
            btn_row.addWidget(self._btn_play)
            btn_row.addWidget(self._btn_pause)
            btn_row.addWidget(self._btn_stop)
            btn_row.addWidget(self._btn_next)
            vsep = QFrame()
            vsep.setFrameShape(QFrame.Shape.VLine)
            vsep.setFixedWidth(18)
            btn_row.addWidget(vsep)
            btn_row.addWidget(self._btn_repeat_mode)
            vsep_live = QFrame()
            vsep_live.setFrameShape(QFrame.Shape.VLine)
            vsep_live.setFixedWidth(14)
            btn_row.addWidget(vsep_live)
            btn_row.addWidget(self._btn_live)
            tc_lay.addLayout(btn_row)
            transport_outer.addWidget(transport_center, 0, Qt.AlignmentFlag.AlignHCenter)
            transport_outer.addStretch(1)
            root.addWidget(transport_frame)
            _splash("재생 정보·플레이리스트 영역을 만드는 중…")

            now_playing_frame = QFrame()
            now_playing_frame.setObjectName("nowPlayingStrip")
            npl = QVBoxLayout(now_playing_frame)
            npl.setContentsMargins(0, 0, 0, 0)
            self._now_playing_label = QLabel("")
            self._now_playing_label.setObjectName("nowPlayingTitle")
            self._now_playing_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._now_playing_label.setWordWrap(True)
            npl.addWidget(self._now_playing_label)
            root.addWidget(now_playing_frame)

            outer_split = QSplitter(Qt.Orientation.Vertical)
            outer_split.setChildrenCollapsible(False)

            pl_box = QGroupBox("Playlist")
            pl_root = QVBoxLayout(pl_box)

            pl_outer = QHBoxLayout()
            pl_lay = QHBoxLayout()
            self._playlist = QTableWidget(0, 9)
            self._playlist.setHorizontalHeaderLabels(
                ["#", "Title", "Ch", "Objects", "Length", "Hz", "Format", "Status", "Path"]
            )
            self._playlist.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self._playlist.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self._playlist.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self._playlist.verticalHeader().setVisible(False)
            self._playlist.setAlternatingRowColors(True)
            self._playlist.setShowGrid(True)
            self._playlist.setTextElideMode(Qt.TextElideMode.ElideMiddle)
            self._playlist.setMinimumHeight(140)
            self._playlist.setMinimumWidth(480)
            pl_sz = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._playlist.setSizePolicy(pl_sz)
            hdr = self._playlist.horizontalHeader()
            hdr.setStretchLastSection(True)
            hdr.setSectionResizeMode(0, hdr.ResizeMode.Fixed)
            self._playlist.setColumnWidth(0, 36)
            hdr.setSectionResizeMode(1, hdr.ResizeMode.Fixed)
            self._playlist.setColumnWidth(1, 200)
            for c in range(2, 8):
                hdr.setSectionResizeMode(c, hdr.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(8, hdr.ResizeMode.Stretch)
            self._playlist.itemSelectionChanged.connect(self._on_playlist_selection_changed)
            self._playlist.cellDoubleClicked.connect(self._on_playlist_cell_double_clicked)
            pv = QVBoxLayout()
            self._btn_pl_add = QPushButton("Add files…")
            self._btn_pl_add.clicked.connect(self._playlist_add_files)
            self._btn_pl_remove = QPushButton("Remove")
            self._btn_pl_remove.clicked.connect(self._playlist_remove)
            self._btn_pl_clear = QPushButton("Clear list")
            self._btn_pl_clear.clicked.connect(self._playlist_clear)
            self._btn_up = QPushButton("↑")
            self._btn_up.setMaximumWidth(40)
            self._btn_up.clicked.connect(self._playlist_move_up)
            self._btn_dn = QPushButton("↓")
            self._btn_dn.setMaximumWidth(40)
            self._btn_dn.clicked.connect(self._playlist_move_down)
            pv.addWidget(self._btn_pl_add)
            pv.addWidget(self._btn_pl_remove)
            pv.addWidget(self._btn_pl_clear)
            pv.addWidget(self._btn_up)
            pv.addWidget(self._btn_dn)
            pv.addStretch()
            pl_lay.addWidget(self._playlist, 1)
            pl_lay.addLayout(pv)
            pl_outer.addLayout(pl_lay, 1)
            pl_root.addLayout(pl_outer, 1)
            outer_split.addWidget(pl_box)
            _splash("채널 요약·오디오·OSC 패널을 만드는 중…")

            splitter = QSplitter(Qt.Orientation.Horizontal)
            left_scroll = QScrollArea()
            left_scroll.setWidgetResizable(True)
            left_scroll.setFrameShape(QFrame.Shape.NoFrame)
            left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            left_scroll.setMinimumWidth(280)

            left_inner = QWidget()
            left_lay = QVBoxLayout(left_inner)
            left_lay.setContentsMargins(0, 0, 0, 0)
            left_lay.setSpacing(4)

            sum_frame = QGroupBox("Selection summary")
            sum_frame.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Maximum,
            )
            sgrid = QGridLayout(sum_frame)
            sgrid.setContentsMargins(6, 4, 6, 5)
            sgrid.setHorizontalSpacing(8)
            sgrid.setVerticalSpacing(2)
            sgrid.setColumnStretch(1, 1)
            sgrid.setColumnStretch(3, 1)
            _lr = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

            lb_sr = QLabel("Sample rate", sum_frame)
            lb_sr.setAlignment(_lr)
            lb_ch = QLabel("Channels", sum_frame)
            lb_ch.setAlignment(_lr)
            lb_du = QLabel("Duration", sum_frame)
            lb_du.setAlignment(_lr)
            lb_fm = QLabel("Format", sum_frame)
            lb_fm.setAlignment(_lr)
            lb_ob = QLabel("Dynamic objects", sum_frame)
            lb_ob.setAlignment(_lr)

            sgrid.addWidget(lb_sr, 0, 0)
            sgrid.addWidget(self._sum_sample, 0, 1)
            sgrid.addWidget(lb_ch, 0, 2)
            sgrid.addWidget(self._sum_ch, 0, 3)
            sgrid.addWidget(lb_du, 1, 0)
            sgrid.addWidget(self._sum_dur, 1, 1)
            sgrid.addWidget(lb_fm, 1, 2)
            sgrid.addWidget(self._sum_fmt, 1, 3)
            sgrid.addWidget(lb_ob, 2, 0)
            sgrid.addWidget(self._sum_obj, 2, 1, 1, 3)

            left_lay.addWidget(sum_frame)

            self._ch_table = QTableWidget()
            self._ch_table.setColumnCount(4)
            self._ch_table.setHorizontalHeaderLabels(
                ["Ch", "UID", "Type", "M · Level · OSC"]
            )
            ch_hdr = self._ch_table.horizontalHeader()
            ch_hdr.setStretchLastSection(True)
            ch_hdr.setSectionResizeMode(0, ch_hdr.ResizeMode.Fixed)
            self._ch_table.setColumnWidth(0, 44)
            ch_hdr.setSectionResizeMode(1, ch_hdr.ResizeMode.ResizeToContents)
            self._ch_table.setColumnHidden(1, True)
            ch_hdr.setSectionResizeMode(2, ch_hdr.ResizeMode.Fixed)
            self._ch_table.setColumnWidth(2, 128)
            ch_hdr.setSectionResizeMode(3, ch_hdr.ResizeMode.Stretch)
            self._ch_table.setColumnWidth(3, 520)
            self._ch_table.setAlternatingRowColors(True)
            self._ch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self._ch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self._ch_table.verticalHeader().setVisible(False)
            mono = QFont("Menlo", 10)
            mono.setStyleHint(QFont.StyleHint.Monospace)
            self._ch_table.setFont(mono)
            self._ch_table.setMinimumHeight(160)
            self._ch_table.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            )
            left_lay.addWidget(self._ch_table, 1)
            left_inner.setMinimumWidth(360)
            left_scroll.setWidget(left_inner)
            splitter.addWidget(left_scroll)

            right_scroll = QScrollArea()
            right_scroll.setWidgetResizable(True)
            right_scroll.setFrameShape(QFrame.Shape.NoFrame)
            right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            right_scroll.setMinimumWidth(340)

            right = QWidget()
            right_lay = QVBoxLayout(right)
            right_lay.setContentsMargins(8, 8, 8, 8)
            right.setMinimumWidth(340)

            self._dev_box = QGroupBox("Audio output")
            dev_box = self._dev_box
            dev_grid = QGridLayout(dev_box)
            dev_grid.addWidget(QLabel("Device"), 0, 0)
            dev_pick_row = QHBoxLayout()
            self._device_combo = QComboBox()
            self._device_combo.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            )
            self._device_combo.setMinimumWidth(0)
            dev_pick_row.addWidget(self._device_combo, 1)
            self._btn_audio_refresh = QPushButton("Refresh")
            self._btn_audio_refresh.setFixedWidth(80)
            self._btn_audio_refresh.clicked.connect(self._fill_devices)
            dev_pick_row.addWidget(self._btn_audio_refresh)
            dev_grid.addLayout(dev_pick_row, 0, 1, 1, 2)
            dev_grid.addWidget(QLabel("Output ch (0=auto)"), 1, 0)
            self._out_ch_spin = QSpinBox()
            self._out_ch_spin.setRange(0, 256)
            self._out_ch_spin.setSpecialValueText("auto")
            self._out_ch_spin.setValue(0)
            dev_grid.addWidget(self._out_ch_spin, 1, 1)
            right_lay.addWidget(dev_box, 0)

            osc_box = QGroupBox("OSC")
            osc_root = QVBoxLayout(osc_box)

            osc_form = QGridLayout()
            self._osc_enable = QCheckBox()
            self._osc_enable.setChecked(True)
            osc_form.addWidget(self._osc_enable, 0, 0, 1, 2)
            osc_form.addWidget(QLabel("Ext. Renderer IP"), 1, 0)
            self._osc_host = QLineEdit("127.0.0.1")
            self._osc_host.setMinimumHeight(26)
            osc_form.addWidget(self._osc_host, 1, 1)
            osc_form.addWidget(QLabel("Ext. Renderer Port"), 2, 0)
            self._osc_port = QSpinBox()
            self._osc_port.setRange(1, 65535)
            self._osc_port.setValue(9000)
            self._osc_port.setMinimumHeight(26)
            osc_form.addWidget(self._osc_port, 2, 1)
            osc_form.addWidget(QLabel("Control Port"), 3, 0)
            self._control_port_spin = QSpinBox()
            self._control_port_spin.setRange(0, 65535)
            self._control_port_spin.setSpecialValueText("off")
            self._control_port_spin.setValue(9010)
            self._control_port_spin.setToolTip("UDP port to receive transport / playlist OSC commands (0 = disabled).")
            self._control_port_spin.setMinimumHeight(26)
            osc_form.addWidget(self._control_port_spin, 3, 1)
            self._flip_az = QCheckBox("Invert azimuth sign")
            osc_form.addWidget(self._flip_az, 4, 0, 1, 2)
            osc_form.addWidget(QLabel("Azimuth offset (°)"), 5, 0)
            self._az_off = QDoubleSpinBox()
            self._az_off.setRange(-360.0, 360.0)
            self._az_off.setDecimals(2)
            osc_form.addWidget(self._az_off, 5, 1)
            osc_root.addLayout(osc_form)
            osc_box.setMinimumHeight(200)
            osc_box.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            )

            self._osc_scroll = QScrollArea()
            self._osc_scroll.setWidgetResizable(True)
            self._osc_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._osc_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._osc_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._osc_scroll.setMinimumHeight(220)
            self._osc_scroll.setWidget(osc_box)
            self._osc_scroll.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            )
            right_lay.addWidget(self._osc_scroll, 0)

            log_box = QGroupBox("OSC log (last 500 lines)")
            log_lay = QVBoxLayout(log_box)
            self._osc_log = QPlainTextEdit()
            self._osc_log.setReadOnly(True)
            self._osc_log.setMaximumBlockCount(500)
            self._osc_log.setFont(QFont("Menlo", 9))
            self._osc_log.setPlaceholderText("OSC messages appear here when sending.")
            self._osc_log.setMinimumHeight(96)
            self._osc_log.setSizePolicy(
                QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            )
            log_lay.addWidget(self._osc_log)
            self._btn_clear_osc_log = QPushButton("Clear log")
            self._btn_clear_osc_log.clicked.connect(self._osc_log.clear)
            log_lay.addWidget(self._btn_clear_osc_log)
            self._btn_osc_setting = QPushButton("OSC Setting")
            self._btn_osc_setting.setToolTip("Adjust OSC preset and scales.")
            self._btn_osc_setting.clicked.connect(self._open_ext_osc_dialog)
            log_lay.addWidget(self._btn_osc_setting)
            right_lay.addWidget(log_box, 1)

            right_scroll.setWidget(right)
            splitter.addWidget(right_scroll)
            splitter.setChildrenCollapsible(False)
            splitter.setSizes([480, 420])
            outer_split.addWidget(splitter)
            outer_split.setStretchFactor(0, 0)
            outer_split.setStretchFactor(1, 1)
            outer_split.setSizes([200, 520])
            root.addWidget(outer_split, 1)
            _splash("푸터와 메뉴를 연결하는 중…")

            footer = QWidget()
            footer_outer = QHBoxLayout(footer)
            footer_outer.setContentsMargins(8, 6, 8, 8)
            footer_outer.addStretch(1)
            footer_col = QVBoxLayout()
            footer_col.setSpacing(8)
            footer_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            _copy = QLabel(
                "© 2026 DREAM SCAPE Immersive Contents Lab. All rights reserved."
            )
            _copy.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            _copy.setStyleSheet("color: #8f8f8f; font-size: 9pt;")
            footer_col.addWidget(_copy, 0, Qt.AlignmentFlag.AlignHCenter)
            _logo_path = resolve_app_logo_path()
            if _logo_path is not None and _logo_path.is_file():
                _lpix = QPixmap(str(_logo_path))
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
            self._btn_system_status = QPushButton("System Status")
            self._btn_system_status.setToolTip(
                "Monitor CPU and RAM used by ADM Player (opens a live panel)."
            )
            self._btn_system_status.clicked.connect(self._open_system_status)
            self._status.addPermanentWidget(self._btn_system_status)

            menu = self.menuBar().addMenu("File")
            act_add = QAction("Add to playlist…", self)
            act_add.triggered.connect(self._playlist_add_files)
            menu.addAction(act_add)
            act_save_pl = QAction("Save playlist…", self)
            act_save_pl.triggered.connect(self._save_playlist_dialog)
            menu.addAction(act_save_pl)
            act_load_pl = QAction("Load playlist…", self)
            act_load_pl.triggered.connect(self._load_playlist_dialog)
            menu.addAction(act_load_pl)
            act_quit = QAction("Quit", self)
            act_quit.triggered.connect(self.close)
            menu.addAction(act_quit)

            help_m = self.menuBar().addMenu("Help")
            act_about = QAction("About", self)
            act_about.triggered.connect(self._about)
            help_m.addAction(act_about)

            self.osc_log_line.connect(self._append_osc_log_line, Qt.ConnectionType.QueuedConnection)
            self.ch_osc_log_line.connect(self._append_ch_osc_log_line, Qt.ConnectionType.QueuedConnection)

            geom = self._settings.value("ui/geometry")
            if geom is not None:
                self.restoreGeometry(geom)
            self._set_transport_idle()
            QTimer.singleShot(0, self._startup_deferred_init)

        def _startup_deferred_init(self) -> None:
            """오디오 장치 검색·OSC 등 무거운 작업 — 메인 UI 표시 후 이벤트 루프에서 실행."""
            sp = self._startup_splash_ref

            def _splash_msg(msg: str) -> None:
                if sp is not None and hasattr(sp, "set_status"):
                    sp.set_status(msg)

            try:
                _splash_msg("오디오 출력 장치를 검색하는 중…")
                self._fill_devices()
                self._load_settings()
                self._restore_audio_device_selection()
                self._connect_settings_autosave()
                self._connect_osc_control_signals()
                self._update_repeat_mode_button()
                self._update_nav_buttons()
                self._restart_osc_control_server()
                self._set_transport_idle()
                _splash_msg("준비 완료")
            finally:
                self._startup_splash_ref = None

        def _append_osc_log_line(self, line: str) -> None:
            self._osc_log.appendPlainText(line)

        def _append_ch_osc_log_line(self, ch1: int, line: str) -> None:
            idx = ch1 - 1
            if not (0 <= idx < len(self._ch_monitor_rows)):
                return
            rowd = self._ch_monitor_rows[idx]
            le = rowd.get("osc")
            if isinstance(le, QLineEdit):
                le.setText(line)
                le.setCursorPosition(0)

        def _osc_log_cb(self, address: str, value: object) -> None:
            line = f"{address}  {value!r}"
            self.osc_log_line.emit(line)
            ch = extract_osc_channel_from_address(address)
            if ch is not None:
                self.ch_osc_log_line.emit(ch, line)

        def _on_ch_mute_toggled(self, ch1: int, on: bool) -> None:
            self._channel_mix.set_mute(ch1, on)

        def _on_ch_solo_toggled(self, ch1: int, on: bool) -> None:
            self._channel_mix.set_solo(ch1, on)

        def _on_live_blink_tick(self) -> None:
            if not self._btn_live.isChecked():
                self._live_blink_timer.stop()
                self._btn_live.setGraphicsEffect(None)
                return
            self._live_blink_phase = not self._live_blink_phase
            self._live_opacity_effect.setOpacity(0.48 if self._live_blink_phase else 1.0)
            self._btn_live.setGraphicsEffect(self._live_opacity_effect)

        def _on_live_toggled(self, on: bool) -> None:
            self._live_mode = on
            if on:
                self._channel_mix.clear()
                for rowd in self._ch_monitor_rows:
                    mb = rowd.get("mute")
                    sb = rowd.get("solo")
                    if isinstance(mb, QPushButton):
                        mb.blockSignals(True)
                        mb.setChecked(False)
                        mb.setEnabled(False)
                        mb.blockSignals(False)
                    if isinstance(sb, QPushButton):
                        sb.blockSignals(True)
                        sb.setChecked(False)
                        sb.setEnabled(False)
                        sb.blockSignals(False)
                self._dev_box.setEnabled(False)
                self._osc_scroll.setEnabled(False)
                self._btn_osc_setting.setEnabled(False)
                self._btn_clear_osc_log.setEnabled(False)
                self._live_opacity_effect.setOpacity(1.0)
                self._live_blink_phase = False
                self._btn_live.setGraphicsEffect(self._live_opacity_effect)
                self._live_blink_timer.start()
            else:
                self._live_blink_timer.stop()
                self._btn_live.setGraphicsEffect(None)
                for rowd in self._ch_monitor_rows:
                    mb = rowd.get("mute")
                    sb = rowd.get("solo")
                    if isinstance(mb, QPushButton):
                        mb.setEnabled(True)
                    if isinstance(sb, QPushButton):
                        sb.setEnabled(True)
                self._dev_box.setEnabled(True)
                self._osc_scroll.setEnabled(True)
                self._btn_osc_setting.setEnabled(True)
                self._btn_clear_osc_log.setEnabled(True)

        def _open_system_status(self) -> None:
            if psutil is None:
                QMessageBox.information(
                    self,
                    "System Status",
                    "Monitoring requires the <b>psutil</b> package.\n\n"
                    "Install:\n"
                    "  pip install psutil\n"
                    "or reinstall GUI extras:\n"
                    "  pip install 'adm-player[gui]'",
                )
                return
            dlg = SystemStatusDialog(self)
            dlg.exec()

        def _about(self) -> None:
            QMessageBox.about(
                self,
                "ADM Player",
                "<p><b>ADM Player</b></p>"
                "<p>Play ADM BWF (.wav) files from a playlist. "
                "OSC object indices match WAV channels (1-based) and /adm/obj/N.</p>",
            )

        def _fmt_time(self, sec: float) -> str:
            sec = max(0.0, sec)
            s = int(sec % 60)
            m = int((sec // 60) % 60)
            h = int(sec // 3600)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

        def _fmt_timecode_hhmmss_frame(self, sec: float) -> str:
            """Display timecode HH:MM:SS.Frame (frame is 00–29 at 30 fps)."""

            sec = max(0.0, float(sec))
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            sub = sec - (h * 3600 + m * 60 + s)
            fr = min(29, int(sub * 30.0 + 1e-9))
            return f"{h:02d}:{m:02d}:{s:02d}.{fr:02d}"

        def _parse_seek_time(self, s: str) -> float | None:
            s = s.strip()
            if not s:
                return None
            parts = s.split(":")
            try:
                if len(parts) == 1:
                    return float(parts[0].replace(",", "."))
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1].replace(",", "."))
                if len(parts) == 3:
                    return (
                        int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2].replace(",", "."))
                    )
            except ValueError:
                return None
            return None

        def _update_time_label_for_frame(self, frame: int) -> None:
            if self._sample_rate <= 0:
                return
            total_sec = self._total_frames / self._sample_rate if self._total_frames else 0.0
            elapsed = frame / float(self._sample_rate)
            self._time_label.setText(
                f"{self._fmt_timecode_hhmmss_frame(elapsed)} / "
                f"{self._fmt_timecode_hhmmss_frame(total_sec)}"
            )

        def _effective_playlist_row(self) -> int:
            n = self._playlist.rowCount()
            if n <= 0:
                return -1
            row = self._playlist.currentRow()
            if row < 0:
                return 0 if n == 1 else -1
            return row

        def _playlist_row_ready(self, row: int) -> bool:
            if row < 0:
                return False
            it = self._playlist.item(row, 7)
            return it is not None and it.text() == "Ready"

        def _can_start_playback(self) -> bool:
            row = self._effective_playlist_row()
            if row < 0:
                return False
            return self._playlist_row_ready(row)

        def _refresh_transport_play_if_idle(self) -> None:
            if self._worker is not None and self._worker.isRunning():
                return
            self._btn_play.setEnabled(self._can_start_playback())

        def _update_now_playing_title(self) -> None:
            row = self._effective_playlist_row()
            if row < 0:
                self._now_playing_label.setText("")
                return
            tit = self._playlist.item(row, 1)
            if tit is None or tit.text() == "":
                self._now_playing_label.setText("")
                return
            self._now_playing_label.setText(tit.text())

        def _make_ch_level_bar(self) -> QProgressBar:
            bar = QProgressBar()
            bar.setOrientation(Qt.Orientation.Horizontal)
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            bar.setMinimumWidth(72)
            bar.setStyleSheet(
                "QProgressBar { border: 1px solid #555; border-radius: 3px; "
                "background-color: #1e1e1e; }"
                "QProgressBar::chunk { border-radius: 2px; "
                "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, "
                "stop:0 #14532d, stop:0.55 #3ecf6e, stop:1 #d9f99d); }"
            )
            return bar

        def _make_ch_monitor_row(self, ch1: int) -> tuple[QWidget, dict[str, object]]:
            root = QWidget()
            lay = QHBoxLayout(root)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.setSpacing(6)
            m = QPushButton("M")
            m.setObjectName("chMuteBtn")
            m.setCheckable(True)
            m.setFixedSize(34, 26)
            m.setToolTip("뮤트: 이 WAV 채널만 출력에서 제외")
            s = QPushButton("S")
            s.setObjectName("chSoloBtn")
            s.setCheckable(True)
            s.setFixedSize(34, 26)
            s.setToolTip("솔로: S가 켜진 채널만 재생 (하나 이상 켜면 나머지는 묵음)")
            if self._live_mode:
                m.setEnabled(False)
                s.setEnabled(False)
            bar = self._make_ch_level_bar()
            osc = QLineEdit()
            osc.setReadOnly(True)
            osc.setClearButtonEnabled(False)
            osc.setMinimumWidth(360)
            oxf = QFont("Menlo", 8)
            oxf.setStyleHint(QFont.StyleHint.Monospace)
            osc.setFont(oxf)
            osc.setPlaceholderText("—")
            osc.setStyleSheet(
                "QLineEdit { background: #1a1a1a; color: #e8e8e8; "
                "border: 1px solid #555; border-radius: 2px; padding: 2px 6px; }"
            )
            m.toggled.connect(lambda on, c=ch1: self._on_ch_mute_toggled(c, on))
            s.toggled.connect(lambda on, c=ch1: self._on_ch_solo_toggled(c, on))
            lay.addWidget(m, 0)
            lay.addWidget(s, 0)
            lay.addWidget(bar, 0)
            lay.addWidget(osc, 1)
            row_info: dict[str, object] = {
                "root": root,
                "bar": bar,
                "osc": osc,
                "mute": m,
                "solo": s,
            }
            return root, row_info

        def _rebuild_ch_monitor_widgets(self, n_rows: int) -> None:
            self._ch_monitor_rows.clear()
            self._channel_mix.clear()
            for r in range(self._ch_table.rowCount()):
                self._ch_table.removeCellWidget(r, 3)
            for r in range(n_rows):
                w, info = self._make_ch_monitor_row(r + 1)
                self._ch_table.setCellWidget(r, 3, w)
                self._ch_monitor_rows.append(info)
            if self._live_mode:
                for rowd in self._ch_monitor_rows:
                    mb = rowd.get("mute")
                    sb = rowd.get("solo")
                    if isinstance(mb, QPushButton):
                        mb.setEnabled(False)
                    if isinstance(sb, QPushButton):
                        sb.setEnabled(False)

        def _zero_ch_level_meters(self) -> None:
            for rowd in self._ch_monitor_rows:
                b = rowd.get("bar")
                if isinstance(b, QProgressBar):
                    b.setValue(0)

        def _on_audio_levels(self, peaks: object) -> None:
            if self._worker is None or self.sender() is not self._worker:
                return
            if not isinstance(peaks, list):
                return
            n = len(self._ch_monitor_rows)
            for r in range(min(n, len(peaks))):
                rowd = self._ch_monitor_rows[r]
                bar = rowd.get("bar")
                if isinstance(bar, QProgressBar):
                    p = float(peaks[r])
                    v = int(min(1000, abs(p) * 800.0))
                    bar.setValue(v)

        def _update_seek_slider_interactive(self) -> None:
            """Seek bar: enabled whenever a file is loaded (idle = set start point; playing = scrub)."""

            on = self._wav_path is not None and self._total_frames > 0
            self._seek_slider.setEnabled(on)

        def _configure_seek_for_duration(self) -> None:
            if self._total_frames <= 0 or self._sample_rate <= 0:
                self._seek_slider.blockSignals(True)
                self._seek_slider.setRange(0, 1000)
                self._seek_slider.setValue(0)
                self._seek_slider.blockSignals(False)
                self._update_seek_slider_interactive()
                return
            self._seek_slider.blockSignals(True)
            self._seek_slider.setRange(0, max(1, self._total_frames))
            self._seek_slider.setValue(0)
            self._seek_slider.blockSignals(False)
            self._update_seek_slider_interactive()
            self._update_time_label_for_frame(0)

        def _teardown_playback_worker(self) -> None:
            if self._worker is None:
                return
            try:
                self._worker.playback_ended.disconnect(self._on_playback_ended)
            except TypeError:
                pass
            try:
                self._worker.progress.disconnect(self._on_progress)
            except TypeError:
                pass
            try:
                self._worker.failed.disconnect(self._on_play_error)
            except TypeError:
                pass
            try:
                self._worker.audio_levels.disconnect(self._on_audio_levels)
            except TypeError:
                pass
            self._worker.request_stop()
            self._worker.wait(20000)
            self._worker = None

        def _build_and_start_worker(self, p: Path, start_frame: int) -> None:
            self._teardown_playback_worker()
            out_val = self._out_ch_spin.value()
            out_ch: int | None = None if out_val == 0 else out_val
            dev = coerce_audio_device(self._device_combo.currentData())
            self._osc_playback_ref.current = self._make_osc()
            self._playing_path = p.resolve()
            self._worker = PlaybackWorker(
                p,
                self._objects,
                self._osc_playback_ref,
                512,
                out_ch,
                dev,
                start_frame,
                channel_mix=self._channel_mix,
            )
            self._worker.progress.connect(self._on_progress)
            self._worker.audio_levels.connect(
                self._on_audio_levels, Qt.ConnectionType.QueuedConnection
            )
            self._worker.failed.connect(self._on_play_error)
            self._worker.playback_ended.connect(self._on_playback_ended)
            self._worker.start()
            self._set_transport_playing()
            self._playlist.setEnabled(False)
            self._btn_pl_add.setEnabled(False)
            self._status.showMessage("Playing…", 0)

        def _restart_playback_at_frame(self, frame: int, resume_paused: bool = False) -> None:
            if self._worker is None or not self._worker.isRunning():
                return
            p = self._playing_path
            if p is None or not self._objects or self._total_frames <= 0:
                return
            frame = max(0, min(int(frame), max(0, self._total_frames - 1)))
            self._teardown_playback_worker()
            self._build_and_start_worker(Path(p), frame)
            if resume_paused and self._worker is not None:

                def _pause_after_seek() -> None:
                    if self._worker is not None and self._worker.isRunning():
                        self._worker.request_pause()
                        self._set_transport_paused()

                QTimer.singleShot(40, _pause_after_seek)

        def _on_seek_slider_pressed(self) -> None:
            if self._total_frames <= 0:
                return
            self._seek_user_dragging = True

        def _on_seek_slider_released(self) -> None:
            self._seek_user_dragging = False
            if self._total_frames <= 0:
                return
            frame = min(
                max(0, self._seek_slider.value()),
                max(0, self._total_frames - 1),
            )
            self._seek_slider.blockSignals(True)
            self._seek_slider.setValue(frame)
            self._seek_slider.blockSignals(False)
            self._update_time_label_for_frame(frame)
            if self._worker is not None and self._worker.isRunning():
                was_paused = self._worker.is_paused()
                self._restart_playback_at_frame(frame, resume_paused=was_paused)

        def _on_seek_value_changed(self, value: int) -> None:
            if self._total_frames <= 0 or self._sample_rate <= 0:
                return
            if self._seek_user_dragging:
                self._update_time_label_for_frame(value)
            elif self._worker is None or not self._worker.isRunning():
                self._update_time_label_for_frame(value)

        def _apply_seek_at_seconds(self, sec: float) -> None:
            if self._total_frames <= 0 or self._sample_rate <= 0:
                return
            max_sec = self._total_frames / self._sample_rate
            sec = max(0.0, min(float(sec), max_sec))
            frame = int(sec * self._sample_rate + 0.5)
            frame = min(frame, max(0, self._total_frames - 1))
            self._seek_slider.blockSignals(True)
            self._seek_slider.setValue(frame)
            self._seek_slider.blockSignals(False)
            self._update_time_label_for_frame(frame)
            if self._worker is not None and self._worker.isRunning():
                was_paused = self._worker.is_paused()
                self._restart_playback_at_frame(frame, resume_paused=was_paused)

        def _on_time_label_seek(self) -> None:
            if self._playlist.rowCount() <= 0:
                return
            if self._playlist.currentRow() < 0:
                if self._playlist.rowCount() == 1:
                    self._playlist.selectRow(0)
                    self._playlist.setCurrentCell(0, 0)
                else:
                    return
            p = self._current_selected_path()
            if p is None or not p.is_file():
                return
            if (
                self._total_frames <= 0
                or self._wav_path != p.resolve()
                or self._sample_rate <= 0
            ):
                if not self._ensure_preview_for_play(p):
                    return
            if self._total_frames <= 0 or self._sample_rate <= 0:
                return
            frame = self._seek_slider.value()
            cur_sec = frame / float(self._sample_rate)
            default_txt = (
                self._fmt_timecode_hhmmss_frame(cur_sec) if cur_sec > 0 else "00:00:00.00"
            )
            text, ok = QInputDialog.getText(
                self,
                "Playhead",
                "Time (seconds, mm:ss, h:mm:ss):",
                text=default_txt,
            )
            if not ok:
                return
            sec = self._parse_seek_time(text)
            if sec is None:
                self._status.showMessage("Use seconds, mm:ss, or h:mm:ss", 4000)
                return
            self._apply_seek_at_seconds(sec)

        def _cycle_repeat_mode(self) -> None:
            self._repeat_mode = (self._repeat_mode + 1) % 3
            self._update_repeat_mode_button()
            self._save_settings()

        def _update_repeat_mode_button(self) -> None:
            modes = [
                (
                    "Play mode: Next in list",
                    "When the track ends, play the next item in the playlist.",
                ),
                ("Play mode: Repeat track", "Loop the current track."),
                ("Play mode: Random next", "When the track ends, pick a random other track."),
            ]
            t, tip = modes[self._repeat_mode]
            self._btn_repeat_mode.setText(t)
            self._btn_repeat_mode.setToolTip(tip)

        def _update_osc_enable_label(self) -> None:
            title = preset_display_title(self._osc_preset_id)
            self._osc_enable.setText(f"Send OSC ({title})")

        def _apply_osc_positions_at_playhead(self, osc: object) -> None:
            if not self._objects or self._sample_rate <= 0:
                return
            frame = int(self._seek_slider.value())
            frame = max(0, min(frame, max(0, self._total_frames - 1)))
            t = frame / float(self._sample_rate)
            for obj in self._objects:
                blk = active_block(obj.blocks, t)
                if blk is not None:
                    osc.send_object_config_cartesian(
                        obj.osc_object_index, blk.position.mode == "cartesian"
                    )
                    osc.send_object_position(obj, blk)

        def _sync_playback_osc_emitter(self) -> None:
            if self._worker is None or not self._worker.isRunning():
                return
            self._osc_playback_ref.current = self._make_osc()
            if self._osc_playback_ref.current is not None:
                self._apply_osc_positions_at_playhead(self._osc_playback_ref.current)

        def _on_osc_runtime_changed(self) -> None:
            self._save_settings()
            self._update_osc_enable_label()
            self._sync_playback_osc_emitter()

        def _on_prev_track(self) -> None:
            n = self._playlist.rowCount()
            if n <= 0:
                return
            if (
                self._worker is not None
                and self._worker.isRunning()
                and self._worker.is_paused()
            ):
                self._stop_playback(reset_ui=True)
                return
            row = self._playlist.currentRow()
            if row < 0:
                row = 0
            if row <= 0:
                return
            self._playlist.selectRow(row - 1)
            self._playlist.setCurrentCell(row - 1, 0)
            self._stop_playback(reset_ui=True)
            self._on_play()

        def _on_next_track(self) -> None:
            n = self._playlist.rowCount()
            if n <= 0:
                return
            row = self._playlist.currentRow()
            if row < 0:
                row = -1
            if row >= n - 1:
                return
            self._playlist.selectRow(row + 1)
            self._playlist.setCurrentCell(row + 1, 0)
            self._stop_playback(reset_ui=True)
            self._on_play()

        def _update_nav_buttons(self) -> None:
            n = self._playlist.rowCount()
            row = self._playlist.currentRow()
            self._btn_prev.setEnabled(n > 0 and row > 0)
            self._btn_next.setEnabled(n > 0 and (row < 0 or row < n - 1))

        def _load_settings(self) -> None:
            s = self._settings
            self._osc_host.setText(str(s.value("osc/host", "127.0.0.1")))
            self._osc_port.setValue(int(s.value("osc/port", 9000)))
            self._osc_enable.setChecked(bool(s.value("osc/enabled", True)))
            self._flip_az.setChecked(bool(s.value("osc/flip_az", False)))
            self._az_off.setValue(float(s.value("osc/az_offset", 0.0)))
            self._osc_preset_id = str(s.value("osc/preset", "adm"))
            self._scale_sa = float(s.value("osc/scale_az", 1.0))
            self._scale_se = float(s.value("osc/scale_el", 1.0))
            self._scale_sd = float(s.value("osc/scale_dist", 1.0))
            self._scale_sx = float(s.value("osc/scale_x", 1.0))
            self._scale_sy = float(s.value("osc/scale_y", 1.0))
            self._scale_sz = float(s.value("osc/scale_z", 1.0))
            self._out_ch_spin.setValue(int(s.value("audio/out_ch", 0)))
            self._repeat_mode = int(s.value("playback/repeat_mode", 0))
            self._osc_custom_templates = {
                "polar": str(s.value("osc/custom_polar", DEFAULT_CUSTOM_TEMPLATES["polar"])),
                "cart": str(s.value("osc/custom_cart", DEFAULT_CUSTOM_TEMPLATES["cart"])),
                "cfg": str(s.value("osc/custom_cfg", DEFAULT_CUSTOM_TEMPLATES["cfg"])),
            }
            self._control_port_spin.blockSignals(True)
            self._control_port_spin.setValue(int(s.value("osc/control_port", 9010)))
            self._control_port_spin.blockSignals(False)
            self._update_osc_enable_label()

        def _open_ext_osc_dialog(self) -> None:
            scales = {
                "sa": self._scale_sa,
                "se": self._scale_se,
                "sd": self._scale_sd,
                "sx": self._scale_sx,
                "sy": self._scale_sy,
                "sz": self._scale_sz,
            }
            dlg = ExtOscSetDialog(self, self._osc_preset_id, scales, self._osc_custom_templates)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            pid, sc, custom_tpl = dlg.result_values()
            self._osc_preset_id = pid
            self._scale_sa = sc["sa"]
            self._scale_se = sc["se"]
            self._scale_sd = sc["sd"]
            self._scale_sx = sc["sx"]
            self._scale_sy = sc["sy"]
            self._scale_sz = sc["sz"]
            self._osc_custom_templates = custom_tpl
            self._save_settings()
            self._update_osc_enable_label()
            self._sync_playback_osc_emitter()

        def _restore_audio_device_selection(self) -> None:
            v = self._settings.value("audio/device_id")
            if v in (None, ""):
                return
            try:
                did = int(v)
            except (TypeError, ValueError):
                return
            for i in range(self._device_combo.count()):
                if self._device_combo.itemData(i) == did:
                    self._device_combo.setCurrentIndex(i)
                    return

        def _save_settings(self) -> None:
            s = self._settings
            s.setValue("osc/host", self._osc_host.text())
            s.setValue("osc/port", int(self._osc_port.value()))
            s.setValue("osc/enabled", self._osc_enable.isChecked())
            s.setValue("osc/flip_az", self._flip_az.isChecked())
            s.setValue("osc/az_offset", float(self._az_off.value()))
            s.setValue("osc/preset", self._osc_preset_id)
            s.setValue("osc/scale_az", self._scale_sa)
            s.setValue("osc/scale_el", self._scale_se)
            s.setValue("osc/scale_dist", self._scale_sd)
            s.setValue("osc/scale_x", self._scale_sx)
            s.setValue("osc/scale_y", self._scale_sy)
            s.setValue("osc/scale_z", self._scale_sz)
            s.setValue("audio/out_ch", int(self._out_ch_spin.value()))
            s.setValue("audio/device_id", self._device_combo.currentData())
            s.setValue("playback/repeat_mode", self._repeat_mode)
            s.setValue("osc/control_port", int(self._control_port_spin.value()))
            s.setValue("osc/custom_polar", self._osc_custom_templates.get("polar", ""))
            s.setValue("osc/custom_cart", self._osc_custom_templates.get("cart", ""))
            s.setValue("osc/custom_cfg", self._osc_custom_templates.get("cfg", ""))

        def _connect_settings_autosave(self) -> None:
            self._osc_host.editingFinished.connect(self._on_osc_runtime_changed)
            self._osc_port.valueChanged.connect(self._on_osc_runtime_changed)
            self._osc_enable.toggled.connect(self._on_osc_runtime_changed)
            self._flip_az.toggled.connect(self._on_osc_runtime_changed)
            self._az_off.valueChanged.connect(self._on_osc_runtime_changed)
            self._control_port_spin.valueChanged.connect(self._on_control_port_changed)
            self._out_ch_spin.valueChanged.connect(self._save_settings)
            self._device_combo.currentIndexChanged.connect(self._save_settings)

        def _on_control_port_changed(self) -> None:
            self._save_settings()
            self._restart_osc_control_server()

        def _connect_osc_control_signals(self) -> None:
            oc = self._osc_ctrl
            oc.play.connect(self._on_play, Qt.ConnectionType.QueuedConnection)
            oc.pause.connect(self._on_pause, Qt.ConnectionType.QueuedConnection)
            oc.stop.connect(self._on_stop, Qt.ConnectionType.QueuedConnection)
            oc.next_track.connect(self._on_next_track, Qt.ConnectionType.QueuedConnection)
            oc.prev_track.connect(self._on_prev_track, Qt.ConnectionType.QueuedConnection)
            oc.playmode.connect(self._on_osc_control_playmode, Qt.ConnectionType.QueuedConnection)
            oc.playlist_play.connect(self._on_osc_control_playlist_play, Qt.ConnectionType.QueuedConnection)

        def _on_osc_control_playmode(self, mode: int) -> None:
            if 1 <= mode <= 3:
                self._repeat_mode = mode - 1
                self._update_repeat_mode_button()
                self._save_settings()

        def _on_osc_control_playlist_play(self, one_based: int) -> None:
            row = one_based - 1
            if row < 0 or row >= self._playlist.rowCount():
                return
            self._playlist.selectRow(row)
            self._playlist.setCurrentCell(row, 0)
            self._stop_playback(reset_ui=True)
            QTimer.singleShot(300, self._on_play)

        def _stop_osc_control_server(self) -> None:
            srv = self._control_osc_server
            if srv is None:
                return
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
            self._control_osc_server = None
            self._control_osc_server_thread = None

        def _restart_osc_control_server(self) -> None:
            self._stop_osc_control_server()
            port = int(self._control_port_spin.value())
            if port <= 0:
                return
            d = Dispatcher()
            b = self._osc_ctrl

            def emit_play(_addr: str, *_a: object) -> None:
                b.play.emit()

            def emit_pause(_addr: str, *_a: object) -> None:
                b.pause.emit()

            def emit_stop(_addr: str, *_a: object) -> None:
                b.stop.emit()

            def emit_next(_addr: str, *_a: object) -> None:
                b.next_track.emit()

            def emit_prev(_addr: str, *_a: object) -> None:
                b.prev_track.emit()

            def playmode_handler(_addr: str, *args: object) -> None:
                if not args:
                    return
                try:
                    v = int(float(args[0]))
                except (TypeError, ValueError):
                    return
                if 1 <= v <= 3:
                    b.playmode.emit(v)

            def playlist_default(address: str, *args: object) -> None:
                m = re.match(r"^/playlist/(\d+)/play$", address)
                if m:
                    b.playlist_play.emit(int(m.group(1)))

            d.map("/play", emit_play)
            d.map("/pause", emit_pause)
            d.map("/stop", emit_stop)
            d.map("/next", emit_next)
            d.map("/previous", emit_prev)
            d.map("/playmode", playmode_handler)
            d.set_default_handler(playlist_default)
            try:
                srv = osc_server.ThreadingOSCUDPServer(("0.0.0.0", port), d)
            except OSError as e:
                self._status.showMessage(f"Control port {port}: {e}", 6000)
                return
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            self._control_osc_server = srv
            self._control_osc_server_thread = th

        def _save_playlist_dialog(self) -> None:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save playlist",
                str(Path.home()),
                "ADM Player playlist (*.json)",
            )
            if not path:
                return
            p = Path(path)
            if p.suffix.lower() != ".json":
                p = p.with_suffix(".json")
            keys: list[str] = []
            for r in range(self._playlist.rowCount()):
                k = self._playlist_key_at_row(r)
                if k:
                    keys.append(k)
            try:
                p.write_text(json.dumps({"version": 1, "paths": keys}, indent=2), encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Playlist", str(e))
                return
            self._status.showMessage(f"Saved playlist ({len(keys)} items)", 3000)

        def _load_playlist_dialog(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Load playlist",
                str(Path.home()),
                "ADM Player playlist (*.json);;All files (*.*)",
            )
            if not path:
                return
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                QMessageBox.warning(self, "Playlist", str(e))
                return
            paths = data.get("paths") if isinstance(data, dict) else None
            if not isinstance(paths, list):
                QMessageBox.warning(self, "Playlist", "Invalid playlist file.")
                return
            self._playlist_clear()
            for s in paths:
                pp = Path(str(s))
                if pp.is_file() and pp.suffix.lower() == ".wav":
                    self._add_playlist_item(pp)
            if self._playlist.rowCount() and self._playlist.currentRow() < 0:
                self._playlist.selectRow(0)
            self._update_nav_buttons()
            self._status.showMessage(f"Loaded playlist ({self._playlist.rowCount()} items)", 3000)

        def _replay_current_track(self) -> None:
            if self._playing_path is None:
                return
            p = Path(self._playing_path)
            if not p.is_file():
                return
            self._seek_slider.blockSignals(True)
            self._seek_slider.setValue(0)
            self._seek_slider.blockSignals(False)
            self._update_time_label_for_frame(0)
            if not self._ensure_preview_for_play(p):
                return
            self._build_and_start_worker(p, 0)

        def _play_random_next(self) -> None:
            n = self._playlist.rowCount()
            if n <= 0:
                return
            if n == 1:
                self._replay_current_track()
                return
            cur_key = str(self._playing_path.resolve()) if self._playing_path else None
            others = [i for i in range(n) if self._playlist_key_at_row(i) != cur_key]
            if not others:
                self._replay_current_track()
                return
            pick = random.choice(others)
            self._playlist.selectRow(pick)
            self._playlist.setCurrentCell(pick, 0)
            QTimer.singleShot(100, self._on_play)

        def _fill_devices(self) -> None:
            self._device_combo.clear()
            self._device_combo.addItem("System default", None)
            try:
                default_out = sd.default.device[1]
            except (TypeError, IndexError, KeyError):
                default_out = None
            for i, d in enumerate(sd.query_devices()):
                n_out = int(d.get("max_output_channels") or 0)
                if n_out < 1:
                    continue
                name = d.get("name", "?")
                label = f"[{i}] {name} (max {n_out} ch)"
                if default_out is not None and i == default_out:
                    label += "  · default"
                self._device_combo.addItem(label, i)

        def _register_metadata_worker(self, w: MetadataLoadWorker) -> None:
            """Keep running QThread instances referenced so they are not GC'd; clean up when done."""

            self._active_metadata_workers.append(w)

            def _cleanup() -> None:
                try:
                    self._active_metadata_workers.remove(w)
                except ValueError:
                    pass
                w.deleteLater()

            w.finished.connect(_cleanup)

        def _playlist_key_at_row(self, row: int) -> str | None:
            it = self._playlist.item(row, 0)
            if it is None:
                return None
            d = it.data(Qt.ItemDataRole.UserRole)
            return str(d) if d else None

        def _find_playlist_row_by_key(self, key: str) -> int:
            for r in range(self._playlist.rowCount()):
                if self._playlist_key_at_row(r) == key:
                    return r
            return -1

        def _apply_playlist_row(
            self,
            row: int,
            p: Path,
            data: PreviewData | None,
            *,
            loading: bool = False,
            error: str | None = None,
        ) -> None:
            key = str(p.resolve())
            path_txt = str(p.resolve())
            title = p.name
            if error:
                ch = obj = hz = fmt = dur_txt = "—"
                status = "Error"
                tip = f"{path_txt}\n{error}"
            else:
                if data is None:
                    data = self._preview_cache.get(key)
                if data is None:
                    ch = obj = hz = fmt = dur_txt = "—"
                    status = "Loading…" if loading else "…"
                    tip = path_txt
                else:
                    dur_s = data.total_frames / data.sample_rate if data.sample_rate else 0.0
                    dur_txt = self._fmt_time(dur_s)
                    ch = str(data.n_ch)
                    obj = str(len(data.objects))
                    hz = str(int(data.sample_rate))
                    fmt = data.finfo
                    status = "Ready"
                    tip = f"{path_txt}\n{hz} Hz · {ch}ch · {dur_txt} · objects {obj}"

            it0 = QTableWidgetItem(str(row + 1))
            it0.setData(Qt.ItemDataRole.UserRole, key)
            it0.setToolTip(tip)
            self._playlist.setItem(row, 0, it0)
            t1 = QTableWidgetItem(title)
            t1.setToolTip(tip)
            self._playlist.setItem(row, 1, t1)
            for c, val in enumerate((ch, obj, dur_txt, hz, fmt, status), start=2):
                tx = QTableWidgetItem(val)
                tx.setToolTip(tip)
                self._playlist.setItem(row, c, tx)
            pw = max(100, self._playlist.columnWidth(8) - 12)
            fm = self._playlist.fontMetrics()
            path_show = fm.elidedText(path_txt, Qt.TextElideMode.ElideMiddle, pw)
            t_path = QTableWidgetItem(path_show)
            t_path.setToolTip(path_txt)
            t_path.setData(Qt.ItemDataRole.UserRole, path_txt)
            self._playlist.setItem(row, 8, t_path)
            if error:
                err_cell = self._playlist.item(row, 7)
                if err_cell is not None:
                    err_cell.setText(err[:200])
                    err_cell.setToolTip(error)
            if row == self._playlist.currentRow():
                self._update_now_playing_title()
            self._refresh_transport_play_if_idle()

        def _renumber_playlist_order_column(self) -> None:
            for r in range(self._playlist.rowCount()):
                it = self._playlist.item(r, 0)
                if it is not None:
                    it.setText(str(r + 1))

        def _swap_playlist_rows(self, a: int, b: int) -> None:
            cols = self._playlist.columnCount()
            for c in range(cols):
                ia = self._playlist.takeItem(a, c)
                ib = self._playlist.takeItem(b, c)
                self._playlist.setItem(a, c, ib)
                self._playlist.setItem(b, c, ia)

        def _update_playlist_row_for_key(self, key: str, data: PreviewData) -> None:
            row = self._find_playlist_row_by_key(key)
            if row < 0:
                return
            self._apply_playlist_row(row, data.path, data, loading=False)

        def _update_playlist_row_error(self, key: str, err: str) -> None:
            row = self._find_playlist_row_by_key(key)
            if row < 0:
                return
            self._apply_playlist_row(row, Path(key), None, loading=False, error=err)

        def _playlist_add_files(self) -> None:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Add ADM BWF",
                str(Path.home()),
                "ADM BWF (*.wav *.WAV);;All files (*.*)",
            )
            for path in paths:
                p = Path(path)
                if p.is_file() and p.suffix.lower() == ".wav":
                    self._add_playlist_item(p)
            if self._playlist.rowCount() and self._playlist.currentRow() < 0:
                self._playlist.selectRow(0)
            self._update_nav_buttons()

        def _add_playlist_item(self, p: Path) -> None:
            r = p.resolve()
            key = str(r)
            cached = self._preview_cache.get(key)
            row = self._playlist.rowCount()
            self._playlist.insertRow(row)
            self._apply_playlist_row(row, r, cached, loading=cached is None)
            self._queue_prefetch(r)
            self._update_nav_buttons()

        def _playlist_remove(self) -> None:
            row = self._playlist.currentRow()
            if row >= 0:
                self._playlist.removeRow(row)
                self._renumber_playlist_order_column()
            self._update_nav_buttons()

        def _playlist_clear(self) -> None:
            self._playlist.setRowCount(0)
            self._preview_cache.clear()
            self._prefetch_queue.clear()
            self._clear_preview()
            self._update_nav_buttons()

        def _playlist_move_up(self) -> None:
            row = self._playlist.currentRow()
            if row > 0:
                self._swap_playlist_rows(row, row - 1)
                self._renumber_playlist_order_column()
                self._playlist.selectRow(row - 1)
                self._playlist.setCurrentCell(row - 1, 0)

        def _playlist_move_down(self) -> None:
            row = self._playlist.currentRow()
            if row >= 0 and row < self._playlist.rowCount() - 1:
                self._swap_playlist_rows(row, row + 1)
                self._renumber_playlist_order_column()
                self._playlist.selectRow(row + 1)
                self._playlist.setCurrentCell(row + 1, 0)

        def _on_playlist_selection_changed(self) -> None:
            self._update_now_playing_title()
            self._update_nav_buttons()
            if self._worker is None:
                self._btn_play.setEnabled(self._can_start_playback())
            if self._playlist.currentRow() < 0:
                self._debounce_sel.stop()
                self._clear_preview()
                return
            self._debounce_sel.start()

        def _debounced_load_selection_preview(self) -> None:
            p = self._current_selected_path()
            if p is None:
                self._clear_preview()
                return
            key = str(p.resolve())
            if key in self._preview_cache:
                self._apply_preview_data(self._preview_cache[key])
                self._status.showMessage(f"Preview: {p.name}", 2500)
                self._schedule_prefetch_neighbors()
                return
            self._show_preview_loading()
            self._preview_load_id += 1
            lid = self._preview_load_id
            w = MetadataLoadWorker(p, self)
            self._selection_metadata_worker = w
            self._register_metadata_worker(w)

            def _sel_thread_done() -> None:
                if self._selection_metadata_worker is w:
                    self._selection_metadata_worker = None

            w.finished.connect(_sel_thread_done)
            w.finished_ok.connect(lambda k, d, lid=lid: self._on_sel_metadata_ok(k, d, lid))
            w.failed.connect(lambda k, e, lid=lid: self._on_sel_metadata_fail(k, e, lid))
            w.start()

        def _show_preview_loading(self) -> None:
            self._wav_path = None
            self._objects = []
            self._total_frames = 0
            self._sum_sample.setText("…")
            self._sum_ch.setText("…")
            self._sum_dur.setText("…")
            self._sum_fmt.setText("…")
            self._sum_obj.setText("…")
            self._ch_table.setRowCount(0)
            self._ch_monitor_rows.clear()
            self._channel_mix.clear()
            self._seek_slider.setEnabled(False)

        def _on_sel_metadata_ok(self, key: str, data: object, lid: int) -> None:
            if not isinstance(data, PreviewData):
                return
            self._preview_cache[key] = data
            self._update_playlist_row_for_key(key, data)
            if lid != self._preview_load_id:
                return
            cur = self._current_selected_path()
            if cur is None or str(cur.resolve()) != key:
                return
            self._apply_preview_data(data)
            self._status.showMessage(f"Preview: {data.path.name}", 2500)
            self._schedule_prefetch_neighbors()

        def _on_sel_metadata_fail(self, key: str, err: str, lid: int) -> None:
            self._update_playlist_row_error(key, err)
            if lid != self._preview_load_id:
                return
            cur = self._current_selected_path()
            if cur is None or str(cur.resolve()) != key:
                return
            self._clear_preview()
            self._status.showMessage(f"Load failed: {err}", 5000)

        def _apply_preview_data(self, data: PreviewData) -> None:
            self._wav_path = data.path
            self._objects = data.objects
            self._sample_rate = data.sample_rate
            self._total_frames = data.total_frames
            duration = data.total_frames / data.sample_rate if data.sample_rate else 0.0
            self._sum_sample.setText(f"{int(data.sample_rate)} Hz")
            self._sum_ch.setText(str(data.n_ch))
            self._sum_dur.setText(f"{self._fmt_time(duration)} ({duration:.1f}s)")
            self._sum_fmt.setText(data.finfo)
            self._sum_obj.setText(f"{len(data.objects)} (OSC ↔ WAV channel #)")
            self._ch_table.setRowCount(len(data.rows))
            for r, (ch, uid, meta) in enumerate(data.rows):
                self._ch_table.setItem(r, 0, QTableWidgetItem(str(ch)))
                self._ch_table.setItem(r, 1, QTableWidgetItem(uid))
                self._ch_table.setItem(r, 2, QTableWidgetItem(meta))
            self._rebuild_ch_monitor_widgets(len(data.rows))
            self._ch_table.resizeColumnsToContents()
            self._configure_seek_for_duration()
            if self._worker is None:
                self._set_transport_idle()

        def _schedule_prefetch_neighbors(self) -> None:
            self._prefetch_timer.stop()
            self._prefetch_timer.start()

        def _do_prefetch_neighbors(self) -> None:
            row = self._playlist.currentRow()
            n = self._playlist.rowCount()
            if row < 0 or n == 0:
                return
            for delta in (1, -1):
                r = row + delta
                if 0 <= r < n:
                    k = self._playlist_key_at_row(r)
                    if k:
                        self._queue_prefetch(Path(k))

        def _queue_prefetch(self, p: Path) -> None:
            key = str(p.resolve())
            if key in self._preview_cache:
                return
            if not p.is_file():
                return
            if key not in self._prefetch_queue:
                self._prefetch_queue.append(key)
            self._pump_prefetch_queue()

        def _pump_prefetch_queue(self) -> None:
            if self._prefetch_worker is not None and self._prefetch_worker.isRunning():
                return
            while self._prefetch_queue:
                k = self._prefetch_queue.pop(0)
                if k in self._preview_cache:
                    continue
                p = Path(k)
                if not p.is_file():
                    continue
                w = MetadataLoadWorker(p, self)
                self._prefetch_worker = w
                self._register_metadata_worker(w)

                def _on_prefetch_thread_done() -> None:
                    if self._prefetch_worker is w:
                        self._prefetch_worker = None
                    self._pump_prefetch_queue()

                w.finished_ok.connect(self._on_prefetch_metadata_ok)
                w.failed.connect(self._on_prefetch_metadata_fail)
                w.finished.connect(_on_prefetch_thread_done)
                w.start()
                return

        def _on_prefetch_metadata_ok(self, key: str, data: object) -> None:
            if not isinstance(data, PreviewData):
                return
            self._preview_cache[key] = data
            self._update_playlist_row_for_key(key, data)

        def _on_prefetch_metadata_fail(self, key: str, err: str) -> None:
            self._update_playlist_row_error(key, err)

        def _on_playlist_cell_double_clicked(self, row: int, _col: int) -> None:
            self._playlist.selectRow(row)
            self._on_play()

        def _current_selected_path(self) -> Path | None:
            row = self._playlist.currentRow()
            if row < 0:
                return None
            k = self._playlist_key_at_row(row)
            return Path(k) if k else None

        def _clear_preview(self) -> None:
            self._wav_path = None
            self._objects = []
            self._total_frames = 0
            self._sum_sample.setText("—")
            self._sum_ch.setText("—")
            self._sum_dur.setText("—")
            self._sum_fmt.setText("—")
            self._sum_obj.setText("—")
            self._ch_table.setRowCount(0)
            self._ch_monitor_rows.clear()
            self._channel_mix.clear()
            self._seek_slider.blockSignals(True)
            self._seek_slider.setRange(0, 1000)
            self._seek_slider.setValue(0)
            self._seek_slider.blockSignals(False)
            self._time_label.setText("00:00:00.00 / 00:00:00.00")
            self._set_transport_idle()

        def _ensure_preview_for_play(self, p: Path) -> bool:
            key = str(p.resolve())
            if key not in self._preview_cache:
                try:
                    data = build_preview_data(p)
                except Exception as e:
                    QMessageBox.warning(self, "File", str(e))
                    return False
                self._preview_cache[key] = data
                self._update_playlist_row_for_key(key, data)
            self._apply_preview_data(self._preview_cache[key])
            return bool(self._objects)

        def _set_transport_idle(self) -> None:
            self._btn_play.setEnabled(self._can_start_playback())
            self._btn_pause.setEnabled(False)
            self._btn_stop.setEnabled(False)
            self._update_seek_slider_interactive()
            self._update_nav_buttons()

        def _set_transport_playing(self) -> None:
            self._btn_play.setEnabled(False)
            self._btn_pause.setEnabled(True)
            self._btn_stop.setEnabled(True)
            self._update_seek_slider_interactive()
            self._update_nav_buttons()

        def _set_transport_paused(self) -> None:
            self._btn_play.setEnabled(True)
            self._btn_pause.setEnabled(False)
            self._btn_stop.setEnabled(True)
            self._update_seek_slider_interactive()
            self._update_nav_buttons()

        def _make_osc(self):
            if not self._osc_enable.isChecked():
                return None
            preset = self._osc_preset_id or "adm"
            scales = {
                "sa": self._scale_sa,
                "se": self._scale_se,
                "sd": self._scale_sd,
                "sx": self._scale_sx,
                "sy": self._scale_sy,
                "sz": self._scale_sz,
            }
            osc = create_osc_emitter(
                preset,
                self._osc_host.text().strip() or "127.0.0.1",
                int(self._osc_port.value()),
                azimuth_offset=float(self._az_off.value()),
                azimuth_flip=self._flip_az.isChecked(),
                on_send=self._osc_log_cb,
                scales=scales,
                custom_templates=self._osc_custom_templates if preset == "custom" else None,
            )
            for obj in self._objects:
                blk = obj.blocks[0] if obj.blocks else None
                if blk is not None:
                    osc.send_object_config_cartesian(obj.osc_object_index, blk.position.mode == "cartesian")
                    osc.send_object_position(obj, blk)
            return osc

        def _on_play(self) -> None:
            if self._playlist.currentRow() < 0:
                if self._playlist.rowCount() == 1:
                    self._playlist.selectRow(0)
                    self._playlist.setCurrentCell(0, 0)
                else:
                    QMessageBox.information(self, "Playback", "Select a file in the playlist.")
                    return
            if not self._can_start_playback():
                row = self._effective_playlist_row()
                stx = "—"
                if row >= 0:
                    it = self._playlist.item(row, 7)
                    if it is not None:
                        stx = it.text()
                QMessageBox.information(
                    self,
                    "Playback",
                    f"메타데이터가 준비(Ready)된 뒤에 재생할 수 있습니다. 현재 상태: {stx}",
                )
                return
            p = self._current_selected_path()
            if p is None or not p.is_file():
                QMessageBox.information(self, "Playback", "Select a file in the playlist.")
                return
            p_res = p.resolve()
            preserved_frame = 0
            if (
                self._wav_path is not None
                and self._wav_path.resolve() == p_res
                and self._total_frames > 0
            ):
                preserved_frame = min(
                    max(0, self._seek_slider.value()),
                    max(0, self._total_frames - 1),
                )
            if not self._ensure_preview_for_play(p):
                return
            if not self._objects:
                return

            if self._worker is not None and self._worker.isRunning():
                same_track = (
                    self._playing_path is not None
                    and self._playing_path.resolve() == p_res
                )
                if same_track:
                    if self._worker.is_paused():
                        self._worker.request_resume()
                        self._set_transport_playing()
                        self._status.showMessage("Playing", 0)
                    return
                self._teardown_playback_worker()

            sf = 0
            if self._total_frames > 0:
                sf = min(
                    max(0, preserved_frame),
                    max(0, self._total_frames - 1),
                )
                self._seek_slider.blockSignals(True)
                self._seek_slider.setValue(sf)
                self._seek_slider.blockSignals(False)
                self._update_time_label_for_frame(sf)
            self._build_and_start_worker(p, sf)

        def _on_pause(self) -> None:
            if self._worker is None or not self._worker.isRunning():
                return
            self._worker.request_pause()
            self._set_transport_paused()
            self._status.showMessage("Paused", 0)

        def _on_stop(self) -> None:
            self._stop_playback(reset_ui=True)

        def _on_progress(self, pos_frames: int, total_frames: int, sr: int) -> None:
            # 워커가 stop 후에도 큐에 남은 progress 한 번이 슬라이더를 다시 덮어쓰는 경우가 있어 무시합니다.
            if self._worker is None or self.sender() is not self._worker:
                return
            if total_frames <= 0:
                return
            elapsed = pos_frames / float(sr)
            total = total_frames / float(sr)
            self._time_label.setText(
                f"{self._fmt_timecode_hhmmss_frame(elapsed)} / "
                f"{self._fmt_timecode_hhmmss_frame(total)}"
            )
            if not self._seek_user_dragging:
                self._seek_slider.blockSignals(True)
                self._seek_slider.setMaximum(max(1, total_frames))
                self._seek_slider.setValue(min(pos_frames, total_frames))
                self._seek_slider.blockSignals(False)

        def _on_play_error(self, msg: str) -> None:
            if self.sender() is not self._worker:
                return
            QMessageBox.warning(self, "Playback error", msg)
            self._stop_playback(reset_ui=True)

        def _on_playback_ended(self, natural_eof: bool) -> None:
            if self.sender() is not self._worker:
                return
            self._zero_ch_level_meters()
            self._worker = None
            self._playlist.setEnabled(True)
            self._btn_pl_add.setEnabled(True)
            if not natural_eof:
                self._status.showMessage("Stopped", 2000)
                self._set_transport_idle()
                return
            self._status.showMessage("Playback finished", 3000)
            if self._total_frames > 0 and self._sample_rate:
                t = self._total_frames / self._sample_rate
                self._time_label.setText(
                    f"{self._fmt_timecode_hhmmss_frame(t)} / {self._fmt_timecode_hhmmss_frame(t)}"
                )
                self._seek_slider.blockSignals(True)
                self._seek_slider.setMaximum(max(1, self._total_frames))
                self._seek_slider.setValue(self._total_frames)
                self._seek_slider.blockSignals(False)
            self._set_transport_idle()
            if self._playing_path is None:
                return
            if self._repeat_mode == 0:
                self._play_next_in_list()
            elif self._repeat_mode == 1:
                QTimer.singleShot(80, self._replay_current_track)
            elif self._repeat_mode == 2:
                QTimer.singleShot(80, self._play_random_next)

        def _play_next_in_list(self) -> None:
            if self._playing_path is None:
                return
            want = str(self._playing_path.resolve())
            for i in range(self._playlist.rowCount()):
                if self._playlist_key_at_row(i) == want:
                    if i + 1 < self._playlist.rowCount():
                        self._playlist.selectRow(i + 1)
                        self._playlist.setCurrentCell(i + 1, 0)
                        QTimer.singleShot(100, self._on_play)
                    break

        def _stop_playback(self, reset_ui: bool = False) -> None:
            self._teardown_playback_worker()
            self._playlist.setEnabled(True)
            self._btn_pl_add.setEnabled(True)
            if reset_ui:
                self._configure_seek_for_duration()
            self._zero_ch_level_meters()
            self._set_transport_idle()

        def dragEnterEvent(self, event: QDragEnterEvent) -> None:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                super().dragEnterEvent(event)

        def dropEvent(self, event: QDropEvent) -> None:
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_file() and p.suffix.lower() == ".wav":
                    self._add_playlist_item(p)
            if self._playlist.rowCount() and self._playlist.currentRow() < 0:
                self._playlist.selectRow(0)
            event.acceptProposedAction()

        def closeEvent(self, event) -> None:
            self._save_settings()
            self._settings.setValue("ui/geometry", self.saveGeometry())
            self._stop_osc_control_server()
            self._stop_playback()
            self._debounce_sel.stop()
            self._prefetch_timer.stop()
            for w in list(self._active_metadata_workers):
                if w.isRunning():
                    w.wait(120000)
            event.accept()

    app = QApplication(sys.argv)
    app.setApplicationName("ADM Player")
    app.setOrganizationName("DREAM SCAPE")

    splash = StartupSplash(resolve_app_logo_path())
    splash.place_center()
    splash.show()
    app.processEvents()

    w = MainWindow(startup_splash=splash)
    w.show()

    def _close_startup_splash() -> None:
        splash.close()
        splash.deleteLater()
        w.raise_()
        w.activateWindow()

    QTimer.singleShot(0, _close_startup_splash)

    return int(app.exec())
