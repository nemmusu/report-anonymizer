"""About dialog with logo, version, third-party licenses."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .icons import app_icon


VERSION = "1.0.0"

THIRD_PARTY = """\
Report Anonymizer is licensed under the GNU GPL v3.0.
See the LICENSE file for the full text.

Third-party components:

PySide6 (LGPL) · Qt for Python
llama.cpp (MIT) · LLM inference
Hugging Face Hub (Apache 2.0) · model downloads
PyMuPDF (GNU AGPL) · PDF in-place edit
python-docx, openpyxl, python-pptx, odfpy · Office formats
Pandoc, WeasyPrint · document rendering
charset-normalizer, html2text, beautifulsoup4, jinja2, pyyaml, requests
psutil · hardware detection
hypothesis, pytest, pytest-qt · testing
"""


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Report Anonymizer")
        self.setModal(True)
        self.resize(560, 460)

        logo = QLabel()
        logo.setPixmap(app_icon(96).pixmap(96, 96))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Report Anonymizer")
        title.setObjectName("H1")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ver = QLabel(f"version {VERSION}")
        ver.setObjectName("Muted")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)

        desc = QLabel(
            "Local LLM-assisted anonymizer for penetration-test reports. "
            "Everything stays on your machine; the only network call is the "
            "optional model download from Hugging Face."
        )
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        licenses = QTextBrowser()
        licenses.setOpenExternalLinks(True)
        licenses.setPlainText(THIRD_PARTY)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        head = QVBoxLayout()
        head.addWidget(logo)
        head.addWidget(title)
        head.addWidget(ver)
        head.addSpacing(8)
        head.addWidget(desc)

        foot = QHBoxLayout()
        foot.addStretch()
        foot.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Third-party software"))
        lay.addWidget(licenses, 1)
        lay.addLayout(foot)


__all__ = ["AboutDialog", "VERSION"]
