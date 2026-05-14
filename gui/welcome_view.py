"""Welcome / drop-zone shown at startup before any project is open."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .icons import welcome_hero_pixmap


class WelcomeView(QScrollArea):
    """Drop-zone + 2 big buttons: Open file / Open multiple files.

    Wrapped in a QScrollArea so the welcome content stays reachable on
    small windows; the hero illustration also scales down with the viewport.
    """

    open_paths = Signal(list)  # list[Path]

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        inner = QWidget()
        inner.setObjectName("WelcomeInner")
        self.setWidget(inner)

        hero = QLabel()
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero.setMinimumHeight(140)
        hero.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._hero = hero
        self._update_hero(560, 200)

        title = QLabel("Anonymize a file or a dossier")
        title.setObjectName("H1")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sub = QLabel(
            "Drop a file, multiple files, or a folder here.<br>"
            "<span style='color:#9aa0a6'>"
            "Supported: .md / .docx / .doc / .xlsx / .pptx / .odt / .rtf / .pdf / "
            ".html / code / dossier folders"
            "</span>"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setTextFormat(Qt.TextFormat.RichText)
        sub.setWordWrap(True)

        drop = QFrame()
        drop.setObjectName("Card")
        drop.setMinimumHeight(120)
        drop.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        drop_l = QVBoxLayout(drop)
        drop_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_label = QLabel("Drop file or folder here")
        drop_label.setObjectName("H2")
        drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_l.addWidget(drop_label)

        btn_file = QPushButton("Open file…")
        btn_files = QPushButton("Open multiple files…")
        for b in (btn_file, btn_files):
            b.setMinimumHeight(34)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn_file.setObjectName("PrimaryButton")
        btn_file.clicked.connect(self._open_file)
        btn_files.clicked.connect(self._open_files)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addWidget(btn_file)
        btn_row.addWidget(btn_files)

        root = QVBoxLayout(inner)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        root.addWidget(hero)
        root.addWidget(title)
        root.addWidget(sub)
        root.addSpacing(8)
        root.addWidget(drop, 1)
        root.addLayout(btn_row)

    def _update_hero(self, w: int, h: int) -> None:
        try:
            self._hero.setPixmap(welcome_hero_pixmap(max(280, w), max(120, h)))
        except Exception:
            pass

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        avail_w = max(280, self.viewport().width() - 56)
        target_w = min(820, avail_w)
        # Hero SVG aspect is 1024:480 (~21:10); keep the rendered
        # pixmap at that exact ratio so the title + mockups + arrow
        # fill the rectangle without leaving an empty band.
        target_h = int(round(target_w * 480 / 1024))
        self._update_hero(target_w, target_h)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open file")
        if path:
            self.open_paths.emit([Path(path)])

    def _open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open multiple files")
        if paths:
            self.open_paths.emit([Path(p) for p in paths])

    # Folder import was retired: drag-drop a folder still works (the
    # OS hands the directory path to ``dropEvent``) but the explicit
    # button has been removed because the simplified pipeline UI
    # handles folders the same way it handles single files.

    # ---- drag & drop --------------------------------------------------------

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        urls = e.mimeData().urls()
        paths: list[Path] = []
        for u in urls:
            p = Path(u.toLocalFile())
            if p.exists():
                paths.append(p)
        if not paths:
            return
        e.acceptProposedAction()
        # Open the import dialog from the next event-loop tick rather
        # than synchronously inside this drop callback, Qt is still
        # processing the drag-and-drop walk and opening a modal dialog
        # in the middle of it can SIGSEGV (observed when the user
        # dropped multiple files at once).
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda ps=paths: self.open_paths.emit(ps))


__all__ = ["WelcomeView"]
