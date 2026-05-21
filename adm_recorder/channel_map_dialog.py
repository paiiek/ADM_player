"""Channel map dialog: bed/object role + input level meters."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .channel_config import ChannelMapState, ChannelRole


def make_level_bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setOrientation(Qt.Orientation.Horizontal)
    bar.setRange(0, 1000)
    bar.setValue(0)
    bar.setTextVisible(False)
    bar.setFixedHeight(16)
    bar.setMinimumWidth(140)
    bar.setMinimumHeight(16)
    bar.setStyleSheet(
        "QProgressBar { border: 1px solid #555; border-radius: 3px; "
        "background-color: #1e1e1e; min-height: 16px; }"
        "QProgressBar::chunk { border-radius: 2px; "
        "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, "
        "stop:0 #14532d, stop:0.55 #3ecf6e, stop:1 #d9f99d); }"
    )
    return bar


class ChannelMapDialog(QDialog):
    def __init__(
        self,
        cmap: ChannelMapState,
        levels_bridge: QObject,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Channel map · levels")
        self.setMinimumSize(920, 560)
        self.resize(1040, 680)
        self._cmap = cmap
        self._levels_bridge = levels_bridge
        self._level_bars: list[QProgressBar] = []

        root = QVBoxLayout(self)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Ch", "Level", "Role", "Note"])
        self._table.horizontalHeader().setStretchLastSection(True)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 56)
        hdr.setSectionResizeMode(1, hdr.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, hdr.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 120)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(36)
        root.addWidget(self._table, 1)

        apply_row = QHBoxLayout()
        apply_row.addWidget(QLabel("Role for selection:"))
        self._role_apply = QComboBox()
        self._role_apply.addItem("Object")
        self._role_apply.addItem("Bed")
        self._btn_apply = QPushButton("Apply")
        self._btn_apply.clicked.connect(self._apply_role_selection)
        apply_row.addWidget(self._role_apply)
        apply_row.addWidget(self._btn_apply)
        apply_row.addStretch(1)
        root.addLayout(apply_row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        root.addWidget(bb)

        self._rebuild_table()
        self._levels_bridge.peaks.connect(self._on_peaks, Qt.ConnectionType.QueuedConnection)

    def closeEvent(self, event) -> None:
        try:
            self._levels_bridge.peaks.disconnect(self._on_peaks)
        except TypeError:
            pass
        super().closeEvent(event)

    def _on_peaks(self, peaks: object) -> None:
        if not isinstance(peaks, list):
            return
        for i, bar in enumerate(self._level_bars):
            if i >= len(peaks):
                bar.setValue(0)
                continue
            try:
                p = float(peaks[i])
            except (TypeError, ValueError):
                p = 0.0
            bar.setValue(int(min(1000, abs(p) * 800.0)))

    def _rebuild_table(self) -> None:
        self._table.clearContents()
        n = self._cmap.n_channels
        self._table.setRowCount(n)
        self._level_bars.clear()
        for r in range(n):
            ch = r + 1
            it0 = QTableWidgetItem(str(ch))
            it0.setFlags(it0.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 0, it0)

            bar = make_level_bar()
            self._level_bars.append(bar)
            self._table.setCellWidget(r, 1, bar)

            role = self._cmap.roles[r]
            it2 = QTableWidgetItem("Bed" if role == ChannelRole.BED else "Object")
            it2.setFlags(it2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 2, it2)

            note = ""
            if role == ChannelRole.BED:
                note = "Bed slot: channel order ↔ layout speakers"
            self._table.setItem(r, 3, QTableWidgetItem(note))

    def refresh_from_cmap(self) -> None:
        """Call after parent changes track count only."""
        self._rebuild_table()

    def _apply_role_selection(self) -> None:
        idx = self._role_apply.currentIndex()
        role = ChannelRole.OBJECT if idx == 0 else ChannelRole.BED
        sm = self._table.selectionModel()
        if sm is None:
            return
        rows = sorted({ix.row() for ix in sm.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Channel map", "Select one or more rows first.")
            return
        for rr in rows:
            if 0 <= rr < len(self._cmap.roles):
                self._cmap.roles[rr] = role
        self._rebuild_table()
