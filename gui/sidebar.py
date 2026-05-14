"""Collapsible icon-only / icon+label sidebar.

Replaces the QTabWidget in the main window. The sidebar emits ``view_changed``
with the destination view key whenever the user clicks one of the buttons.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import icon
from .theme import PALETTE


_DEFAULT_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("pipeline", "Pipeline", "play"),
    ("review", "Review", "list"),
    ("diff", "Diff", "diff"),
    ("server", "Server", "server"),
)


class Sidebar(QFrame):
    view_changed = Signal(str)
    toggled_collapsed = Signal(bool)
    # Emitted when the operator clicks a per-row status indicator
    # (e.g. the "connect to server" dot next to the Server entry).
    # Carries the row's key so the MainWindow can route the action.
    indicator_clicked = Signal(str)

    EXPANDED_WIDTH = 220
    COLLAPSED_WIDTH = 56

    # Indicator colour states reused by ``set_indicator``. Keeps the
    # palette in one place so the dot looks coherent with the other
    # "led" widgets in the app.
    _INDICATOR_COLORS = {
        "off": PALETTE["err"],          # red — server offline
        "starting": PALETTE["warn"],    # amber — boot in progress
        "on": PALETTE["ok"],            # green — health endpoint up
        "warn": PALETTE["warn"],
    }

    def __init__(self, *, views: tuple[tuple[str, str, str], ...] = _DEFAULT_VIEWS) -> None:
        super().__init__()
        self.setObjectName("Sidebar")
        self._collapsed = False
        self._buttons: dict[str, QToolButton] = {}
        self._labels: dict[str, str] = {key: label for key, label, _ in views}
        self._indicators: dict[str, QToolButton] = {}

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        head = QLabel("VIEWS")
        head.setObjectName("SidebarHeader")
        head.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._head_label = head

        toggle = QToolButton()
        toggle.setIcon(icon("menu", color=PALETTE["text_dim"]))
        toggle.setIconSize(QSize(20, 20))
        toggle.setAutoRaise(True)
        toggle.clicked.connect(self.toggle_collapsed)
        self._toggle_btn = toggle

        head_lay = QHBoxLayout()
        head_lay.setContentsMargins(8, 6, 6, 6)
        head_lay.addWidget(head, 1)
        head_lay.addWidget(toggle)

        body = QVBoxLayout()
        body.setContentsMargins(6, 6, 6, 6)
        body.setSpacing(2)
        for key, label, ic in views:
            b = QToolButton()
            b.setObjectName("SidebarButton")
            b.setCheckable(True)
            b.setIcon(icon(ic, color=PALETTE["text_dim"]))
            b.setIconSize(QSize(18, 18))
            b.setText(f"  {label}")
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setToolTip(label)
            b.clicked.connect(lambda _checked=False, k=key: self._on_clicked(k))
            self._buttons[key] = b
            self._group.addButton(b)

            # Per-row status indicator: a small clickable circle
            # rendered as a QToolButton. Hidden by default, callers
            # opt in via ``set_indicator(key, state, ...)``. Painted
            # via stylesheet so it inherits the palette and stays
            # crisp at any DPI without bundling extra SVGs.
            ind = QToolButton(self)
            ind.setFixedSize(14, 14)
            ind.setAutoRaise(True)
            ind.setCursor(Qt.CursorShape.PointingHandCursor)
            ind.setVisible(False)
            ind.clicked.connect(
                lambda _checked=False, k=key: self.indicator_clicked.emit(k)
            )
            self._indicators[key] = ind

            row_widget = QWidget(self)
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 6, 0)
            row.setSpacing(0)
            row.addWidget(b, 1)
            row.addWidget(ind, 0, Qt.AlignmentFlag.AlignVCenter)
            body.addWidget(row_widget)
        body.addStretch()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(head_lay)
        lay.addLayout(body)

        self.setFixedWidth(self.EXPANDED_WIDTH)
        if self._buttons:
            first = next(iter(self._buttons.values()))
            first.setChecked(True)

    def select(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)
            self.view_changed.emit(key)

    def set_indicator(
        self,
        key: str,
        state: Optional[str],
        *,
        tooltip: str = "",
    ) -> None:
        """Show / hide the per-row status indicator for ``key``.

        ``state`` is one of ``"off"`` (red), ``"starting"`` /
        ``"warn"`` (amber), ``"on"`` (green); pass ``None`` to hide
        the dot entirely.
        """
        ind = self._indicators.get(key)
        if ind is None:
            return
        if state is None:
            ind.setVisible(False)
            return
        color = self._INDICATOR_COLORS.get(state, PALETTE["text_dim"])
        # Style as a flat coloured dot. The half-radius (= width/2)
        # makes a perfect circle at any DPI.
        ind.setStyleSheet(
            "QToolButton {"
            f" background: {color}; "
            f" border: 1px solid {PALETTE['border']}; "
            " border-radius: 7px; padding: 0;"
            "} "
            "QToolButton:hover {"
            f" border-color: {PALETTE['accent_glow']}; "
            "}"
        )
        if tooltip:
            ind.setToolTip(tooltip)
        ind.setVisible(True)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.setFixedWidth(self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH)
        self._head_label.setVisible(not collapsed)
        for key, b in self._buttons.items():
            if collapsed:
                b.setText("")
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            else:
                b.setText(f"  {self._labels[key]}")
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # The indicator stays visible in both states so the operator
        # can always see + click it; in collapsed mode it sits to
        # the right of the icon, in expanded mode on the row's edge.
        self.toggled_collapsed.emit(collapsed)

    def _on_clicked(self, key: str) -> None:
        self.view_changed.emit(key)


__all__ = ["Sidebar"]
