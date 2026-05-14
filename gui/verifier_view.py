"""Verifier alerts: list of residual leaks; click-through opens the file."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonymize.verifier import LeakHit

from .state import AppState
from .toast import Toaster


class VerifierView(QWidget):
    open_in_diff_requested = Signal(str)  # file_rel
    send_to_review_requested = Signal(list)  # list[LeakHit]

    HEADERS = ("File", "Pattern", "Match", "Snippet")

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._summary_base = "Verifier not run yet"
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.textChanged.connect(self._apply_filter)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.cellDoubleClicked.connect(self._on_double)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self._row_hits: list[LeakHit] = []

        self.lbl = QLabel("Verifier not run yet")
        self.lbl.setObjectName("Muted")

        btn_open = QPushButton("Open selected in Diff")
        btn_open.clicked.connect(self._open_selected)

        btn_send = QPushButton("Send selected to Review")
        btn_send.setObjectName("PrimaryButton")
        btn_send.setToolTip(
            "Forward the selected residual leaks to the Review view as new "
            "T3_verifier candidates so you can edit a placeholder, promote "
            "them and re-run apply."
        )
        btn_send.clicked.connect(self._send_selected)

        btn_send_all = QPushButton("Send all to Review")
        btn_send_all.setToolTip(
            "Send EVERY residual hit currently shown to the Review view."
        )
        btn_send_all.clicked.connect(self._send_all)

        top = QHBoxLayout()
        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.search, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(self.lbl)
        bottom.addStretch()
        bottom.addWidget(btn_send)
        bottom.addWidget(btn_send_all)
        bottom.addWidget(btn_open)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.addLayout(top)
        root.addWidget(self.table, 1)
        root.addLayout(bottom)

        state.verifier_changed.connect(self._on_changed)

    def _on_changed(self, report) -> None:
        self.table.setRowCount(0)
        self._row_hits = []
        if report is None:
            self._summary_base = "Verifier not run yet"
            self.lbl.setText(self._summary_base)
            return
        for h in report.hits:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(h.file))
            self.table.setItem(r, 1, QTableWidgetItem(h.pattern))
            self.table.setItem(r, 2, QTableWidgetItem(h.match))
            self.table.setItem(r, 3, QTableWidgetItem(h.snippet))
            self._row_hits.append(h)
        self._summary_base = (
            f"{len(report.hits)} residual leaks · {report.files_scanned} files "
            f"({report.pdfs_scanned} PDFs)" + (" · CLEAN" if report.is_clean else "")
        )
        self._apply_filter()

    def _on_selection_changed(self) -> None:
        n = len({i.row() for i in self.table.selectedIndexes()})
        if n <= 0:
            self.lbl.setText(self._summary_base)
        else:
            self.lbl.setText(f"{self._summary_base} · {n} row(s) selected")

    def _apply_filter(self) -> None:
        q = self.search.text().lower().strip()
        for r in range(self.table.rowCount()):
            row_text = " ".join(
                (self.table.item(r, c).text() if self.table.item(r, c) else "")
                for c in range(self.table.columnCount())
            ).lower()
            self.table.setRowHidden(r, q not in row_text if q else False)
        self._on_selection_changed()

    def _on_double(self, row, col) -> None:
        self.open_in_diff_requested.emit(self.table.item(row, 0).text())

    def _open_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        self.open_in_diff_requested.emit(self.table.item(rows[0], 0).text())

    def _send_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            Toaster.notify(
                "No rows selected",
                "Select one or more rows in the table, "
                "or use 'Send all to Review'.",
                kind="warn",
            )
            return
        hits = [self._row_hits[r] for r in rows if 0 <= r < len(self._row_hits)]
        if hits:
            self.send_to_review_requested.emit(hits)

    def _send_all(self) -> None:
        visible = [
            self._row_hits[r]
            for r in range(self.table.rowCount())
            if not self.table.isRowHidden(r) and 0 <= r < len(self._row_hits)
        ]
        if not visible:
            Toaster.notify(
                "Nothing to send",
                "No rows match the current filter.",
                kind="warn",
            )
            return
        self.send_to_review_requested.emit(visible)


__all__ = ["VerifierView"]
