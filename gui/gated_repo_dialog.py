"""Dialog shown when HF returns 401/403 for a gated model repo."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from anonymize.hf_models import HF_TOKEN_PATH, save_hf_token


class GatedRepoDialog(QDialog):
    accepted_token = Signal(str)

    def __init__(self, repo_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Accept license · {repo_id}")
        self.setModal(True)
        self.resize(520, 320)
        self.repo_id = repo_id

        title = QLabel(f"<b>{repo_id}</b> requires a Hugging Face access token.")
        title.setObjectName("H2")
        title.setWordWrap(True)

        steps = QLabel(
            "1) Open the model page on Hugging Face<br>"
            "2) Accept the license<br>"
            "3) Create a token at huggingface.co/settings/tokens (read scope)<br>"
            "4) Paste it below"
        )
        steps.setWordWrap(True)
        steps.setObjectName("Muted")

        open_btn = QPushButton("Open model page")
        open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(f"https://huggingface.co/{repo_id}"))
        )
        token_btn = QPushButton("Open tokens page")
        token_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://huggingface.co/settings/tokens"))
        )

        self.token = QLineEdit()
        self.token.setPlaceholderText("hf_...")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)

        self.save_token = QCheckBox(f"Save token to {HF_TOKEN_PATH} (chmod 600)")
        self.save_token.setChecked(True)

        ok = QPushButton("Continue")
        ok.setObjectName("PrimaryButton")
        ok.clicked.connect(self._accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        btns = QHBoxLayout()
        btns.addWidget(open_btn)
        btns.addWidget(token_btn)
        btns.addStretch()

        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(ok)

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(steps)
        lay.addLayout(btns)
        lay.addSpacing(8)
        lay.addWidget(QLabel("Access token:"))
        lay.addWidget(self.token)
        lay.addWidget(self.save_token)
        lay.addStretch()
        lay.addLayout(actions)

    def _accept(self) -> None:
        tok = self.token.text().strip()
        if not tok:
            return
        if self.save_token.isChecked():
            try:
                save_hf_token(tok)
            except Exception:
                pass
        self.accepted_token.emit(tok)
        self.accept()


__all__ = ["GatedRepoDialog"]
