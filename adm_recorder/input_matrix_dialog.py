"""Device inputs → interleaved record tracks (patch matrix)."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .input_routing import resize_route


class InputMatrixDialog(QDialog):
    def __init__(
        self,
        n_in: int,
        n_out: int,
        route: np.ndarray,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Input matrix")
        self.resize(900, 520)
        self._n_in = max(1, n_in)
        self._n_out = max(1, n_out)
        self._mat = resize_route(route, self._n_in, self._n_out).astype(np.float32).copy()

        root = QVBoxLayout(self)
        self._table = QTableWidget(self._n_in, self._n_out)
        self._table.setVerticalHeaderLabels([f"In {i + 1}" for i in range(self._n_in)])
        self._table.setHorizontalHeaderLabels([f"{j + 1}" for j in range(self._n_out)])
        self._table.verticalHeader().setDefaultSectionSize(28)
        self._table.horizontalHeader().setDefaultSectionSize(36)
        self._table.setShowGrid(True)
        self._table.cellClicked.connect(self._on_cell_clicked)
        for r in range(self._n_in):
            for c in range(self._n_out):
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(r, c, it)
                self._paint_cell(r, c)
        root.addWidget(self._table, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        one_btn = QPushButton("1:1")
        one_btn.clicked.connect(self._identity_full)
        row.addWidget(one_btn)
        root.addLayout(row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def route_matrix(self) -> np.ndarray:
        return self._mat.copy()

    def _is_on(self, r: int, c: int) -> bool:
        return float(self._mat[r, c]) >= 0.5

    def _paint_cell(self, r: int, c: int) -> None:
        it = self._table.item(r, c)
        if it is None:
            return
        if self._is_on(r, c):
            it.setBackground(QBrush(QColor("#1f7a3a")))
            it.setForeground(QBrush(QColor("#e8ffe8")))
            it.setText("●")
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            it.setBackground(QBrush(QColor("#0a0a0a")))
            it.setForeground(QBrush(QColor("#303030")))
            it.setText("")
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        mods = QApplication.keyboardModifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._identity_full()
            return
        if float(self._mat[row, col]) >= 0.5:
            self._mat[row, col] = 0.0
        else:
            self._mat[row, col] = 1.0
        self._paint_cell(row, col)

    def _identity_full(self) -> None:
        self._mat.fill(0.0)
        for i in range(min(self._n_in, self._n_out)):
            self._mat[i, i] = 1.0
        for r in range(self._n_in):
            for c in range(self._n_out):
                self._paint_cell(r, c)
