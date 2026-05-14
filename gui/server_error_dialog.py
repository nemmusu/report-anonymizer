"""Dialog displaying a llama-server diagnosis with actionable buttons."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from anonymize.server_doctor import Diagnosis


_ACTION_LABELS = {
    "switch_preset:cpu_only": "Switch to CPU-only preset",
    "switch_preset:default": "Switch to default preset",
    "open_preset_editor": "Open preset editor",
    "reduce_ngl": "Reduce GPU layers",
    "redownload_model": "Re-download model",
    "open_model_manager": "Open Model Manager",
    "browse_binary": "Browse for llama-server binary",
    "install_llama_cpp": "How to install llama.cpp",
    "free_port": "Free the port",
    "change_port": "Change port",
    "update_llama_cpp": "Update llama.cpp",
    "remove_mmproj": "Remove mmproj override",
    "open_log": "Open full log",
}


class ServerErrorDialog(QDialog):
    action_requested = Signal(str)

    def __init__(self, diag: Diagnosis, *, log_tail: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Server failure")
        self.setModal(True)
        self.resize(560, 460)

        title = QLabel(f"<b>{diag.cause}</b>")
        title.setObjectName("H2")
        msg = QLabel(diag.message)
        msg.setWordWrap(True)
        msg.setObjectName("Muted")

        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setPlainText(log_tail or "(no log captured)")
        log.setMaximumHeight(220)

        actions_lbl = QLabel("Suggested actions")
        actions_lbl.setObjectName("Caption")

        actions_box = QVBoxLayout()
        for code in diag.suggested_actions or ["open_log"]:
            btn = QPushButton(_ACTION_LABELS.get(code, code))
            btn.clicked.connect(lambda _=False, c=code: self._fire(c))
            actions_box.addWidget(btn)
        actions_box.addStretch()

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(msg)
        lay.addSpacing(6)
        lay.addWidget(actions_lbl)
        lay.addLayout(actions_box)
        lay.addSpacing(6)
        lay.addWidget(QLabel("Log tail"))
        lay.addWidget(log, 1)
        lay.addLayout(bottom)

    def _fire(self, code: str) -> None:
        self.action_requested.emit(code)


__all__ = ["ServerErrorDialog"]
