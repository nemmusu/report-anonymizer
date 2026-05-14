"""Compact status-bar widget for the local llama-server."""
from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from .icons import icon
from .state import AppState
from .theme import PALETTE


class ServerStatusWidget(QWidget):
    """LED + preset name + Open Server panel button."""

    request_open_panel = Signal()
    request_settings = Signal()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.led = QLabel("●")
        self.led.setStyleSheet(f"color: {PALETTE['err']}; font-size: 14px;")
        self.label = QLabel("llama-server: offline")
        self.label.setObjectName("Muted")
        self.preset_label = QLabel("")
        self.preset_label.setObjectName("BadgeMuted")

        self.open_btn = QToolButton()
        self.open_btn.setIcon(icon("server", color=PALETTE["text_dim"]))
        self.open_btn.setIconSize(QSize(14, 14))
        self.open_btn.setToolTip("Open Server panel")
        self.open_btn.setAutoRaise(True)
        self.open_btn.clicked.connect(self.request_open_panel.emit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.led)
        lay.addWidget(self.label)
        lay.addWidget(self.preset_label)
        lay.addWidget(self.open_btn)

        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        # React the moment ServerPanel kicks off / finishes a Start so
        # the small status widget flips to "starting…" without waiting
        # the full 3 s poll interval.
        try:
            self.state.server_starting_changed.connect(lambda _v: self._poll())
        except Exception:
            pass
        self._poll()

    def _poll(self) -> None:
        # Non-blocking probe: see ServerManager.health_nowait. Keeps
        # the UI thread free even when the server is offline.
        ok = self.state.server.health_nowait(timeout=1.0)
        self._set(ok)

    def _set(self, ok: bool) -> None:
        prof = self.state.server.profile
        # Mirror ServerPanel's behaviour: while the Start worker is
        # still bringing the binary up, render "starting…" + a yellow
        # LED instead of bouncing through "offline" while the health
        # endpoint warms up.
        if not ok and self.state.server_starting:
            self.led.setStyleSheet(f"color: {PALETTE['warn']}; font-size: 14px;")
            self.label.setText("llama-server: starting…")
        elif ok:
            self.led.setStyleSheet(f"color: {PALETTE['ok']}; font-size: 14px;")
            self.label.setText(
                f"llama-server: online · {prof.host}:{prof.port}"
            )
        else:
            self.led.setStyleSheet(f"color: {PALETTE['err']}; font-size: 14px;")
            self.label.setText("llama-server: offline")
        self.preset_label.setText(f" {prof.name} ")
        self.state.server_status_changed.emit(ok, self.label.text())


__all__ = ["ServerStatusWidget"]
