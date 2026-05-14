"""System tray integration with native notifications."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .icons import app_icon


class Tray(QSystemTrayIcon):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(app_icon(32), parent)
        self._parent = parent
        self.setToolTip("document-anonymizer-production")

        self.menu = QMenu(parent)
        self.act_show = QAction("Show window", parent)
        self.act_quit = QAction("Quit", parent)
        self.menu.addAction(self.act_show)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)
        self.setContextMenu(self.menu)

        self.act_show.triggered.connect(self._show_main)
        self.activated.connect(self._on_activated)

    def _show_main(self) -> None:
        if self._parent:
            self._parent.show()
            self._parent.raise_()
            self._parent.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_main()

    def notify(
        self,
        title: str,
        message: str = "",
        *,
        kind: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
        duration_ms: int = 4500,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        if on_quit is not None:
            try:
                self.act_quit.triggered.disconnect()
            except Exception:
                pass
            self.act_quit.triggered.connect(on_quit)
        try:
            self.showMessage(title, message, kind, duration_ms)
        except Exception:
            pass


__all__ = ["Tray"]
