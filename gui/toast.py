"""Frameless top-right toast notifications.

Use :func:`Toaster.notify` from anywhere in the GUI; toasts auto-stack and
fade out. The class is a singleton attached to the main window so even
modal dialogs can emit non-blocking notifications.

The pipeline-tab feeds a different sink than the toast layer: a call
to ``Toaster.notify(pipeline_event=True)`` routes the message to the
``PipelineView``'s in-layout activity feed (registered via
:meth:`Toaster.set_pipeline_sink`) instead of stacking yet another
floating toast on the right edge. This keeps Run-all from drowning
the main UI in 8-10 simultaneous top-right toasts.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import icon
from .theme import PALETTE


_KIND_TO_FRAME = {
    "info": "ToastInfo",
    "ok": "ToastOk",
    "warn": "ToastWarn",
    "err": "ToastErr",
}


class _Toast(QFrame):
    def __init__(self, parent, *, title: str, message: str, kind: str = "info", duration_ms: int = 4500) -> None:
        # Top-level tooltip-style window so the toast can sit on top of
        # the main view without being clipped by its central widget,
        # but with explicit Qt.Tool + FramelessWindowHint so it does NOT
        # show up in the taskbar and never steals focus. Without this
        # the toast was a normal child widget of QMainWindow whose
        # right edge could escape the central widget on the Windows
        # window-decoration / DWM combination, producing a "popup
        # tagliato" effect on the right edge of the screen.
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setObjectName("Toast")
        self.setProperty("class", _KIND_TO_FRAME.get(kind, "ToastInfo"))
        self.setObjectName(_KIND_TO_FRAME.get(kind, "ToastInfo"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(320)
        self.setMaximumWidth(420)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("H3")
        title_lbl.setWordWrap(True)
        msg_lbl = QLabel(message)
        msg_lbl.setObjectName("Muted")
        msg_lbl.setWordWrap(True)

        close_btn = QToolButton()
        close_btn.setIcon(icon("x", size=14, color=PALETTE["text_dim"]))
        close_btn.setIconSize(QSize(14, 14))
        close_btn.setAutoRaise(True)
        close_btn.clicked.connect(self.dismiss)

        head = QHBoxLayout()
        head.addWidget(title_lbl, 1)
        head.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(4)
        lay.addLayout(head)
        if message:
            lay.addWidget(msg_lbl)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)

        self._fade_in = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_in.setDuration(180)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade_out = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_out.setDuration(220)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.finished.connect(self.deleteLater)

        QTimer.singleShot(0, self._fade_in.start)
        if duration_ms > 0:
            QTimer.singleShot(duration_ms, self.dismiss)

    def dismiss(self) -> None:
        try:
            self._fade_out.start()
        except Exception:
            self.deleteLater()


class Toaster:
    """Singleton toast manager attached to the main window."""

    _instance: Optional["Toaster"] = None
    # Hard cap on simultaneously-visible top-right toasts. Anything
    # beyond this gets routed to the oldest slot (the elder is
    # dismissed first) so the right edge never accumulates a stack
    # taller than the residuals banner / build banner below it.
    MAX_TOASTS = 3

    def __init__(self, host: QWidget) -> None:
        self.host = host
        self._toasts: list[_Toast] = []
        # Optional sink for pipeline-flavoured events. Wired by
        # PipelineView at construction so Run-all chains land in the
        # in-layout activity feed instead of stacking floating toasts.
        self._pipeline_sink: Optional[
            Callable[[str, str, str], None]
        ] = None

    @classmethod
    def attach(cls, host: QWidget) -> "Toaster":
        cls._instance = Toaster(host)
        return cls._instance

    @classmethod
    def get(cls) -> Optional["Toaster"]:
        return cls._instance

    @classmethod
    def set_pipeline_sink(
        cls, sink: Optional[Callable[[str, str, str], None]]
    ) -> None:
        """Register a callback for ``notify(pipeline_event=True)``.

        The callback receives ``(title, message, kind)`` and is
        expected to render the event in the Pipeline view's activity
        feed. Pass ``None`` to clear (used when the host widget is
        destroyed).
        """
        inst = cls.get()
        if inst is None:
            return
        inst._pipeline_sink = sink

    @classmethod
    def notify(
        cls,
        title: str,
        message: str = "",
        *,
        kind: str = "info",
        duration_ms: int = 4500,
        pipeline_event: bool = False,
    ) -> None:
        inst = cls.get()
        if inst is None:
            return
        if pipeline_event and inst._pipeline_sink is not None:
            try:
                inst._pipeline_sink(title, message, kind)
                return
            except Exception:
                # Sink failed — fall back to a floating toast so the
                # operator never loses a notification entirely.
                pass
        inst._show(title, message, kind=kind, duration_ms=duration_ms)

    def _show(self, title: str, message: str, *, kind: str = "info", duration_ms: int = 4500) -> None:
        # Cap the simultaneous toast count: when the queue is already
        # at MAX_TOASTS, dismiss the oldest one so the newcomer can
        # take its slot. Without this, a Run-all burst leaves a stack
        # of 8-10 toasts in the top-right covering the residuals and
        # build banners.
        while len(self._toasts) >= self.MAX_TOASTS:
            oldest = self._toasts[0]
            try:
                oldest.dismiss()
            except Exception:
                pass
            # Guard against ``dismiss`` not removing the entry from
            # ``self._toasts`` synchronously (the fade-out animation
            # is what frees the slot via ``destroyed`` signal). If we
            # didn't pop here we would infinite-loop on the same item.
            if self._toasts and self._toasts[0] is oldest:
                self._toasts.pop(0)
        t = _Toast(self.host, title=title, message=message, kind=kind, duration_ms=duration_ms)
        self._toasts.append(t)
        t.destroyed.connect(lambda *_: self._toasts.remove(t) if t in self._toasts else None)
        self._reposition()
        t.show()
        QTimer.singleShot(50, self._reposition)

    def _reposition(self) -> None:
        """Pin every live toast to the top-right of the host window
        in screen coordinates.

        Toasts are top-level ``Qt.Tool`` windows so ``setGeometry`` is
        evaluated in screen space, not widget space. We map the host
        window's top-right corner to global coords, anchor each toast
        ``margin`` pixels in from the right edge, and clamp the toast
        width so it never overflows past the host's left margin (a
        very narrow window would otherwise push the toast off-screen).
        """
        if not self.host:
            return
        try:
            if not self.host.isVisible():
                return
        except Exception:
            return
        host_rect = self.host.rect()
        margin = 16
        spacing = 8
        try:
            top_right_global = self.host.mapToGlobal(
                QPoint(host_rect.right(), host_rect.top())
            )
            top_left_global = self.host.mapToGlobal(
                QPoint(host_rect.left(), host_rect.top())
            )
        except Exception:
            return
        x_anchor = top_right_global.x() - margin
        max_w = max(0, x_anchor - (top_left_global.x() + margin))
        y = top_right_global.y() + margin + 60  # below status bar / menu
        for t in self._toasts:
            if not t.isVisible():
                t.adjustSize()
            sz = t.sizeHint()
            w = min(sz.width(), max_w) if max_w > 0 else sz.width()
            x = x_anchor - w
            t.setGeometry(x, y, w, sz.height())
            y += sz.height() + spacing


__all__ = ["Toaster"]
