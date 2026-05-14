"""Pre-scan preview: extension breakdown + tree with include/exclude.

Run before the actual pipeline so the operator can prune obviously unwanted
folders/files (logs, cache, etc.) without surprises.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from anonymize.scanner import scan_path

from .file_tree_view import FileTreeView


def _human_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


class ScanPreviewDialog(QDialog):
    def __init__(
        self,
        root: Path,
        *,
        respect_gitignore: bool = True,
        max_file_size_mb: int = 50,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Scan preview · {root}")
        self.resize(1024, 660)
        self.root = root
        self._excluded: set[Path] = set()

        scan = scan_path(
            root,
            respect_gitignore=respect_gitignore,
            max_file_size_mb=max_file_size_mb,
        )

        # ---- breakdown table ----
        self.breakdown = QTableWidget(0, 3)
        self.breakdown.setHorizontalHeaderLabels(["Extension", "Files", "Total size"])
        self.breakdown.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for ext, (count, size) in sorted(
            scan.breakdown_by_ext().items(), key=lambda x: -x[1][1]
        ):
            r = self.breakdown.rowCount()
            self.breakdown.insertRow(r)
            self.breakdown.setItem(r, 0, QTableWidgetItem(ext))
            self.breakdown.setItem(r, 1, QTableWidgetItem(str(count)))
            self.breakdown.setItem(r, 2, QTableWidgetItem(_human_size(size)))

        # ---- tree ----
        self.tree = FileTreeView(root, scan)
        self.tree.exclusions_changed.connect(self._on_exclusions_changed)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.breakdown)
        split.addWidget(self.tree)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)

        head = QLabel(
            f"Found {len(scan.files)} files in {root}. "
            f"Skipped folders: {len(scan.skipped_dirs)}."
        )
        head.setObjectName("Muted")

        ok = QPushButton("Continue with selection")
        ok.setObjectName("PrimaryButton")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(cancel)
        bottom.addWidget(ok)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

    def _on_exclusions_changed(self, paths: list) -> None:
        self._excluded = {Path(p) for p in paths}

    def excluded_paths(self) -> list[Path]:
        return sorted(self._excluded)


__all__ = ["ScanPreviewDialog"]
