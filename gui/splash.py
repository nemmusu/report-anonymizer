"""Splash screen shown during app boot."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QSplashScreen

from .icons import splash_pixmap


class Splash(QSplashScreen):
    def __init__(self) -> None:
        super().__init__(splash_pixmap(), Qt.WindowType.SplashScreen)
        self.setFixedSize(480, 280)

    def update_message(self, msg: str) -> None:
        self.showMessage(
            msg,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
            QColor("#9aa0a6"),
        )


__all__ = ["Splash"]
