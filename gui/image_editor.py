"""Flameshot / Greenshot-style image editor.

Two flavours:

* :class:`ImageEditorWidget` — the embeddable canvas + toolbar +
  side-panel-of-rects, no Save/Cancel buttons. The Image review
  tab uses this directly; navigation (next / previous image, save
  decisions) lives one level up in :class:`ImageReviewPanel`.
* :class:`ImageEditorDialog` — a thin modal wrapper around the
  widget, kept for any flow that still wants a one-shot dialog
  (tests, scripts, future "edit this single image" affordances).

The widget operates entirely in pixel space (the same coordinate
system the on-disk YAML uses), so the rect list it returns can be
saved verbatim.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSpinBox,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anonymize.image_inventory import RedactionRect

from .icons import icon
from .theme import PALETTE


# Colour-coded translucent fills per tool. Helps the operator scan
# the canvas and tell at a glance which redaction is which kind.
_TOOL_COLOURS: dict[str, QColor] = {
    "blackout": QColor(220, 53, 69, 128),       # red, semi-transparent
    "blur": QColor(31, 110, 200, 110),          # blue
    "pixelate": QColor(40, 180, 90, 110),       # green
    "text_overlay": QColor(245, 166, 35, 130),  # amber
}

_TOOL_BORDERS: dict[str, QColor] = {
    "blackout": QColor(220, 53, 69, 220),
    "blur": QColor(31, 110, 200, 220),
    "pixelate": QColor(40, 180, 90, 220),
    "text_overlay": QColor(245, 166, 35, 240),
}

_TOOL_LABELS: dict[str, str] = {
    "blackout": "Blackout",
    "blur": "Blur",
    "pixelate": "Pixelate",
    "text_overlay": "Text overlay",
}

_TOOL_ICONS: dict[str, str] = {
    "blackout": "blackout",
    "blur": "blur",
    "pixelate": "pixelate",
    "text_overlay": "text-overlay",
}


# --------------------------------------------------------------------
# Custom QGraphicsRectItem subclass: carries tool metadata, draws a
# distinctive translucent fill plus solid border, and is movable +
# selectable when the [select] tool is active.
# --------------------------------------------------------------------

class RedactionRectItem(QGraphicsRectItem):
    """A redaction rect on the editor canvas."""

    def __init__(
        self,
        rect: QRectF,
        *,
        tool: str = "blackout",
        intensity: Optional[int] = None,
        text: Optional[str] = None,
        font_size: Optional[int] = None,
        fg: Optional[str] = None,
        bg: Optional[str] = None,
    ) -> None:
        super().__init__(rect)
        self.tool: str = tool
        self.intensity: Optional[int] = intensity
        self.text: Optional[str] = text
        self.font_size: Optional[int] = font_size
        self.fg: Optional[str] = fg
        self.bg: Optional[str] = bg
        self._apply_style()
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._apply_style()
        self.update()

    def _apply_style(self) -> None:
        # The canvas now shows the BAKED image (with redactions
        # rendered in real pixels) so the rect overlay must NOT
        # cover the rendered result with a translucent colour.
        # We keep a thin coloured border so the operator can still
        # see, click, and drag the rect handles without obscuring
        # the actual pixels.
        border = _TOOL_BORDERS.get(self.tool, QColor(120, 120, 120, 220))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        pen = QPen(border)
        pen.setWidthF(1.4)
        pen.setCosmetic(True)        # constant width regardless of zoom
        self.setPen(pen)

    def to_redaction(self) -> RedactionRect:
        r = self.rect().normalized().translated(self.pos())
        return RedactionRect(
            x=int(round(r.x())),
            y=int(round(r.y())),
            w=max(1, int(round(r.width()))),
            h=max(1, int(round(r.height()))),
            tool=self.tool,
            intensity=self.intensity,
            text=self.text,
            font_size=self.font_size,
            fg=self.fg,
            bg=self.bg,
        )

    @classmethod
    def from_redaction(cls, r: RedactionRect) -> "RedactionRectItem":
        return cls(
            QRectF(r.x, r.y, r.w, r.h),
            tool=r.tool,
            intensity=r.intensity,
            text=r.text,
            font_size=r.font_size,
            fg=r.fg,
            bg=r.bg,
        )


# --------------------------------------------------------------------
# QUndoCommand subclasses. Strictly local to the parent widget: the
# QUndoStack lives on :class:`ImageEditorWidget` and is reset every
# time a different image is loaded.
# --------------------------------------------------------------------

class _AddRedactionCommand(QUndoCommand):
    def __init__(self, scene: QGraphicsScene, item: RedactionRectItem) -> None:
        super().__init__("Add redaction")
        self._scene = scene
        self._item = item

    def redo(self) -> None:
        self._scene.addItem(self._item)

    def undo(self) -> None:
        self._scene.removeItem(self._item)


class _DeleteRedactionCommand(QUndoCommand):
    def __init__(self, scene: QGraphicsScene, item: RedactionRectItem) -> None:
        super().__init__("Delete redaction")
        self._scene = scene
        self._item = item

    def redo(self) -> None:
        self._scene.removeItem(self._item)

    def undo(self) -> None:
        self._scene.addItem(self._item)


class _MoveRedactionCommand(QUndoCommand):
    def __init__(
        self,
        item: RedactionRectItem,
        old_rect: QRectF,
        new_rect: QRectF,
    ) -> None:
        super().__init__("Move redaction")
        self._item = item
        self._old = QRectF(old_rect)
        self._new = QRectF(new_rect)

    def redo(self) -> None:
        self._item.setRect(self._new)
        self._item.setPos(0, 0)

    def undo(self) -> None:
        self._item.setRect(self._old)
        self._item.setPos(0, 0)


# --------------------------------------------------------------------
# Image canvas: a QGraphicsView with click-and-drag rectangle drawing
# when a redaction tool is active, plain selection / move when the
# [select] tool is active.
# --------------------------------------------------------------------

class _ImageCanvas(QGraphicsView):
    """The image canvas. Emits ``rect_drawn`` while the user paints
    a new rect and ``rect_remove_requested`` when the user picks
    Remove from the right-click context menu over a rect."""

    rect_drawn = Signal(QRectF)
    selection_changed = Signal()
    rect_geometry_committed = Signal(QGraphicsRectItem, QRectF, QRectF)
    # Emitted when the operator picks "Remove" from the right-click
    # context menu over an existing rect. Carries the rect item so
    # the parent widget can wrap the removal in a QUndoCommand.
    rect_remove_requested = Signal(QGraphicsRectItem)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        scene = QGraphicsScene(parent)
        super().__init__(scene, parent)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setBackgroundBrush(QBrush(QColor(40, 40, 45)))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._draw_mode: bool = False
        self._draw_origin: Optional[QPointF] = None
        self._draft_item: Optional[QGraphicsRectItem] = None
        self._image_size: QSize = QSize(0, 0)
        # Per-item old-rect cache, populated on mousePress when the
        # user starts a drag, used on mouseRelease to commit a
        # MoveRedactionCommand.
        self._drag_old: dict[int, QRectF] = {}
        # Track whether the operator has explicitly zoomed; if not,
        # subsequent resize events (window grow/shrink) keep the
        # image fitted. Once the operator zooms / scrolls, we leave
        # them alone.
        self._user_has_zoomed: bool = False

    def set_image(self, raw_bytes: bytes) -> QGraphicsPixmapItem:
        scene = self.scene()
        scene.clear()
        img = QImage.fromData(raw_bytes)
        pix = QPixmap.fromImage(img)
        item = QGraphicsPixmapItem(pix)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(-10)
        item.setFlags(QGraphicsItem.GraphicsItemFlag.ItemClipsToShape)
        scene.addItem(item)
        scene.setSceneRect(QRectF(0, 0, pix.width(), pix.height()))
        self._image_size = QSize(pix.width(), pix.height())
        # Reset the zoom transform so a stale scale from a previous
        # image doesn't carry over (different aspect ratios otherwise
        # leave the new pixmap squished or pushed off-screen). The
        # subsequent fit_to_view rebuilds the right transform.
        self.resetTransform()
        self._user_has_zoomed = False
        # Defer the fit until Qt has a real viewport size for us. On
        # the very first ``set_image`` call (right after construction)
        # the view has zero geometry and ``fitInView`` collapses to
        # the smallest possible scale, the bug that produced the
        # tiny-image-in-the-middle screenshot. Multiple passes:
        # explicit now (steady-state nav), 0ms (post-event-loop), and
        # 50ms (covers the splitter-resizes-mid-show race that
        # otherwise leaves wide screenshots half-rendered).
        self.fit_to_view()
        QTimer.singleShot(0, self.fit_to_view)
        QTimer.singleShot(50, self.fit_to_view)
        return item

    def fit_to_view(self) -> None:
        if self._image_size.isEmpty():
            return
        if self.viewport().width() < 16 or self.viewport().height() < 16:
            return
        self.fitInView(
            self.scene().sceneRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def reset_zoom_100(self) -> None:
        self._user_has_zoomed = True
        self.resetTransform()

    def zoom(self, factor: float) -> None:
        self._user_has_zoomed = True
        self.scale(factor, factor)

    def set_draw_mode(self, on: bool) -> None:
        self._draw_mode = on
        if on:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            for item in self.scene().items():
                if isinstance(item, RedactionRectItem):
                    item.setFlag(
                        QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False
                    )
                    item.setFlag(
                        QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False
                    )
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            for item in self.scene().items():
                if isinstance(item, RedactionRectItem):
                    item.setFlag(
                        QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
                    )
                    item.setFlag(
                        QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True
                    )

    # ---- mouse interaction ----

    def mousePressEvent(self, event):
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            origin = self.mapToScene(event.position().toPoint())
            self._draw_origin = origin
            self._draft_item = QGraphicsRectItem(QRectF(origin, origin))
            pen = QPen(QColor(255, 255, 255, 220))
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._draft_item.setPen(pen)
            self._draft_item.setBrush(QBrush(QColor(255, 255, 255, 60)))
            self._draft_item.setZValue(50)
            self.scene().addItem(self._draft_item)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_old.clear()
            for it in self.scene().selectedItems():
                if isinstance(it, RedactionRectItem):
                    self._drag_old[id(it)] = QRectF(it.rect())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._draw_mode and self._draft_item is not None and self._draw_origin is not None:
            cur = self.mapToScene(event.position().toPoint())
            r = QRectF(self._draw_origin, cur).normalized()
            self._draft_item.setRect(r)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._draw_mode and self._draft_item is not None and event.button() == Qt.MouseButton.LeftButton:
            r = self._draft_item.rect()
            self.scene().removeItem(self._draft_item)
            self._draft_item = None
            self._draw_origin = None
            if r.width() >= 4 and r.height() >= 4:
                bounds = self.scene().sceneRect()
                r = r.intersected(bounds)
                self.rect_drawn.emit(r)
            event.accept()
            return
        for it in self.scene().selectedItems():
            if isinstance(it, RedactionRectItem):
                old = self._drag_old.get(id(it))
                if old is None:
                    continue
                r_now = it.rect().translated(it.pos())
                if r_now != old:
                    self.rect_geometry_committed.emit(it, old, r_now)
        self._drag_old.clear()
        super().mouseReleaseEvent(event)
        self.selection_changed.emit()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            d = event.angleDelta().y()
            factor = 1.15 if d > 0 else (1 / 1.15)
            self._user_has_zoomed = True
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event):
        """Right-click on a rect → context menu with Remove.

        Walks the items at the click position and picks the first
        ``RedactionRectItem`` it finds; if no rect is under the
        cursor we fall through to the default behaviour so plain
        right-clicks on the image background still do nothing
        surprising.
        """
        scene_pos = self.mapToScene(event.pos())
        target: Optional[RedactionRectItem] = None
        for it in self.scene().items(scene_pos):
            if isinstance(it, RedactionRectItem):
                target = it
                break
        if target is None:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        act_remove = menu.addAction("Remove")
        chosen = menu.exec(event.globalPos())
        if chosen is act_remove:
            self.rect_remove_requested.emit(target)
        event.accept()

    def showEvent(self, event):
        # First show: the geometry is now real, fit again. Without
        # this the "tiny image in the corner" bug came back every
        # time the editor was opened cold.
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_to_view)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Track the viewport: as long as the user hasn't deliberately
        # zoomed, follow the new viewport size. This is what a
        # "preview" should do (think browser image viewer); once the
        # user zooms in / scrolls, we stay where they put us.
        if not self._user_has_zoomed:
            QTimer.singleShot(0, self.fit_to_view)


# --------------------------------------------------------------------
# Embeddable editor widget. Owns the toolbar, canvas, side rect-list,
# and the QUndoStack. Exposes ``load_image`` / ``current_rects``.
# --------------------------------------------------------------------

class ImageEditorWidget(QWidget):
    """The non-modal editor body. Embed this in any container.

    Lifecycle:

    1. Caller constructs the widget once and adds it to a layout.
    2. Caller invokes ``load_image(bytes, initial_rects, ...)`` to
       swap the current image. Every call resets the undo stack and
       starts from scratch on the new image.
    3. Caller invokes ``current_rects()`` whenever it needs the
       fresh rect list (for example before navigating to another
       image, so the in-memory decisions can be updated).
    """

    rects_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._undo = QUndoStack(self)
        self._image_id_short: str = ""
        self._occurrence_count: int = 1
        self._current_tool: str = "select"
        # Cache the original bytes + format hint so the live-bake
        # pass can ask :class:`ImageRedactor` to render the current
        # rects into actual pixels. Reset on every ``load_image``
        # call so the cache always matches the currently displayed
        # image.
        self._orig_bytes: bytes = b""
        self._orig_fmt: str = "png"

        # Debounced live-bake. Every rect change (added / moved /
        # resized / property edit) restarts this timer; when it
        # fires we re-render the canvas pixmap so the operator
        # sees the actual blackout / blur / pixelate / text overlay
        # in real pixels, not a translucent placeholder rectangle.
        self._bake_timer = QTimer(self)
        self._bake_timer.setSingleShot(True)
        self._bake_timer.setInterval(80)
        self._bake_timer.timeout.connect(self._render_preview_to_canvas)

        self._canvas = _ImageCanvas(self)
        self._side_list = QListWidget(self)
        self._side_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._side_list.itemSelectionChanged.connect(self._on_side_selection_changed)

        self._build_toolbar()
        self._build_layout()
        self._wire_signals()

    # ---- public API ----

    def load_image(
        self,
        image_bytes: bytes,
        initial_rects: Optional[list[RedactionRect]] = None,
        *,
        image_id_short: str = "",
        occurrence_count: int = 1,
        fmt_hint: str = "png",
    ) -> None:
        """Swap the current image. Discards the previous undo stack."""
        self._image_id_short = image_id_short
        self._occurrence_count = max(1, occurrence_count)
        self._orig_bytes = image_bytes
        self._orig_fmt = fmt_hint or "png"
        self._undo.clear()
        self._canvas.set_image(image_bytes)
        # Insert the operator's pre-existing decisions silently so
        # the undo stack starts at a clean baseline. Undoing past
        # the open of this image would otherwise erase decisions
        # the operator already saved on a prior session.
        for r in (initial_rects or []):
            self._add_rect_silent(RedactionRectItem.from_redaction(r))
        # Preserve the currently-selected tool across image swaps:
        # when the operator picks "Blackout" and clicks Next, they
        # almost always want to keep painting blackout rects on the
        # next screenshot.  Just rewire the canvas draw-mode so the
        # cursor and rubber-band track the active tool.
        tool = self._current_tool or "select"
        if tool in self._tool_buttons and not self._tool_buttons[tool].isChecked():
            self._tool_buttons[tool].setChecked(True)
        self._canvas.set_draw_mode(tool != "select")
        self._refresh_side_list()
        self._update_status()
        self._update_occurrence_banner()
        # Kick the bake immediately so any pre-existing decisions
        # appear rendered the moment the image opens. The debounce
        # timer also runs as a safety net for any late changes.
        self._render_preview_to_canvas()

    def current_rects(self) -> list[RedactionRect]:
        """Snapshot of the current rect list, sorted (y, x)."""
        rects: list[RedactionRect] = []
        for item in self._canvas.scene().items():
            if not isinstance(item, RedactionRectItem):
                continue
            rects.append(item.to_redaction())
        rects.sort(key=lambda r: (r.y, r.x))
        return rects

    def has_rects(self) -> bool:
        return any(
            isinstance(item, RedactionRectItem)
            for item in self._canvas.scene().items()
        )

    def clear_rects(self) -> None:
        scene = self._canvas.scene()
        for item in list(scene.items()):
            if isinstance(item, RedactionRectItem):
                scene.removeItem(item)
        self._refresh_side_list()
        self._update_status()
        self._undo.clear()
        # Pixmap may still show baked rects from a prior state; force
        # a re-bake so the canvas reverts to the unredacted source.
        self._render_preview_to_canvas()
        self.rects_changed.emit()

    # ---- toolbar / layout ----

    def _build_toolbar(self) -> None:
        bar = QToolBar(self)
        bar.setMovable(False)
        bar.setIconSize(QSize(20, 20))

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self._tool_buttons: dict[str, QToolButton] = {}

        for key, label, icon_name, tooltip in (
            ("select", "Select", "cursor", "Select / move / resize redactions"),
            ("blackout", _TOOL_LABELS["blackout"], "blackout", "Solid black rectangle"),
            ("blur", _TOOL_LABELS["blur"], "blur", "Gaussian blur"),
            ("pixelate", _TOOL_LABELS["pixelate"], "pixelate", "Pixelate"),
            ("text_overlay", _TOOL_LABELS["text_overlay"], "text-overlay", "Text overlay (REDACTED label)"),
        ):
            btn = QToolButton(self)
            btn.setText(label)
            btn.setIcon(icon(icon_name))
            btn.setCheckable(True)
            btn.setToolTip(tooltip)
            btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self._tool_group.addButton(btn)
            self._tool_buttons[key] = btn
            bar.addWidget(btn)
            btn.toggled.connect(
                lambda checked, k=key: checked and self._on_tool_changed(k)
            )

        bar.addSeparator()

        self._intensity_label = QLabel("Intensity:")
        self._intensity_spin = QSpinBox(self)
        self._intensity_spin.setRange(2, 64)
        self._intensity_spin.setValue(8)
        self._intensity_spin.setToolTip(
            "Blur radius (px) or pixelate block size (px)."
        )
        bar.addWidget(self._intensity_label)
        bar.addWidget(self._intensity_spin)

        self._text_label = QLabel("Text:")
        self._text_edit = QLineEdit("REDACTED")
        self._text_edit.setMaximumWidth(140)
        self._text_size_label = QLabel("Size:")
        self._text_size_spin = QSpinBox(self)
        self._text_size_spin.setRange(8, 96)
        self._text_size_spin.setValue(18)
        bar.addWidget(self._text_label)
        bar.addWidget(self._text_edit)
        bar.addWidget(self._text_size_label)
        bar.addWidget(self._text_size_spin)

        # Foreground / background colour pickers for the text-overlay
        # tool. Each button paints a coloured square in its own icon
        # area so the operator sees the current selection at a glance.
        self._text_fg_color: str = "#FFFFFF"
        self._text_bg_color: str = "#000000"
        self._text_fg_label = QLabel("Font:")
        self._text_fg_btn = QToolButton(self)
        self._text_fg_btn.setToolTip("Pick the text colour (foreground).")
        self._text_fg_btn.clicked.connect(
            lambda: self._pick_text_colour("fg")
        )
        self._text_bg_label = QLabel("Bg:")
        self._text_bg_btn = QToolButton(self)
        self._text_bg_btn.setToolTip(
            "Pick the box colour painted under the text (background)."
        )
        self._text_bg_btn.clicked.connect(
            lambda: self._pick_text_colour("bg")
        )
        bar.addWidget(self._text_fg_label)
        bar.addWidget(self._text_fg_btn)
        bar.addWidget(self._text_bg_label)
        bar.addWidget(self._text_bg_btn)
        self._refresh_colour_buttons()

        bar.addSeparator()

        # Undo / redo: register the QActions both on the toolbar
        # AND on the editor widget itself so the Ctrl+Z / Ctrl+Y
        # shortcuts fire even when keyboard focus sits on the side
        # rect-list, the toolbar buttons, or any other child. The
        # default ``WindowShortcut`` context can lose to a sibling
        # tab's own undo, so we widen the catchment by adding the
        # action twice (Qt deduplicates by QAction object).
        act_undo = self._undo.createUndoAction(self, "Undo")
        act_undo.setIcon(icon("undo"))
        act_undo.setShortcuts(QKeySequence.StandardKey.Undo)
        act_undo.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        bar.addAction(act_undo)
        self.addAction(act_undo)
        act_redo = self._undo.createRedoAction(self, "Redo")
        act_redo.setIcon(icon("redo"))
        act_redo.setShortcuts(QKeySequence.StandardKey.Redo)
        act_redo.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        bar.addAction(act_redo)
        self.addAction(act_redo)

        bar.addSeparator()

        act_zoom_in = QAction(icon("zoom-in"), "Zoom in", self)
        act_zoom_in.triggered.connect(lambda: self._canvas.zoom(1.2))
        bar.addAction(act_zoom_in)
        act_zoom_out = QAction(icon("zoom-out"), "Zoom out", self)
        act_zoom_out.triggered.connect(lambda: self._canvas.zoom(1 / 1.2))
        bar.addAction(act_zoom_out)
        act_fit = QAction(icon("maximize"), "Fit", self)
        act_fit.triggered.connect(self._canvas.fit_to_view)
        bar.addAction(act_fit)
        act_100 = QAction("100%", self)
        act_100.triggered.connect(self._canvas.reset_zoom_100)
        bar.addAction(act_100)

        self._toolbar = bar

    def _build_layout(self) -> None:
        side = QWidget(self)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(8, 8, 8, 8)
        side_lay.setSpacing(6)

        self._occurrence_banner = QLabel("")
        self._occurrence_banner.setWordWrap(True)
        self._occurrence_banner.setVisible(False)
        self._occurrence_banner.setStyleSheet(
            f"QLabel {{ color: {PALETTE['warn']}; padding: 4px; "
            f"border: 1px solid {PALETTE['warn']}; border-radius: 4px; }}"
        )
        side_lay.addWidget(self._occurrence_banner)
        side_lay.addWidget(QLabel("Redactions:"))
        side_lay.addWidget(self._side_list, 1)

        delete_hint = QLabel("Press Del / Backspace to remove a selected rect.")
        delete_hint.setStyleSheet(f"QLabel {{ color: {PALETTE['text_dim']}; font-size: 11px; }}")
        side_lay.addWidget(delete_hint)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._canvas)
        splitter.addWidget(side)
        splitter.setSizes([900, 280])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setHandleWidth(6)

        self._status = QLabel("")
        self._status.setStyleSheet(
            f"QLabel {{ color: {PALETTE['text_dim']}; padding: 4px 6px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._toolbar)
        root.addWidget(splitter, 1)
        root.addWidget(self._status)

    def _wire_signals(self) -> None:
        self._canvas.rect_drawn.connect(self._on_rect_drawn)
        self._canvas.rect_geometry_committed.connect(self._on_rect_moved)
        self._canvas.selection_changed.connect(self._refresh_side_list_selection)
        self._canvas.rect_remove_requested.connect(self._on_remove_requested)

        for keyseq in (QKeySequence(Qt.Key.Key_Delete),
                       QKeySequence(Qt.Key.Key_Backspace)):
            sc = QShortcut(keyseq, self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self._delete_selected)

        # Toolbar inputs that affect rendering. Each one updates the
        # currently-selected rect (so the operator can dial in the
        # right intensity / text without redrawing) and re-bakes.
        self._intensity_spin.valueChanged.connect(self._on_intensity_changed)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_size_spin.valueChanged.connect(self._on_text_size_changed)
        # Undo / redo replay rect mutations behind the scenes; hook
        # the stack's index change so the canvas re-bakes after the
        # rect lifecycle without the user having to nudge anything.
        self._undo.indexChanged.connect(lambda _i: self._schedule_bake())

    # ---- tool changes ----

    def _on_tool_changed(self, tool: str) -> None:
        is_drawing_tool = tool != "select"
        self._canvas.set_draw_mode(is_drawing_tool)
        is_intensity = tool in ("blur", "pixelate")
        self._intensity_label.setVisible(is_intensity)
        self._intensity_spin.setVisible(is_intensity)
        is_text = tool == "text_overlay"
        self._text_label.setVisible(is_text)
        self._text_edit.setVisible(is_text)
        self._text_size_label.setVisible(is_text)
        self._text_size_spin.setVisible(is_text)
        self._text_fg_label.setVisible(is_text)
        self._text_fg_btn.setVisible(is_text)
        self._text_bg_label.setVisible(is_text)
        self._text_bg_btn.setVisible(is_text)
        self._current_tool = tool

    def _on_intensity_changed(self, value: int) -> None:
        for it in self._canvas.scene().selectedItems():
            if isinstance(it, RedactionRectItem) and it.tool in ("blur", "pixelate"):
                it.intensity = int(value)
        self._schedule_bake()
        self.rects_changed.emit()

    def _on_text_changed(self, value: str) -> None:
        clean = value.strip() or "REDACTED"
        for it in self._canvas.scene().selectedItems():
            if isinstance(it, RedactionRectItem) and it.tool == "text_overlay":
                it.text = clean
        self._schedule_bake()
        self.rects_changed.emit()

    def _on_text_size_changed(self, value: int) -> None:
        for it in self._canvas.scene().selectedItems():
            if isinstance(it, RedactionRectItem) and it.tool == "text_overlay":
                it.font_size = int(value)
        self._schedule_bake()
        self.rects_changed.emit()

    # ---- text-overlay colour pickers ----

    def _pick_text_colour(self, which: str) -> None:
        """Open a colour dialog and store the result on the editor.

        The chosen colour applies to subsequently-drawn text-overlay
        rects AND to the currently-selected one if it is a text
        overlay (so the operator can re-tint after the fact). The
        canvas re-bakes immediately so the change is visible.
        """
        current_hex = self._text_fg_color if which == "fg" else self._text_bg_color
        c = QColorDialog.getColor(
            QColor(current_hex), self,
            "Pick text colour" if which == "fg" else "Pick background colour",
        )
        if not c.isValid():
            return
        new_hex = c.name(QColor.NameFormat.HexRgb).upper()
        if which == "fg":
            self._text_fg_color = new_hex
        else:
            self._text_bg_color = new_hex
        self._refresh_colour_buttons()
        # Re-tint the currently-selected text-overlay rects so the
        # operator does not have to redraw a rect just to change a
        # colour. New rects also pick up the change.
        for it in self._canvas.scene().selectedItems():
            if isinstance(it, RedactionRectItem) and it.tool == "text_overlay":
                if which == "fg":
                    it.fg = new_hex
                else:
                    it.bg = new_hex
        self._schedule_bake()
        self.rects_changed.emit()

    def _refresh_colour_buttons(self) -> None:
        """Paint each colour-picker button with its current colour
        as a flat icon so the operator sees the choice at a glance.
        """
        for btn, hex_str in (
            (self._text_fg_btn, self._text_fg_color),
            (self._text_bg_btn, self._text_bg_color),
        ):
            pix = QPixmap(20, 20)
            pix.fill(QColor(hex_str))
            painter = QPainter(pix)
            painter.setPen(QPen(QColor(0, 0, 0, 80)))
            painter.drawRect(0, 0, 19, 19)
            painter.end()
            btn.setIcon(pix)
            btn.setIconSize(QSize(20, 20))
            btn.setText(hex_str)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    # ---- rect lifecycle ----

    def _on_rect_drawn(self, rect: QRectF) -> None:
        tool = self._current_tool
        if tool == "select":
            return
        intensity: Optional[int] = None
        if tool in ("blur", "pixelate"):
            intensity = int(self._intensity_spin.value())
        text: Optional[str] = None
        font_size: Optional[int] = None
        fg: Optional[str] = None
        bg: Optional[str] = None
        if tool == "text_overlay":
            text = self._text_edit.text().strip() or "REDACTED"
            font_size = int(self._text_size_spin.value())
            fg = self._text_fg_color
            bg = self._text_bg_color
        item = RedactionRectItem(
            rect, tool=tool, intensity=intensity,
            text=text, font_size=font_size, fg=fg, bg=bg,
        )
        self._undo.push(_AddRedactionCommand(self._canvas.scene(), item))
        self._refresh_side_list()
        self._update_status()
        self._schedule_bake()
        self.rects_changed.emit()

    def _on_rect_moved(self, item, old_rect: QRectF, new_rect: QRectF) -> None:
        if not isinstance(item, RedactionRectItem):
            return
        self._undo.push(_MoveRedactionCommand(item, old_rect, new_rect))
        self._refresh_side_list()
        self._update_status()
        self._schedule_bake()
        self.rects_changed.emit()

    def _delete_selected(self) -> None:
        scene = self._canvas.scene()
        for item in scene.selectedItems():
            if isinstance(item, RedactionRectItem):
                self._undo.push(_DeleteRedactionCommand(scene, item))
        self._refresh_side_list()
        self._update_status()
        self._schedule_bake()
        self.rects_changed.emit()

    def _on_remove_requested(self, item) -> None:
        """Right-click → Remove flow. Wraps the deletion in a
        QUndoCommand so the operator can still hit Ctrl+Z if they
        clicked the wrong rect."""
        if not isinstance(item, RedactionRectItem):
            return
        scene = self._canvas.scene()
        self._undo.push(_DeleteRedactionCommand(scene, item))
        self._refresh_side_list()
        self._update_status()
        self._schedule_bake()
        self.rects_changed.emit()

    def _add_rect_silent(self, item: RedactionRectItem) -> None:
        """Add a rect WITHOUT pushing to the undo stack. Used when
        seeding the editor with operator decisions persisted on a
        previous session: undoing back past the load should not
        erase work the operator already saved.
        """
        self._canvas.scene().addItem(item)

    # ---- side panel sync ----

    def _refresh_side_list(self) -> None:
        self._side_list.blockSignals(True)
        self._side_list.clear()
        for item in self._canvas.scene().items():
            if not isinstance(item, RedactionRectItem):
                continue
            r = item.rect().translated(item.pos())
            label_text = (
                f"{_TOOL_LABELS.get(item.tool, item.tool)}  "
                f"({int(r.x())}, {int(r.y())})  "
                f"{int(r.width())}x{int(r.height())}"
            )
            li = QListWidgetItem(icon(_TOOL_ICONS.get(item.tool, "wand")), label_text)
            li.setData(Qt.ItemDataRole.UserRole, id(item))
            self._side_list.addItem(li)
        self._side_list.blockSignals(False)

    def _refresh_side_list_selection(self) -> None:
        sel_ids = {
            id(it) for it in self._canvas.scene().selectedItems()
            if isinstance(it, RedactionRectItem)
        }
        for i in range(self._side_list.count()):
            li = self._side_list.item(i)
            li.setSelected(li.data(Qt.ItemDataRole.UserRole) in sel_ids)

    def _on_side_selection_changed(self) -> None:
        sel_ids = {
            li.data(Qt.ItemDataRole.UserRole)
            for li in self._side_list.selectedItems()
        }
        for it in self._canvas.scene().items():
            if not isinstance(it, RedactionRectItem):
                continue
            it.setSelected(id(it) in sel_ids)

    def _update_status(self) -> None:
        n = sum(
            1 for it in self._canvas.scene().items()
            if isinstance(it, RedactionRectItem)
        )
        size = self._canvas._image_size
        bits: list[str] = []
        if self._image_id_short:
            bits.append(self._image_id_short)
        if not size.isEmpty():
            bits.append(f"{size.width()} x {size.height()}")
        bits.append(f"{n} redaction{'s' if n != 1 else ''}")
        self._status.setText("  ·  ".join(bits))

    # ---- preview mode ----

    def _schedule_bake(self) -> None:
        """Coalesce rapid edits into a single re-bake."""
        self._bake_timer.start()

    def _render_preview_to_canvas(self) -> None:
        """Run :class:`ImageRedactor` with the current rects and swap
        the canvas pixmap to the baked result.

        This is the *live* canvas now: the operator always sees the
        actual blackout / blur / pixelate / text-overlay pixels
        rather than translucent rectangles. Rect borders stay drawn
        on top so the rects remain selectable and movable.
        """
        if not self._orig_bytes:
            return
        from anonymize.image_redactor import ImageRedaction, ImageRedactor

        rects: list[ImageRedaction] = []
        for r in self.current_rects():
            rects.append(
                ImageRedaction(
                    x=r.x, y=r.y, w=r.w, h=r.h,
                    tool=r.tool,
                    intensity=r.intensity,
                    text=r.text,
                    font_size=r.font_size,
                    fg=r.fg,
                    bg=r.bg,
                )
            )
        try:
            result = ImageRedactor.redact_bytes(
                self._orig_bytes, self._orig_fmt, rects
            )
        except Exception:
            # Fall back to the original on any error so the operator
            # sees something rather than a blank canvas.
            self._set_canvas_pixmap(self._orig_bytes)
            return
        self._set_canvas_pixmap(result.bytes_)

    def _set_canvas_pixmap(self, raw_bytes: bytes) -> None:
        """Replace ONLY the background pixmap, keeping the rect items
        attached to the scene (still hidden in preview mode).

        This is subtler than :meth:`_ImageCanvas.set_image`, which
        clears the whole scene. Here we walk the scene, find the
        existing pixmap item (z=-10 by convention), update its
        pixmap, and leave the rect items in place.
        """
        scene = self._canvas.scene()
        # Find the existing pixmap item.
        pix_item: Optional[QGraphicsPixmapItem] = None
        for item in scene.items():
            if isinstance(item, QGraphicsPixmapItem):
                pix_item = item
                break
        img = QImage.fromData(raw_bytes)
        new_pix = QPixmap.fromImage(img)
        if pix_item is None:
            self._canvas.set_image(raw_bytes)
            return
        pix_item.setPixmap(new_pix)
        scene.setSceneRect(QRectF(0, 0, new_pix.width(), new_pix.height()))

    def _update_occurrence_banner(self) -> None:
        if self._occurrence_count > 1:
            self._occurrence_banner.setText(
                f"This image appears {self._occurrence_count} times across "
                "the project. Saving redactions applies to every occurrence."
            )
            self._occurrence_banner.setVisible(True)
        else:
            self._occurrence_banner.setVisible(False)


# --------------------------------------------------------------------
# Modal wrapper (kept for any code path that wants a one-shot dialog).
# --------------------------------------------------------------------

class ImageEditorDialog(QDialog):
    """Modal wrapper around :class:`ImageEditorWidget`."""

    def __init__(
        self,
        image_bytes: bytes,
        initial_rects: Optional[list[RedactionRect]] = None,
        *,
        image_id_short: str = "",
        occurrence_count: int = 1,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Image redaction editor")
        self.setModal(True)
        self.resize(1100, 800)

        self._editor = ImageEditorWidget(self)
        self._editor.load_image(
            image_bytes, initial_rects,
            image_id_short=image_id_short,
            occurrence_count=occurrence_count,
        )

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._editor, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 8)
        bottom.addStretch()
        bottom.addWidget(button_box)
        root.addLayout(bottom)

        self._result: Optional[list[RedactionRect]] = None

    @staticmethod
    def edit(
        image_bytes: bytes,
        initial_rects: Optional[list[RedactionRect]] = None,
        *,
        image_id_short: str = "",
        occurrence_count: int = 1,
        parent: Optional[QWidget] = None,
    ) -> Optional[list[RedactionRect]]:
        dlg = ImageEditorDialog(
            image_bytes,
            initial_rects,
            image_id_short=image_id_short,
            occurrence_count=occurrence_count,
            parent=parent,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._editor.current_rects()
        return None


__all__ = [
    "ImageEditorDialog",
    "ImageEditorWidget",
    "RedactionRectItem",
]
