"""Per-format render panes used by the rendered diff view.

Each pane shows a single document (original OR anonymized) with the
substituted regions highlighted. Three flavours:

* :class:`PdfRenderPane`, rasterises every page via PyMuPDF and
  overlays semi-transparent ``QGraphicsRectItem`` on the substituted
  rectangles. Used directly for ``.pdf`` files.

* :class:`OfficeRenderPane`, converts office docs (``.docx``,
  ``.pptx``, ``.odt``, ``.rtf``) to PDF via ``libreoffice --headless
  --convert-to pdf`` (cached under ``/tmp``), then delegates to
  :class:`PdfRenderPane`.

* :class:`HtmlRenderPane`, uses ``QWebEngineView`` to render HTML
  with the substituted spans wrapped in highlighted ``<mark>`` tags.

* :class:`PlainTextRenderPane`, falls back to the legacy plaintext
  view for ``.txt`` / ``.md`` / ``.json`` / unknown formats. Highlights
  by character offset, same as the old diff view.
"""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


_PLAINTEXT_EXTS = {
    ".txt", ".log",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".sh",
}
# Office docs handled via libreoffice → PDF.  Spreadsheets get a
# dedicated HTML-table pane (vastly better than the cropped PDF
# render libreoffice produces for wide sheets).
_OFFICE_EXTS = {".docx", ".pptx", ".odt", ".odp", ".doc", ".rtf"}
_HTML_EXTS = {".html", ".htm"}
_MARKDOWN_EXTS = {".md", ".markdown"}
_SPREADSHEET_EXTS = {".xlsx", ".xlsm", ".ods", ".csv", ".tsv"}


def _hashed_color(value: str, alpha: int = 90) -> QColor:
    """Stable per-value HSL accent so the same mapping looks the same
    on both sides of the diff."""
    h = int(hashlib.md5(value.encode("utf-8")).hexdigest()[:8], 16) % 360
    c = QColor.fromHsl(h, int(255 * 0.55), int(255 * 0.55))
    c.setAlpha(alpha)
    return c


def _border_color(value: str) -> QColor:
    h = int(hashlib.md5(value.encode("utf-8")).hexdigest()[:8], 16) % 360
    return QColor.fromHsl(h, int(255 * 0.70), int(255 * 0.45))


# ---------------------------------------------------------------------------
# Add-to-substitution-map context-menu helpers
#
# The right-click "Add to substitution map" action is offered by every
# render pane that supports text selection (PDF, Office, HTML, Markdown,
# Spreadsheet, plaintext). The actual *menu* per-widget differs because
# Chromium-based panes can't read their selection via Qt APIs and have
# to copy-to-clipboard first, but the prompt + label conventions live
# here so the operator sees the same wording everywhere.
# ---------------------------------------------------------------------------


def _prompt_manual_add_to_map(parent) -> Optional[str]:
    """Modal input for the "type a value to anonymize" fallback. Returns
    the stripped non-empty value, or ``None`` when the operator cancels
    or types only whitespace."""
    from PySide6.QtWidgets import QInputDialog

    text, ok = QInputDialog.getText(
        parent,
        "Add to substitution map",
        "Value to anonymize (placeholder defaults to XXXX):",
    )
    if not ok:
        return None
    clean = (text or "").strip()
    return clean or None


def _add_to_map_action_label(text: str) -> str:
    preview = text if len(text) <= 60 else text[:57] + "..."
    return f'Add "{preview}" to substitution map (XXXX)'


class _SelectablePlainTextEdit(QPlainTextEdit):
    """``QPlainTextEdit`` with the anonymizer right-click menu grafted
    onto Qt's standard context menu (Copy / Select all etc.).
    """

    add_to_map_requested = Signal(str)

    def contextMenuEvent(self, event):  # type: ignore[override]
        menu = self.createStandardContextMenu()
        selected = self._selection_or_word_at(event)
        menu.addSeparator()
        act_add = None
        if selected:
            act_add = menu.addAction(_add_to_map_action_label(selected))
        act_manual = menu.addAction("Add to substitution map manually…")
        chosen = menu.exec(event.globalPos())
        if act_add is not None and chosen is act_add and selected:
            self.add_to_map_requested.emit(selected)
        elif chosen is act_manual:
            text = _prompt_manual_add_to_map(self)
            if text:
                self.add_to_map_requested.emit(text)

    def _selection_or_word_at(self, event) -> str:
        cur = self.textCursor()
        if cur.hasSelection():
            return (cur.selectedText() or "").strip()
        word_cur = self.cursorForPosition(event.pos())
        word_cur.select(QTextCursor.SelectionType.WordUnderCursor)
        return (word_cur.selectedText() or "").strip()


class _SelectableTextEdit(QTextEdit):
    """``QTextEdit`` variant of :class:`_SelectablePlainTextEdit`."""

    add_to_map_requested = Signal(str)

    def contextMenuEvent(self, event):  # type: ignore[override]
        menu = self.createStandardContextMenu()
        selected = self._selection_or_word_at(event)
        menu.addSeparator()
        act_add = None
        if selected:
            act_add = menu.addAction(_add_to_map_action_label(selected))
        act_manual = menu.addAction("Add to substitution map manually…")
        chosen = menu.exec(event.globalPos())
        if act_add is not None and chosen is act_add and selected:
            self.add_to_map_requested.emit(selected)
        elif chosen is act_manual:
            text = _prompt_manual_add_to_map(self)
            if text:
                self.add_to_map_requested.emit(text)

    def _selection_or_word_at(self, event) -> str:
        cur = self.textCursor()
        if cur.hasSelection():
            return (cur.selectedText() or "").strip()
        word_cur = self.cursorForPosition(event.pos())
        word_cur.select(QTextCursor.SelectionType.WordUnderCursor)
        return (word_cur.selectedText() or "").strip()


# ---------------------------------------------------------------------------
# Plaintext fallback (legacy behaviour, kept for ``.txt`` / ``.md`` / etc.)
# ---------------------------------------------------------------------------


class PlainTextRenderPane(QWidget):
    """Read-only plaintext view with offset-based highlights, same UX
    the old diff view had on every format."""

    add_to_map_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.edit = _SelectablePlainTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.edit.add_to_map_requested.connect(self.add_to_map_requested.emit)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self._spans: list[dict] = []
        self._scheme = "per_mapping"
        self._category_filter = ""
        self._base_pt = max(8, self.edit.font().pointSize() or 10)
        self._zoom = 1.0

    # ---- zoom --------------------------------------------------------------

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        # No layout to fit for plain text, leave at 100%.
        self._set_zoom(1.0)

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.25, min(8.0, factor))
        self._zoom = factor
        f = self.edit.font()
        f.setPointSizeF(self._base_pt * factor)
        self.edit.setFont(f)

    def set_text(self, text: str) -> None:
        self.edit.setPlainText(text)

    def set_spans(self, spans: list[dict]) -> None:
        self._spans = spans
        self._repaint_highlights()

    def set_scheme(self, scheme: str) -> None:
        self._scheme = scheme
        self._repaint_highlights()

    def set_category_filter(self, category: str) -> None:
        self._category_filter = category
        self._repaint_highlights()

    def _repaint_highlights(self) -> None:
        # Use ExtraSelections, cheaper than re-running the highlighter
        # and works on the existing document.
        sel: list = []
        from PySide6.QtWidgets import QTextEdit

        for span in self._spans:
            if (
                self._category_filter
                and span.get("category") != self._category_filter
            ):
                continue
            off = span.get("off", 0)
            ln = span.get("len", 0)
            if ln <= 0:
                continue
            cur = self.edit.textCursor()
            cur.setPosition(int(off))
            cur.movePosition(
                cur.MoveOperation.NextCharacter,
                cur.MoveMode.KeepAnchor,
                int(ln),
            )
            fmt = QTextCharFormat()
            if self._scheme == "classic":
                # Caller must override via set_scheme + side hint; for
                # plaintext we just use per-mapping.
                color = _hashed_color(span.get("value", ""))
            else:
                color = _hashed_color(span.get("value", ""))
            fmt.setBackground(color)
            es = QTextEdit.ExtraSelection()
            es.cursor = cur
            es.format = fmt
            sel.append(es)
        self.edit.setExtraSelections(sel)


# ---------------------------------------------------------------------------
# PDF rendering (PyMuPDF) with rect-level overlays
# ---------------------------------------------------------------------------


class PdfRenderPane(QWidget):
    """Render every page of a PDF as a pixmap, stack them vertically in
    a QGraphicsScene, and overlay highlight rectangles for the
    substituted regions.

    ``set_events(events, side='left'|'right')`` decides whether to
    use the original-text rects (``side='left'``) or rebuild the rects
    from the anonymised PDF via ``page.search_for(to)``
    (``side='right'``).
    """

    DEFAULT_DPI = 144  # readable on a 1080p screen, ~2× the 72 PDF default

    def __init__(self) -> None:
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.Antialiasing
        )
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Ctrl+wheel zoom, normal wheel = scroll.
        self.view.viewport().installEventFilter(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self._page_origins: list[tuple[float, float, float, float]] = []
        # ``(y_top, height, scale, page_width_px)`` per page.
        self._scale = self.DEFAULT_DPI / 72.0
        self._highlight_items: list[QGraphicsRectItem] = []
        self._scheme = "per_mapping"
        self._category_filter = ""
        self._pending_events: list[dict] = []
        self._side: str = "left"
        self._zoom = 1.0
        self._fit_mode = False  # if True, refit on resize
        self._pdf_path: Optional[Path] = None
        self._page_widths_pt: list[float] = []
        # Re-rasterising N pages on every wheel tick is wasteful, coalesce
        # rapid zoom changes into a single render so the pixmap is sharp
        # at the displayed DPI without burning CPU.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._do_rerender)

    # ---- public API --------------------------------------------------------

    def load_pdf(self, path: Path) -> bool:
        try:
            import fitz
        except Exception:
            return False
        self._pdf_path = path
        try:
            doc = fitz.open(str(path))
            try:
                self._page_widths_pt = [float(p.rect.width) for p in doc]
            finally:
                doc.close()
        except Exception:
            return False
        if self._fit_mode:
            self._update_zoom_for_fit()
        return self._render_pages_at_scale(self._scale_from_zoom())

    def _scale_from_zoom(self) -> float:
        return (self.DEFAULT_DPI / 72.0) * self._zoom

    def _update_zoom_for_fit(self) -> None:
        """Compute the zoom factor that makes the widest page fit the
        viewport width exactly. Stores the result in ``self._zoom``
        without triggering a re-render (the caller decides when)."""
        if not self._page_widths_pt:
            return
        max_w_pt = max(self._page_widths_pt)
        if max_w_pt <= 0:
            return
        viewport_w_px = max(1, self.view.viewport().width() - 4)
        # rendered_pixels = pt * (DEFAULT_DPI/72) * zoom == viewport
        target_scale = viewport_w_px / max_w_pt
        self._zoom = max(0.1, min(8.0, target_scale / (self.DEFAULT_DPI / 72.0)))

    def _render_pages_at_scale(self, scale: float) -> bool:
        """Re-rasterise every page at ``scale`` (PDF-points → pixels).

        The pixmap is generated at the actual displayed DPI so there is
        no blurry interpolation regardless of the zoom level.  The
        QGraphicsView transform is reset to identity since the scene
        already holds pages at the right resolution.
        """
        if self._pdf_path is None:
            return False
        try:
            import fitz
        except Exception:
            return False
        # Preserve the visible center so re-renders don't jolt the user
        # back to the top.
        old_rect = self.scene.sceneRect()
        cx_frac = cy_frac = 0.0
        if old_rect.width() > 0 and old_rect.height() > 0:
            visible_center = self.view.mapToScene(
                self.view.viewport().rect().center()
            )
            cx_frac = visible_center.x() / old_rect.width()
            cy_frac = visible_center.y() / old_rect.height()

        self.scene.clear()
        self._highlight_items.clear()
        self._page_origins.clear()
        self._scale = scale
        try:
            doc = fitz.open(str(self._pdf_path))
        except Exception:
            return False
        try:
            y = 0.0
            mat = self._matrix(scale)
            max_w_px = 0
            for page in doc:
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = QImage(
                    bytes(pix.samples),
                    pix.width,
                    pix.height,
                    pix.stride,
                    QImage.Format.Format_RGB888,
                )
                pm = QPixmap.fromImage(img.copy())
                gp = QGraphicsPixmapItem(pm)
                gp.setOffset(0, y)
                gp.setZValue(0)
                gp.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                self.scene.addItem(gp)
                self._page_origins.append((y, pix.height, scale, pix.width))
                if pix.width > max_w_px:
                    max_w_px = pix.width
                y += pix.height + 8  # 8px gutter between pages
            self.scene.setSceneRect(0, 0, max_w_px, y)
        finally:
            doc.close()
        # Pages are now at the right resolution, drop any leftover
        # transform from a previous transform-based zoom.
        self.view.setTransform(QTransform())
        # Restore the visible-center fraction across the resize.
        new_rect = self.scene.sceneRect()
        if new_rect.width() > 0 and new_rect.height() > 0 and (cx_frac or cy_frac):
            self.view.centerOn(
                cx_frac * new_rect.width(), cy_frac * new_rect.height()
            )
        self._repaint_highlights()
        return True

    def _do_rerender(self) -> None:
        if self._fit_mode:
            self._update_zoom_for_fit()
        self._render_pages_at_scale(self._scale_from_zoom())

    def set_events(self, events: list[dict], *, side: str) -> None:
        """Consumes the substitution events as stored in
        ``applied_substitutions.json``. ``side`` selects whether to
        overlay on original rects (``"left"``) or recompute rects on
        the right side via ``search_for(to)``.
        """
        self._pending_events = events or []
        self._side = side
        self._repaint_highlights()

    def set_scheme(self, scheme: str) -> None:
        self._scheme = scheme
        self._repaint_highlights()

    def set_category_filter(self, category: str) -> None:
        self._category_filter = category
        self._repaint_highlights()

    def jump_to_event(self, ev_index: int) -> None:
        """Center the viewport on the n-th event highlight, if any."""
        if 0 <= ev_index < len(self._highlight_items):
            self.view.centerOn(self._highlight_items[ev_index])

    # ---- zoom --------------------------------------------------------------

    def zoom_in(self) -> None:
        self._fit_mode = False
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._fit_mode = False
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._fit_mode = False
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        """Scale so the widest page exactly fits the viewport width.

        Uses the page width in PDF points (not the previously-rendered
        pixel width) so repeated fits don't accumulate rounding drift.
        """
        if not self._page_widths_pt:
            return
        self._fit_mode = True
        # Schedule a debounced re-render at the fitted zoom, the timer
        # callback recomputes the exact scale so concurrent resizes
        # collapse into a single rasterisation.
        self._render_timer.start()

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.1, min(8.0, factor))
        self._zoom = factor
        self._render_timer.start()

    def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(ev)
        if self._fit_mode:
            self._render_timer.start()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 (Qt naming)
        if obj is self.view.viewport() and ev.type() == QEvent.Type.Wheel:
            if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if ev.angleDelta().y() > 0:
                    self.zoom_in()
                else:
                    self.zoom_out()
                ev.accept()
                return True
        return super().eventFilter(obj, ev)

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _matrix(scale: float):
        import fitz

        return fitz.Matrix(scale, scale)

    def _repaint_highlights(self) -> None:
        # Drop existing overlays.
        for it in self._highlight_items:
            try:
                self.scene.removeItem(it)
            except Exception:
                pass
        self._highlight_items.clear()
        if not self._page_origins:
            return
        for ev in self._pending_events or []:
            if (
                self._category_filter
                and ev.get("category") != self._category_filter
            ):
                continue
            page = ev.get("page")
            rects = ev.get("rects") if self._side == "left" else ev.get("rects_right")
            if not rects or page is None or page >= len(self._page_origins):
                continue
            y_top, _ph, scale, _pw = self._page_origins[page]
            if self._scheme == "classic":
                color = (
                    QColor(248, 81, 73, 90)
                    if self._side == "left"
                    else QColor(63, 185, 80, 90)
                )
                pen = QPen(QColor(248 if self._side == "left" else 63, 81, 73))
            else:
                color = _hashed_color(ev.get("from") or ev.get("value") or "")
                pen = QPen(_border_color(ev.get("from") or ev.get("value") or ""))
            pen.setWidthF(0.5)
            for r in rects:
                if len(r) != 4:
                    continue
                x0, y0, x1, y1 = (float(c) for c in r)
                rect = QRectF(
                    x0 * scale,
                    y_top + y0 * scale,
                    (x1 - x0) * scale,
                    (y1 - y0) * scale,
                )
                item = QGraphicsRectItem(rect)
                item.setBrush(QBrush(color))
                item.setPen(pen)
                item.setZValue(1)
                item.setData(0, ev.get("from") or "")
                self.scene.addItem(item)
                self._highlight_items.append(item)


def derive_right_rects(
    events: list[dict], anonymized_pdf: Path
) -> list[dict]:
    """For each event, also compute on-page rects of the anonymised
    placeholder via ``page.search_for(to)`` so the right pane can
    highlight too. Returns a NEW list (events are deep-copied).

    Search strategy is layered, because PyMuPDF's default
    ``search_for`` requires the literal text to appear contiguous in
    a single line: a placeholder that wraps across a soft line break
    or that gets hyphenated will silently miss. We progressively
    relax the match until something is found, in order:

    1. Exact ``search_for(to)``.
    2. ``search_for(to, flags=TEXT_DEHYPHENATE)`` so hyphenated wraps
       still match.
    3. Tokenised search: split on whitespace, search each token, and
       join adjacent rects on the same line into one cluster.

    The returned rects are tuples of ``(x0, y0, x1, y1)`` in PDF
    points, ready to be scaled by the render pane.
    """
    try:
        import fitz
    except Exception:
        return events
    out: list[dict] = []
    if not anonymized_pdf.exists():
        return [dict(ev) for ev in events]
    try:
        doc = fitz.open(str(anonymized_pdf))
    except Exception:
        return [dict(ev) for ev in events]
    # Cache: per-page list of (to_value, [cluster, …]) so multiple
    # events for the same placeholder can pop in document order.
    pop_idx: dict[tuple[int, str], int] = {}
    cache: dict[tuple[int, str], list[list[tuple[float, float, float, float]]]] = {}
    try:
        for ev in events:
            new_ev = dict(ev)
            page_idx = ev.get("page")
            to_str = ev.get("to") or ""
            if page_idx is None or not to_str or page_idx >= doc.page_count:
                out.append(new_ev)
                continue
            cache_key = (page_idx, to_str)
            if cache_key not in cache:
                page = doc.load_page(page_idx)
                cache[cache_key] = _search_placeholder_clusters(
                    page, to_str, fitz
                )
            clusters = cache[cache_key]
            i = pop_idx.get(cache_key, 0)
            if i < len(clusters):
                new_ev["rects_right"] = clusters[i]
                pop_idx[cache_key] = i + 1
            out.append(new_ev)
    finally:
        doc.close()
    return out


def _search_placeholder_clusters(
    page, to_str: str, fitz_mod
) -> list[list[tuple[float, float, float, float]]]:
    """Locate every occurrence of ``to_str`` on ``page``, returning a
    list of rect-clusters (one cluster per occurrence). Each cluster
    is a list of ``(x0, y0, x1, y1)`` tuples; multi-rect clusters
    cover wrapped or split matches."""
    # 1. Exact search (single rect per hit; fast path).
    try:
        hits = list(page.search_for(to_str) or [])
    except Exception:
        hits = []
    if hits:
        return [
            [(float(r[0]), float(r[1]), float(r[2]), float(r[3]))]
            for r in hits
        ]
    # 2. Dehyphenate flag: handle line-break hyphenation that PyMuPDF
    #    otherwise treats as a literal hyphen in the haystack.
    flags = getattr(fitz_mod, "TEXT_DEHYPHENATE", 0)
    if flags:
        try:
            hits = list(page.search_for(to_str, flags=flags) or [])
        except Exception:
            hits = []
        if hits:
            return [
                [(float(r[0]), float(r[1]), float(r[2]), float(r[3]))]
                for r in hits
            ]
    # 3. Tokenised search: split on whitespace, look for each token,
    #    then walk left-to-right and group consecutive token rects on
    #    the same line into a single cluster (one per occurrence).
    tokens = [t for t in to_str.split() if t]
    if not tokens:
        return []
    per_token: list[list[tuple[float, float, float, float]]] = []
    for tok in tokens:
        try:
            tok_hits = list(page.search_for(tok) or [])
        except Exception:
            tok_hits = []
        per_token.append([
            (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
            for r in tok_hits
        ])
    if not per_token[0]:
        return []
    clusters: list[list[tuple[float, float, float, float]]] = []
    consumed = [set() for _ in tokens]
    for anchor in per_token[0]:
        cluster = [anchor]
        cursor = anchor
        ok = True
        for ti in range(1, len(tokens)):
            best = -1
            best_dx = float("inf")
            for ci, cand in enumerate(per_token[ti]):
                if ci in consumed[ti]:
                    continue
                # Same-line heuristic: vertical centres within half a line
                # height, and the candidate starts to the right of the
                # current cursor.
                cand_yc = (cand[1] + cand[3]) / 2.0
                cur_yc = (cursor[1] + cursor[3]) / 2.0
                line_h = max(cursor[3] - cursor[1], 1.0)
                if abs(cand_yc - cur_yc) > line_h * 0.6:
                    continue
                if cand[0] < cursor[2] - 0.1:
                    continue
                dx = cand[0] - cursor[2]
                if dx < best_dx:
                    best_dx = dx
                    best = ci
            if best == -1:
                ok = False
                break
            cluster.append(per_token[ti][best])
            consumed[ti].add(best)
            cursor = per_token[ti][best]
        if ok:
            clusters.append(cluster)
    return clusters


# ---------------------------------------------------------------------------
# Office (libreoffice) → PDF cache → PdfRenderPane
# ---------------------------------------------------------------------------


class OfficeRenderPane(PdfRenderPane):
    """Convert office documents to PDF on demand and delegate to
    :class:`PdfRenderPane`. The PDF is cached under
    ``/tmp/anonbench/officediff/`` so repeated views of the same file
    are fast.
    """

    _CACHE_ROOT = Path(tempfile.gettempdir()) / "anondiff" / "office"

    def load_office(self, path: Path) -> bool:
        pdf_path = self._convert_to_pdf(path)
        if pdf_path is None:
            return False
        return self.load_pdf(pdf_path)

    @classmethod
    def _convert_to_pdf(cls, src: Path) -> Optional[Path]:
        cls._CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        # Cache key: src absolute path + mtime.
        try:
            mtime = src.stat().st_mtime_ns
        except Exception:
            mtime = 0
        key = hashlib.md5(
            f"{src.resolve()}|{mtime}".encode("utf-8")
        ).hexdigest()[:16]
        target = cls._CACHE_ROOT / f"{key}_{src.stem}.pdf"
        if target.exists():
            return target
        soffice = shutil.which("libreoffice") or shutil.which("soffice")
        if soffice is None:
            return None
        with tempfile.TemporaryDirectory() as td:
            cmd = [
                soffice,
                "--headless",
                "--norestore",
                "--nologo",
                "--nodefault",
                "--convert-to",
                "pdf",
                "--outdir",
                td,
                str(src),
            ]
            try:
                subprocess.run(
                    cmd, check=False, capture_output=True, timeout=120
                )
            except Exception:
                return None
            produced = Path(td) / f"{src.stem}.pdf"
            if not produced.exists():
                return None
            try:
                shutil.copy2(produced, target)
            except Exception:
                return None
        return target


# ---------------------------------------------------------------------------
# HTML (QWebEngineView) with mark-based highlights
# ---------------------------------------------------------------------------


class HtmlRenderPane(QWidget):
    """Render an HTML file with substituted spans wrapped in
    highlighted ``<mark>`` tags.

    QtWebEngine is loaded lazily so the rest of the app keeps working
    when ``PySide6-Addons`` is not installed.
    """

    add_to_map_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._web = None
        self._fallback = QLabel(
            "QtWebEngine is not installed. Install PySide6-Addons to "
            "render HTML with highlights, or open the file in your "
            "browser."
        )
        self._fallback.setObjectName("Muted")
        self._fallback.setWordWrap(True)
        try:
            if _SelectablePdfWebView is None:
                raise ImportError("QtWebEngine widgets not available")
            self._web = _SelectablePdfWebView()
            self._web.add_to_map_requested.connect(
                self.add_to_map_requested.emit
            )
            lay.addWidget(self._web)
        except Exception:
            lay.addWidget(self._fallback)
        self._zoom = 1.0

    def is_available(self) -> bool:
        return self._web is not None

    # ---- zoom --------------------------------------------------------------

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        # QWebEngineView reflows on resize already, reset to 100%.
        self._set_zoom(1.0)

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.25, min(5.0, factor))
        self._zoom = factor
        if self._web is not None:
            self._web.setZoomFactor(factor)

    def render_html(
        self,
        html_text: str,
        *,
        highlight_values: list[str],
        scheme: str = "per_mapping",
        color_keys: list[str] | None = None,
    ) -> None:
        if self._web is None:
            return
        injected = self._inject_marks(
            html_text, highlight_values, scheme, color_keys
        )
        self._web.setHtml(injected)

    @staticmethod
    def _inject_marks(
        html_text: str,
        values: list[str],
        scheme: str,
        color_keys: list[str] | None = None,
    ) -> str:
        """Wrap each occurrence of every value in ``<mark
        style="background:…">…</mark>`` outside of HTML tags. We do
        the replacement on the rendered text outside ``<…>`` segments
        so we don't accidentally rewrite tag attributes.

        ``color_keys`` is an optional parallel list aligning each
        ``values[i]`` with the string used to derive its background
        colour. This makes the same ``from→to`` mapping look identical
        on both sides of the diff: the left side highlights the
        original text, the right side highlights the placeholder, and
        both pass ``from`` as the colour key.
        """
        if not values:
            return html_text
        # Build a value→color_key map (deduplicated, longest-first so
        # nested tokens are replaced in the right order).
        keys = list(color_keys or [])
        pairs: dict[str, str] = {}
        for idx, v in enumerate(values or []):
            if not v:
                continue
            ck = keys[idx] if idx < len(keys) and keys[idx] else v
            pairs.setdefault(v, ck)
        sorted_values = sorted(pairs.keys(), key=lambda v: -len(v))
        # Walk the HTML, replacing only outside tag boundaries.
        out: list[str] = []
        i = 0
        n = len(html_text)
        while i < n:
            ch = html_text[i]
            if ch == "<":
                # Skip the tag.
                end = html_text.find(">", i + 1)
                if end == -1:
                    out.append(html_text[i:])
                    break
                out.append(html_text[i : end + 1])
                i = end + 1
                continue
            # Find the next tag start; replace inside [i, next_lt).
            next_lt = html_text.find("<", i)
            if next_lt == -1:
                next_lt = n
            chunk = html_text[i:next_lt]
            chunk_replaced = chunk
            for v in sorted_values:
                if scheme == "classic":
                    color = "#f8514950"
                else:
                    color = _hashed_color(pairs[v]).name(QColor.NameFormat.HexArgb)
                replacement = (
                    f'<mark style="background:{color}; '
                    f'padding:0 2px; border-radius:3px;">'
                    f"{html.escape(v)}</mark>"
                )
                # Case-sensitive token replacement.
                chunk_replaced = chunk_replaced.replace(html.escape(v), replacement)
            out.append(chunk_replaced)
            i = next_lt
        return "".join(out)


# ---------------------------------------------------------------------------
# Markdown (Qt's built-in QTextDocument.setMarkdown) with mark-style highlights
# ---------------------------------------------------------------------------


class MarkdownRenderPane(QWidget):
    """Render a Markdown document with native rich-text formatting.

    Uses ``QTextDocument.setMarkdown`` (shipped with Qt 6) so we don't
    pull in a Python markdown library, headings, lists, tables, code
    fences and emphasis all render with Qt's built-in styling and
    inherit the GUI palette automatically.

    Substituted placeholders are highlighted via ``QTextEdit``
    extra-selections, same hashed accent the other panes use, so the
    same mapping looks identical on both sides of the diff.
    """

    add_to_map_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.edit = _SelectableTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.edit.add_to_map_requested.connect(self.add_to_map_requested.emit)
        # A monospaced fallback for code blocks; QTextDocument's
        # markdown renderer respects ``font-family: monospace`` inside
        # ``<pre>``/``<code>`` blocks via the document's default font.
        f = self.edit.font()
        if f.pointSize() <= 0:
            f.setPointSize(11)
        self.edit.setFont(f)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self._base_pt = max(8, f.pointSize() or 11)
        self._zoom = 1.0
        self._highlight_values: list[str] = []
        self._color_keys: list[str] = []
        self._scheme = "per_mapping"

    # ---- public API --------------------------------------------------------

    def render_markdown(
        self,
        md_text: str,
        *,
        highlight_values: list[str],
        scheme: str = "per_mapping",
        color_keys: list[str] | None = None,
    ) -> None:
        # ``setMarkdown`` accepts CommonMark + GitHub-flavoured tables.
        # Empty input still clears the view, which is what we want when
        # the file becomes unreadable mid-pipeline.
        self.edit.setMarkdown(md_text or "")
        self._highlight_values = list(highlight_values or [])
        self._color_keys = list(color_keys or [])
        self._scheme = scheme
        self._repaint_highlights()

    # ---- zoom --------------------------------------------------------------

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        # The QTextEdit already wraps to the viewport width; "fit"
        # collapses to a 100% reset so the user gets a predictable
        # baseline.
        self._set_zoom(1.0)

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.25, min(8.0, factor))
        self._zoom = factor
        f = self.edit.font()
        f.setPointSizeF(self._base_pt * factor)
        self.edit.setFont(f)

    # ---- internals ---------------------------------------------------------

    def _repaint_highlights(self) -> None:
        sel: list = []
        if not self._highlight_values:
            self.edit.setExtraSelections([])
            return
        # Pair each highlight value with its color key (defaulting to
        # the value itself for back-compat). Same color key means same
        # background, which is what makes from↔to share a colour
        # across the diff panes.
        keys = self._color_keys
        pairs: dict[str, str] = {}
        for idx, v in enumerate(self._highlight_values):
            if not v:
                continue
            ck = keys[idx] if idx < len(keys) and keys[idx] else v
            pairs.setdefault(v, ck)
        doc = self.edit.document()
        for v in sorted(pairs.keys(), key=lambda x: -len(x)):
            if self._scheme == "classic":
                color = QColor(63, 185, 80, 90)
            else:
                color = _hashed_color(pairs[v])
            cursor = QTextCursor(doc)
            while True:
                cursor = doc.find(v, cursor)
                if cursor.isNull():
                    break
                fmt = QTextCharFormat()
                fmt.setBackground(color)
                es = QTextEdit.ExtraSelection()
                es.cursor = QTextCursor(cursor)
                es.format = fmt
                sel.append(es)
        self.edit.setExtraSelections(sel)


# ---------------------------------------------------------------------------
# Spreadsheets (xlsx / xlsm / ods / csv / tsv) → HTML tables in QtWebEngine
# ---------------------------------------------------------------------------


def _read_xlsx(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Return ``[(sheet_name, rows), …]`` from a workbook.

    Rows are flat lists of strings; ``None`` cells become empty
    strings. Read in *read-only* mode with ``data_only=True`` so we
    pull the cached formula values rather than the formulas
    themselves.  Capped at 5000 rows per sheet to keep the webview
    responsive on huge workbooks.
    """
    try:
        import openpyxl
    except Exception:
        return []
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception:
        return []
    out: list[tuple[str, list[list[str]]]] = []
    try:
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 5000:
                    break
                rows.append(["" if v is None else str(v) for v in row])
            out.append((ws.title, rows))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return out


def _read_ods(path: Path) -> list[tuple[str, list[list[str]]]]:
    try:
        from odf.opendocument import load
        from odf.table import Table, TableCell, TableRow
        from odf.text import P
    except Exception:
        return []
    try:
        doc = load(str(path))
    except Exception:
        return []
    out: list[tuple[str, list[list[str]]]] = []
    for table in doc.spreadsheet.getElementsByType(Table):
        name = table.getAttribute("name") or "Sheet"
        rows: list[list[str]] = []
        for row in table.getElementsByType(TableRow):
            if len(rows) >= 5000:
                break
            cells: list[str] = []
            for cell in row.getElementsByType(TableCell):
                # ODF cells repeat with ``number-columns-repeated`` -
                # honour it so wide rows aren't truncated.
                repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                paragraphs = cell.getElementsByType(P)
                text = " ".join(str(p) for p in paragraphs)
                cells.extend([text] * max(1, repeat))
            rows.append(cells)
        out.append((name, rows))
    return out


def _read_delimited(path: Path, *, delimiter: str) -> list[tuple[str, list[list[str]]]]:
    import csv

    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            rows = []
            for i, r in enumerate(csv.reader(f, delimiter=delimiter)):
                if i >= 5000:
                    break
                rows.append(list(r))
    except Exception:
        return []
    return [(path.stem or "Sheet", rows)]


_SPREADSHEET_CSS = """
body {
    background: #1a1d23;
    color: #c7cfd9;
    font-family: -apple-system, "Segoe UI", "Helvetica Neue",
                 "Inter", sans-serif;
    font-size: 13px;
    margin: 0;
    padding: 16px;
}
h2 {
    color: #e6eaef;
    font-size: 15px;
    font-weight: 600;
    margin: 12px 0 6px 0;
}
.sheet-meta {
    color: #7a8390;
    font-size: 11px;
    margin-bottom: 10px;
}
table {
    border-collapse: collapse;
    margin-bottom: 22px;
    background: #1f2228;
    border: 1px solid #2e323a;
    table-layout: auto;
}
th, td {
    padding: 4px 10px;
    border: 1px solid #2a2e36;
    text-align: left;
    vertical-align: top;
    white-space: pre-wrap;
    max-width: 360px;
    word-wrap: break-word;
}
thead {
    background: #262a32;
    position: sticky;
    top: 0;
}
thead th {
    color: #9aa3b0;
    font-weight: 600;
    border-bottom: 1px solid #3a3f48;
}
tr:nth-child(even) td {
    background: rgba(255,255,255,0.015);
}
mark {
    padding: 0 2px;
    border-radius: 2px;
}
"""


class SpreadsheetRenderPane(QWidget):
    """Render xlsx / xlsm / ods / csv / tsv as a styled HTML table.

    Each sheet becomes one ``<table>`` stacked vertically inside a
    QWebEngineView, with the first row treated as ``<thead>`` and the
    rest as ``<tbody>``. Substituted placeholders are wrapped in
    ``<mark>`` so the operator can spot what changed.

    Why a custom pane: routing spreadsheets through libreoffice → PDF
    (the previous OfficeRenderPane path) crops wide tables to the
    page width and rasterises them, text becomes blurry and rows
    bleed into each other. An HTML table reflows to the viewport,
    stays sharp at any zoom, and lets the user copy cell values.
    """

    add_to_map_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._web = None
        self._fallback = QLabel(
            "QtWebEngine is not installed. Install PySide6-Addons to "
            "render spreadsheets, or open the file in your browser."
        )
        self._fallback.setObjectName("Muted")
        self._fallback.setWordWrap(True)
        try:
            if _SelectablePdfWebView is None:
                raise ImportError("QtWebEngine widgets not available")
            self._web = _SelectablePdfWebView()
            self._web.add_to_map_requested.connect(
                self.add_to_map_requested.emit
            )
            lay.addWidget(self._web)
        except Exception:
            lay.addWidget(self._fallback)
        self._zoom = 1.0

    # ---- public API --------------------------------------------------------

    def is_available(self) -> bool:
        return self._web is not None

    def render_spreadsheet(
        self,
        path: Path,
        *,
        highlight_values: list[str],
        scheme: str = "per_mapping",
        color_keys: list[str] | None = None,
    ) -> None:
        if self._web is None:
            return
        sheets = self._read_sheets(path)
        html_text = self._build_html(
            sheets, highlight_values, scheme, color_keys
        )
        # ``setHtml(html, baseUrl)`` resolves relative resources against
        # the source's parent so any embedded images keep working;
        # spreadsheets here are inline-only, so the base url is
        # irrelevant but harmless.
        from PySide6.QtCore import QUrl  # local import to avoid hard dep

        self._web.setHtml(
            html_text, baseUrl=QUrl.fromLocalFile(str(path.parent) + "/")
        )

    # ---- zoom (mirrors HtmlRenderPane) ------------------------------------

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        self._set_zoom(1.0)

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.25, min(5.0, factor))
        self._zoom = factor
        if self._web is not None:
            self._web.setZoomFactor(factor)

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _read_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
        ext = path.suffix.lower()
        if ext in (".xlsx", ".xlsm"):
            return _read_xlsx(path)
        if ext == ".ods":
            return _read_ods(path)
        if ext == ".tsv":
            return _read_delimited(path, delimiter="\t")
        if ext == ".csv":
            return _read_delimited(path, delimiter=",")
        return []

    @staticmethod
    def _build_html(
        sheets: list[tuple[str, list[list[str]]]],
        highlight_values: list[str],
        scheme: str,
        color_keys: list[str] | None = None,
    ) -> str:
        # Longest-first replacement so e.g. "alice@example.com" wins
        # over "alice" when both happen to be in the highlight set.
        keys = list(color_keys or [])
        pairs: dict[str, str] = {}
        for idx, v in enumerate(highlight_values or []):
            if not v:
                continue
            ck = keys[idx] if idx < len(keys) and keys[idx] else v
            pairs.setdefault(v, ck)
        values = sorted(pairs.keys(), key=lambda x: -len(x))

        def mark_cell(text: str) -> str:
            esc = html.escape(text)
            for v in values:
                if scheme == "classic":
                    color = "#3FB95080"
                else:
                    color = _hashed_color(pairs[v]).name(QColor.NameFormat.HexArgb)
                esc_v = html.escape(v)
                esc = esc.replace(
                    esc_v,
                    f'<mark style="background:{color};">{esc_v}</mark>',
                )
            return esc

        parts: list[str] = []
        for name, rows in sheets:
            row_count = len(rows)
            col_count = max((len(r) for r in rows), default=0)
            parts.append(
                f"<h2>{html.escape(name)}</h2>"
                f"<div class=\"sheet-meta\">{row_count} rows × {col_count} cols</div>"
            )
            if not rows:
                parts.append("<p class=\"sheet-meta\">(empty)</p>")
                continue
            parts.append("<table>")
            head, *body = rows
            parts.append("<thead><tr>")
            for cell in head:
                parts.append(f"<th>{mark_cell(cell)}</th>")
            # Pad header to match the widest body row so columns align.
            for _ in range(col_count - len(head)):
                parts.append("<th></th>")
            parts.append("</tr></thead>")
            parts.append("<tbody>")
            for r in body:
                parts.append("<tr>")
                for cell in r:
                    parts.append(f"<td>{mark_cell(cell)}</td>")
                for _ in range(col_count - len(r)):
                    parts.append("<td></td>")
                parts.append("</tr>")
            parts.append("</tbody></table>")
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<style>{_SPREADSHEET_CSS}</style></head>'
            f'<body>{"".join(parts) or "<p>(no sheets)</p>"}</body></html>'
        )


# ---------------------------------------------------------------------------
# Format dispatch
# ---------------------------------------------------------------------------


def pick_pane_for(path: Path):
    """Return the *class* of the appropriate render pane for ``path``.
    Caller instantiates and calls the appropriate ``load_*`` method.
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        return PdfRenderPane
    if ext in _SPREADSHEET_EXTS:
        return SpreadsheetRenderPane
    if ext in _OFFICE_EXTS:
        return OfficeRenderPane
    if ext in _HTML_EXTS:
        return HtmlRenderPane
    if ext in _MARKDOWN_EXTS:
        return MarkdownRenderPane
    return PlainTextRenderPane


# ---------------------------------------------------------------------------
# Text-selectable PDF / Office rendering via Qt's built-in QPdfView.
#
# The rasterised PdfRenderPane is keyed for the diff view because it
# overlays per-event highlight rects on top of each page; QGraphicsView
# is the right tool for that. But it has no concept of glyphs, so the
# operator cannot click-and-drag to select text. The Build-preview
# tab does not need overlays (it is a single non-annotated render),
# so we use QPdfView there: native PDF text selection (Ctrl+C copies
# the selection to the clipboard), search, zoom, the works. Office
# documents go through the same libreoffice-to-PDF cache used by
# OfficeRenderPane, then feed the PDF into QPdfView.
# ---------------------------------------------------------------------------


try:
    from PySide6.QtWebEngineWidgets import QWebEngineView as _QWEV

    class _SelectablePdfWebView(_QWEV):
        """QWebEngineView wrapper that ALWAYS shows the anonymizer
        context menu on right-click (instead of Chromium's default
        back/forward/reload/save menu, which has nothing to do with
        the anonymization workflow).

        Reusable for any web-rendered content: the PDF viewer
        (PDF.js inside Chromium), HTML previews, and the HTML-table
        spreadsheet pane. The class name is a historical artefact —
        the resolution logic only cares that the page is a Chromium
        web view.

        Resolution order for the value to anonymize:

        1. Live text selection (drag-select then right-click).
        2. The DOM text at the cursor position, fetched via
           ``runJavaScript`` so the operator does NOT have to drag
           or type the word: a plain right-click on a word in the
           rendered content is enough.
        3. Manual input dialog as the last resort, in case the JS
           probe returns nothing (annotations, image-only pages).

        Emits :attr:`add_to_map_requested` carrying the cleaned
        string the operator wants to anonymize.
        """

        add_to_map_requested = Signal(str)

        def contextMenuEvent(self, event):  # type: ignore[override]
            global_pos = event.globalPos()
            pos = event.pos()
            # Chromium's built-in PDF viewer renders its content in
            # a cross-origin iframe that neither QWebEnginePage's
            # ``selectedText()`` nor a top-frame ``window.getSelection()``
            # can read. The reliable workaround is to trigger the
            # frame-level Copy action (Chromium routes this to the
            # focused frame, which IS the PDF viewer) and then read
            # the resulting clipboard text. We snapshot the previous
            # clipboard so we can restore it afterwards and avoid
            # surprising the operator with stolen clipboard state.
            from PySide6.QtCore import QTimer
            from PySide6.QtWebEngineCore import QWebEnginePage
            from PySide6.QtWidgets import QApplication

            cb = QApplication.clipboard()
            prev_text = cb.text() or ""

            self.page().triggerAction(QWebEnginePage.WebAction.Copy)

            def _after_copy() -> None:
                new_text = (cb.text() or "").strip()
                if new_text and new_text != prev_text.strip():
                    # Got a real selection — restore the old clipboard
                    # so the operator's previous Ctrl+C content isn't
                    # nuked by a side effect of our menu.
                    try:
                        cb.setText(prev_text)
                    except Exception:
                        pass
                    self._show_menu_for_text(new_text, global_pos)
                    return
                # No selection: fall back to "text under cursor" via
                # JS. Works on standard webpages and sometimes inside
                # the PDF viewer's text layer.
                js = (
                    "(function(){"
                    f"var el=document.elementFromPoint({pos.x()},{pos.y()});"
                    "if(!el)return '';"
                    "var node=el;"
                    "for(var i=0;i<5&&node;i++){"
                    "var t=(node.textContent||'').trim();"
                    "if(t.length>0&&t.length<200)return t;"
                    "node=node.parentElement;"
                    "}return '';})()"
                )
                self.page().runJavaScript(
                    js,
                    lambda result: self._on_text_at_point(
                        result or "", global_pos
                    ),
                )

            # Chromium needs a moment to flush the Copy through the
            # renderer process to the OS clipboard. 60 ms is enough
            # in practice without the menu feeling laggy.
            QTimer.singleShot(60, _after_copy)
            event.accept()

        def _on_text_at_point(self, text: str, global_pos) -> None:
            text = (text or "").strip()
            if text:
                self._show_menu_for_text(text, global_pos)
            else:
                self._show_menu_no_text(global_pos)

        def _show_menu_for_text(self, text: str, global_pos) -> None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu(self)
            act_copy = menu.addAction("Copy")
            menu.addSeparator()
            act_add = menu.addAction(_add_to_map_action_label(text))
            menu.addSeparator()
            act_manual = menu.addAction("Add to substitution map manually…")
            chosen = menu.exec(global_pos)
            if chosen is act_copy:
                self._copy_text(text)
            elif chosen is act_add:
                self.add_to_map_requested.emit(text)
            elif chosen is act_manual:
                self._prompt_manual_add()

        def _show_menu_no_text(self, global_pos) -> None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu(self)
            info = menu.addAction("No text detected under the cursor")
            info.setEnabled(False)
            menu.addSeparator()
            act_manual = menu.addAction("Add to substitution map manually…")
            chosen = menu.exec(global_pos)
            if chosen is act_manual:
                self._prompt_manual_add()

        def _copy_text(self, text: str) -> None:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(text)

        def _prompt_manual_add(self) -> None:
            """Fallback when the operator wants a value the DOM
            probe didn't find. Same target as the auto path —
            placeholder defaults to XXXX, category 'other'."""
            text = _prompt_manual_add_to_map(self)
            if text:
                self.add_to_map_requested.emit(text)
except Exception:
    _SelectablePdfWebView = None  # type: ignore[assignment]


class SelectablePdfRenderPane(QWidget):
    """PDF viewer with native text selection.

    Qt's bundled :class:`QPdfView` is a read-only viewer with no
    text-selection API in Qt 6.x, so this pane uses Chromium's
    in-built PDF viewer (PDF.js, exposed through QtWebEngine's
    ``PdfViewerEnabled`` attribute) instead. That gives free
    drag-select, Ctrl+C copy, on-page search, page navigation, and
    zoom controls without any custom mouse plumbing.

    Provides the same ``load_pdf`` / zoom / fit-to-window public
    surface as :class:`PdfRenderPane` so callers can swap one for
    the other transparently.

    Emits :attr:`add_to_map_requested` when the operator highlights
    text in the rendered PDF and picks "Add to substitution map" in
    the right-click context menu. Parent widgets connect that
    signal to their AppState writer.
    """

    add_to_map_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        try:
            from PySide6.QtWebEngineCore import QWebEngineSettings

            self._web = _SelectablePdfWebView(self)
            settings = self._web.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.PdfViewerEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.PluginsEnabled, True
            )
            self._web.add_to_map_requested.connect(
                self.add_to_map_requested.emit
            )
            lay.addWidget(self._web)
        except Exception:
            self._web = None
            from PySide6.QtWidgets import QLabel

            fallback = QLabel(
                "PDF text selection requires QtWebEngine "
                "(install PySide6-Addons)."
            )
            fallback.setObjectName("Muted")
            fallback.setWordWrap(True)
            lay.addWidget(fallback)
        self._zoom = 1.0
        self._loaded_path: Optional[Path] = None

    # ---- public API matching PdfRenderPane ---------------------------------

    def load_pdf(
        self,
        path: Path,
        *,
        events: Optional[list[dict]] = None,
        side: str = "left",
    ) -> bool:
        """Load ``path`` into the embedded PDF.js viewer.

        When ``events`` is supplied (the same per-event rect dicts the
        rasterised :class:`PdfRenderPane` consumes), each event's
        rectangles are pre-baked into the PDF as native highlight
        annotations on a temp copy of the file. PDF.js renders the
        annotations as translucent boxes on top of the page; text
        selection still works because annotations live in their own
        layer above the text glyphs, never under them.

        ``side`` selects the rect set per event ("left" → ``rects``,
        "right" → ``rects_right``), so the same call signature works
        for both halves of a future selectable diff view.
        """
        from PySide6.QtCore import QUrl

        if self._web is None:
            return False
        if not path.exists():
            return False
        target = path
        if events:
            try:
                target = self._bake_highlights(path, events, side)
            except Exception:
                # Fall back to the un-annotated PDF rather than failing
                # the preview entirely. Selection still works.
                target = path
        try:
            self._web.load(QUrl.fromLocalFile(str(target)))
        except Exception:
            return False
        self._loaded_path = target
        return True

    @staticmethod
    def _bake_highlights(
        src: Path, events: list[dict], side: str
    ) -> Path:
        """Return a temp PDF with one highlight annotation per
        event rect. Cached on disk under
        ``<tempfile.gettempdir()>/anondiff/highlight`` keyed by
        (source, mtime, events) so repeated views of the same
        combination reuse the artifact."""
        try:
            import fitz
        except Exception:
            return src
        rect_key = side
        try:
            mtime = src.stat().st_mtime_ns
        except OSError:
            mtime = 0
        # Stable cache key: source path + mtime + per-event rects.
        h = hashlib.sha256()
        h.update(f"{src.resolve()}|{mtime}|{rect_key}".encode("utf-8"))
        for ev in events:
            page = ev.get("page")
            rects = (
                ev.get("rects") if rect_key == "left"
                else ev.get("rects_right")
            ) or []
            from_val = ev.get("from") or ev.get("value") or ""
            h.update(f"|{page}:{from_val}".encode("utf-8"))
            for r in rects:
                h.update("|".join(str(c) for c in r).encode("utf-8"))
        cache_root = Path(tempfile.gettempdir()) / "anondiff" / "highlight"
        cache_root.mkdir(parents=True, exist_ok=True)
        out = cache_root / f"{h.hexdigest()[:24]}_{src.stem}.pdf"
        if out.exists():
            return out
        try:
            doc = fitz.open(str(src))
        except Exception:
            return src
        try:
            for ev in events:
                page_idx = ev.get("page")
                rects = (
                    ev.get("rects") if rect_key == "left"
                    else ev.get("rects_right")
                ) or []
                if page_idx is None or not rects or page_idx >= doc.page_count:
                    continue
                page = doc.load_page(page_idx)
                key = ev.get("from") or ev.get("value") or ""
                color = _hashed_color(key)
                rgb = (
                    color.red() / 255.0,
                    color.green() / 255.0,
                    color.blue() / 255.0,
                )
                for r in rects:
                    if len(r) != 4:
                        continue
                    rect = fitz.Rect(*[float(c) for c in r])
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=rgb)
                    annot.update(opacity=0.45)
            doc.save(str(out), garbage=3, deflate=True, clean=True)
        except Exception:
            try:
                doc.close()
            except Exception:
                pass
            return src
        doc.close()
        return out

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.2)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.2)

    def zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def fit_to_window(self) -> None:
        # PDF.js manages its own page-fit logic when the view is
        # resized, so we just reset to 100% and let the embedded
        # toolbar do the rest.
        self._set_zoom(1.0)

    def _set_zoom(self, factor: float) -> None:
        factor = max(0.25, min(5.0, factor))
        self._zoom = factor
        if self._web is not None:
            self._web.setZoomFactor(factor)


class SelectableOfficeRenderPane(SelectablePdfRenderPane):
    """Same as :class:`OfficeRenderPane` but rendered through
    :class:`SelectablePdfRenderPane` so text selection works on
    docx / pptx / odt previews too. The libreoffice-to-PDF cache is
    shared with :class:`OfficeRenderPane`.
    """

    _CACHE_ROOT = OfficeRenderPane._CACHE_ROOT

    def load_office(
        self,
        path: Path,
        *,
        events: Optional[list[dict]] = None,
        side: str = "left",
    ) -> bool:
        pdf_path = OfficeRenderPane._convert_to_pdf(path)
        if pdf_path is None:
            return False
        return self.load_pdf(pdf_path, events=events, side=side)


def pick_selectable_pane_for(path: Path):
    """Same role as :func:`pick_pane_for`, but returns the
    text-selectable variants for PDF / Office (so the operator can
    drag-select text in the Build-preview render). Other formats
    fall back to the existing panes, which already support text
    selection natively (QTextEdit / QWebEngineView)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return SelectablePdfRenderPane
    if ext in _OFFICE_EXTS:
        return SelectableOfficeRenderPane
    return pick_pane_for(path)


__all__ = [
    "PdfRenderPane",
    "OfficeRenderPane",
    "HtmlRenderPane",
    "MarkdownRenderPane",
    "SpreadsheetRenderPane",
    "PlainTextRenderPane",
    "SelectablePdfRenderPane",
    "SelectableOfficeRenderPane",
    "pick_pane_for",
    "pick_selectable_pane_for",
    "derive_right_rects",
]
