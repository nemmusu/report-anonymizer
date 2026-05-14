"""Keyboard shortcuts cheat-sheet overlay (press F1 / ?)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)


SHORTCUTS = (
    ("Ctrl+O", "Open file"),
    ("Ctrl+Shift+O", "Open multiple files"),
    ("Ctrl+B", "Open dossier folder"),
    ("Ctrl+,", "Settings"),
    ("Ctrl+R", "Run all stages"),
    ("Esc", "Stop current stage"),
    ("F1 / ?", "Show this help"),
    ("Ctrl+Q", "Quit"),
)


class ShortcutsOverlay(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        self.resize(440, 380)
        title = QLabel("Keyboard shortcuts")
        title.setObjectName("H1")

        rows = QVBoxLayout()
        rows.setSpacing(6)
        for shortcut, desc in SHORTCUTS:
            row = QHBoxLayout()
            kb = QLabel(shortcut)
            kb.setObjectName("Badge")
            kb.setFixedWidth(120)
            kb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(kb)
            row.addSpacing(12)
            row.addWidget(QLabel(desc), 1)
            rows.addLayout(row)

        card = QFrame()
        card.setObjectName("Card")
        cardlay = QVBoxLayout(card)
        cardlay.setContentsMargins(20, 16, 20, 16)
        cardlay.addWidget(title)
        cardlay.addSpacing(8)
        cardlay.addLayout(rows)
        cardlay.addStretch()

        lay = QVBoxLayout(self)
        lay.addWidget(card)


__all__ = ["ShortcutsOverlay"]
