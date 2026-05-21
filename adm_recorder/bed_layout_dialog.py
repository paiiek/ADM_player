from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .channel_config import BED_LAYOUTS_BY_ID, ChannelMapState, ChannelRole

_BED_ORDER = ("stereo", "5_1", "7_1", "7_1_2", "7_1_4", "objects_only")


class BedLayoutDialog(QDialog):
    def __init__(self, cmap: ChannelMapState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bed layout")
        self._cmap = cmap
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "First N interleaved tracks are bed; remaining tracks are objects "
                "(N = speaker count for this layout)."
            )
        )
        self._combo = QComboBox()
        for lid in _BED_ORDER:
            if lid not in BED_LAYOUTS_BY_ID:
                continue
            lay = BED_LAYOUTS_BY_ID[lid]
            self._combo.addItem(f"{lay.label}", lid)
        idx = self._combo.findData(cmap.bed_layout_id)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        root.addWidget(self._combo)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _apply(self) -> None:
        lid = self._combo.currentData()
        if isinstance(lid, str):
            self._cmap.bed_layout_id = lid
        n_bed = len(self._cmap.bed_layout().speakers)
        n = self._cmap.n_channels
        for r in range(n):
            self._cmap.roles[r] = ChannelRole.BED if r < n_bed else ChannelRole.OBJECT
        self.accept()
