"""Reusable empty state widget."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .icons import empty_state_pixmap


class EmptyState(QWidget):
    cta_clicked = Signal()

    def __init__(
        self,
        *,
        kind: str = "info",
        title: str = "Nothing here yet",
        subtitle: str = "",
        cta_label: Optional[str] = None,
    ) -> None:
        super().__init__()
        pix = empty_state_pixmap(kind)
        img = QLabel()
        img.setPixmap(pix)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("H2")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("Muted")
        sub_lbl.setWordWrap(True)
        sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cta = QPushButton(cta_label) if cta_label else None
        if cta:
            cta.setObjectName("PrimaryButton")
            cta.clicked.connect(self.cta_clicked.emit)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)
        lay.addStretch()
        lay.addWidget(img)
        lay.addSpacing(12)
        lay.addWidget(title_lbl)
        if subtitle:
            lay.addWidget(sub_lbl)
        if cta:
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(cta)
            row.addStretch()
            lay.addSpacing(12)
            lay.addLayout(row)
        lay.addStretch()


__all__ = ["EmptyState"]
