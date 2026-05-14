"""Side-by-side rendered diff with per-mapping unique-color highlights.

For every supported format the original and anonymised documents are
rendered with their layout intact (PDF rasterised with PyMuPDF; office
docs converted to PDF via libreoffice; HTML via QWebEngineView; plain
text falls back to the legacy plaintext view). Highlights are overlaid
on the substituted regions using rect bookkeeping the PDF adapter now
persists in ``applied_substitutions.json``.

Navigation:
* Left-hand list of files (always visible) lets the user jump between
  documents in the project.
* Toolbar arrows ``← Prev file`` / ``Next file →`` cycle between
  files in the same list.
* The legacy ``← Prev sub`` / ``Next sub →`` buttons still jump
  between substitutions inside the current file, but only on the
  plaintext fallback (the rendered panes scroll continuously).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .icons import icon
from ._render_panes import (
    HtmlRenderPane,
    MarkdownRenderPane,
    OfficeRenderPane,
    PdfRenderPane,
    PlainTextRenderPane,
    SpreadsheetRenderPane,
    derive_right_rects,
    pick_pane_for,
)
from .state import AppState


class DiffView(QWidget):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._applied: dict = {}
        self._scheme = "per_mapping"
        self._category_filter = ""
        self._current_file_index = 0
        self._files: list[dict] = []
        # Bookkeeping for the synced-scroll feature: every active
        # mount installs a pair of valueChanged listeners on the
        # left and right scrollbars; we keep the QMetaObject.Connection
        # objects so the next mount can disconnect them cleanly
        # (otherwise we would leak listeners and create a feedback
        # storm).
        self._sync_connections: list = []
        self._sync_enabled: bool = True
        self._sync_in_progress: bool = False

        # ---- toolbar -------------------------------------------------------
        self.scheme_combo = QComboBox()
        self.scheme_combo.addItem("Per-mapping color", "per_mapping")
        self.scheme_combo.addItem("Classic red/green", "classic")
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        self.cat_combo = QComboBox()
        self.cat_combo.addItem("All categories", "")
        self.cat_combo.currentIndexChanged.connect(self._on_cat_changed)

        prev_btn = QPushButton("⟵ Prev file")
        next_btn = QPushButton("Next file ⟶")
        prev_btn.clicked.connect(lambda: self._jump_file(-1))
        next_btn.clicked.connect(lambda: self._jump_file(+1))

        zoom_icon_size = QSize(16, 16)
        zoom_out_btn = QPushButton(icon("zoom-out"), "")
        zoom_out_btn.setIconSize(zoom_icon_size)
        zoom_out_btn.setToolTip("Zoom out (Ctrl+wheel on the document also works)")
        zoom_out_btn.setFixedSize(32, 28)
        zoom_in_btn = QPushButton(icon("zoom-in"), "")
        zoom_in_btn.setIconSize(zoom_icon_size)
        zoom_in_btn.setToolTip("Zoom in (Ctrl+wheel on the document also works)")
        zoom_in_btn.setFixedSize(32, 28)
        zoom_fit_btn = QPushButton(icon("maximize"), " Fit")
        zoom_fit_btn.setIconSize(zoom_icon_size)
        zoom_fit_btn.setToolTip("Fit page to window width (auto-refits on resize)")
        zoom_100_btn = QPushButton("100%")
        zoom_100_btn.setToolTip("Reset zoom to 100%")
        zoom_out_btn.clicked.connect(lambda: self._zoom_both("out"))
        zoom_in_btn.clicked.connect(lambda: self._zoom_both("in"))
        zoom_fit_btn.clicked.connect(lambda: self._zoom_both("fit"))
        zoom_100_btn.clicked.connect(lambda: self._zoom_both("reset"))

        # Synced scroll: original and anonymized share the same page
        # geometry (text layout is preserved), so scrolling them
        # together is the natural compare-side-by-side experience.
        # Toggle it off when the operator wants to inspect a single
        # side without the partner moving.
        self.sync_scroll_chk = QCheckBox("Sync scroll")
        self.sync_scroll_chk.setChecked(True)
        self.sync_scroll_chk.setToolTip(
            "Scroll left and right panes together. Toggle off to "
            "scroll one side without the other moving."
        )
        self.sync_scroll_chk.toggled.connect(self._on_sync_toggled)

        top = QHBoxLayout()
        top.addWidget(QLabel("Scheme:"))
        top.addWidget(self.scheme_combo)
        top.addWidget(QLabel("Category:"))
        top.addWidget(self.cat_combo)
        top.addWidget(self.sync_scroll_chk)
        top.addStretch()
        top.addWidget(zoom_out_btn)
        top.addWidget(zoom_in_btn)
        top.addWidget(zoom_fit_btn)
        top.addWidget(zoom_100_btn)
        top.addWidget(prev_btn)
        top.addWidget(next_btn)

        # ---- file tree (left) ---------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("File", "Subs"))
        self.tree.setColumnWidth(0, 240)
        self.tree.itemSelectionChanged.connect(self._on_file_selected_from_tree)

        # ---- two render-pane stacks (right) -------------------------------
        # Each side holds a ``QStackedWidget`` of one widget per pane
        # type (PDF / Office / HTML / Plain). We swap to the right
        # one based on the file's extension. Keeping the widgets
        # alive across file switches avoids re-creating QtWebEngine
        # instances every time.
        self.left_stack = QStackedWidget()
        self.right_stack = QStackedWidget()
        self._left_panes: dict[type, QWidget] = {}
        self._right_panes: dict[type, QWidget] = {}

        editors = QSplitter(Qt.Orientation.Horizontal)
        editors.addWidget(self.left_stack)
        editors.addWidget(self.right_stack)
        editors.setSizes([400, 400])
        editors.setHandleWidth(6)

        outer = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.tree)
        outer.addWidget(editors)
        outer.setSizes([240, 800])

        # ---- inspector -----------------------------------------------------
        self.inspector = QLabel(
            "Pick a file on the left to see the rendered before / after."
        )
        self.inspector.setObjectName("Muted")
        self.inspector.setWordWrap(True)
        self.inspector.setMinimumHeight(40)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.addLayout(top)
        root.addWidget(outer, 1)
        root.addWidget(self.inspector)

        state.apply_report_changed.connect(lambda _r: self.reload())
        self.reload()

    # ---- public API ---------------------------------------------------------

    def reload(self) -> None:
        if self.state.project is None:
            self.tree.clear()
            self._files = []
            return
        try:
            self._applied = json.loads(
                self.state.project.applied_path.read_text(encoding="utf-8")
            )
        except Exception:
            self._applied = {}
        self._files = [
            f for f in (self._applied.get("files") or []) if f.get("events")
        ]
        self._populate_tree()
        self._populate_categories()
        if self._files:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    # ---- tree ---------------------------------------------------------------

    def _populate_tree(self) -> None:
        self.tree.clear()
        for f in self._files:
            it = QTreeWidgetItem(self.tree)
            it.setText(0, str(f.get("file", "")))
            it.setText(1, str(len(f.get("events", []))))
            it.setData(0, Qt.ItemDataRole.UserRole, f)

    def _populate_categories(self) -> None:
        cats = set()
        for f in self._files:
            for e in f.get("events") or []:
                c = e.get("category")
                if c:
                    cats.add(c)
        cur = self.cat_combo.currentData() or ""
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem("All categories", "")
        for c in sorted(cats):
            self.cat_combo.addItem(c, c)
        idx = self.cat_combo.findData(cur)
        if idx >= 0:
            self.cat_combo.setCurrentIndex(idx)
        self.cat_combo.blockSignals(False)

    # ---- file selection -----------------------------------------------------

    def _on_file_selected_from_tree(self) -> None:
        sel = self.tree.currentItem()
        if not sel:
            return
        idx = self.tree.indexOfTopLevelItem(sel)
        if idx >= 0:
            self._current_file_index = idx
        self._render_current_file()

    def _jump_file(self, direction: int) -> None:
        if not self._files:
            return
        new_idx = (self._current_file_index + direction) % len(self._files)
        self.tree.setCurrentItem(self.tree.topLevelItem(new_idx))

    def _render_current_file(self) -> None:
        if not self._files:
            return
        idx = self._current_file_index
        if idx >= len(self._files):
            return
        f = self._files[idx]
        rel = str(f.get("file", ""))
        if not rel or self.state.project is None:
            return
        if self.state.project.mode == "single":
            src = self.state.project.input_paths[0]
            dst = self.state.project.output_path_for(_FakeScanned(src))
        elif self.state.project.mode == "multi":
            # ``input_paths`` is a list of files (not a folder); match by basename.
            name = Path(rel).name
            src = next(
                (p for p in self.state.project.input_paths if p.name == name),
                self.state.project.input_paths[0],
            )
            dst = self.state.project.output_dir / name
        else:  # folder
            in_root = self.state.project.input_paths[0]
            src = in_root / rel
            dst = self.state.project.output_dir / rel

        events = list(f.get("events") or [])
        # Compute right-side rects for PDFs (and Office that go through
        # PDF cache) by re-searching the placeholder text in the
        # anonymised file.
        try:
            events = derive_right_rects(events, dst if dst.suffix.lower() == ".pdf" else dst)
        except Exception:
            pass

        # Annotate each event with the canonical "value" used by the
        # plaintext fallback's color scheme.
        for ev in events:
            ev.setdefault("value", ev.get("from") or "")

        self._mount_panes(src, dst, events)

        self.inspector.setText(
            f"<b>{rel}</b> · {len(events)} substitution(s) · "
            f"<i>left:</i> original · <i>right:</i> anonymized"
        )
        self.inspector.setTextFormat(Qt.TextFormat.RichText)

    def _mount_panes(self, src: Path, dst: Path, events: list[dict]) -> None:
        """Build / reuse the panes for the file's extension."""
        pane_cls = pick_pane_for(src)
        # The right side may be a different extension if the user
        # picked an extra-export format; pick by extension of dst.
        right_cls = pick_pane_for(dst)

        left = self._get_or_create_pane(self._left_panes, self.left_stack, pane_cls)
        right = self._get_or_create_pane(self._right_panes, self.right_stack, right_cls)
        self.left_stack.setCurrentWidget(left)
        self.right_stack.setCurrentWidget(right)

        self._load_into_pane(left, src, events, side="left")
        self._load_into_pane(right, dst, events, side="right")
        self._install_scroll_sync(left, right)

    # ---- synced scroll ------------------------------------------------------

    @staticmethod
    def _scroll_target(pane) -> Optional[QAbstractScrollArea]:
        """Return the inner scrollable widget for a pane, or None
        when the pane uses a QtWebEngine view (those expose only
        JS-side scroll APIs and aren't worth syncing for the
        rasterised diff)."""
        for attr in ("view", "edit"):
            w = getattr(pane, attr, None)
            if isinstance(w, QAbstractScrollArea):
                return w
        return None

    def _install_scroll_sync(self, left, right) -> None:
        """Wire valueChanged on both vertical scrollbars so a drag
        on either side moves the partner. Uses fractional sync so
        small differences in document height (e.g. PDF re-derive
        adds a footer) don't cause drift at the bottom."""
        for sb, slot in self._sync_connections:
            try:
                sb.valueChanged.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._sync_connections.clear()
        if not self._sync_enabled:
            return
        lw = self._scroll_target(left)
        rw = self._scroll_target(right)
        if lw is None or rw is None:
            return
        lv = lw.verticalScrollBar()
        rv = rw.verticalScrollBar()

        def make_slot(src, dst):
            def _slot(_value: int) -> None:
                if self._sync_in_progress or not self._sync_enabled:
                    return
                self._sync_in_progress = True
                try:
                    smin, smax = src.minimum(), src.maximum()
                    if smax <= smin:
                        return
                    frac = (src.value() - smin) / (smax - smin)
                    dmin, dmax = dst.minimum(), dst.maximum()
                    dst.setValue(round(dmin + frac * (dmax - dmin)))
                finally:
                    self._sync_in_progress = False
            return _slot

        l_slot = make_slot(lv, rv)
        r_slot = make_slot(rv, lv)
        lv.valueChanged.connect(l_slot)
        rv.valueChanged.connect(r_slot)
        self._sync_connections.append((lv, l_slot))
        self._sync_connections.append((rv, r_slot))

    def _on_sync_toggled(self, on: bool) -> None:
        self._sync_enabled = bool(on)
        if not on:
            for sb, slot in self._sync_connections:
                try:
                    sb.valueChanged.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._sync_connections.clear()
        else:
            # Re-arm on the currently-mounted panes.
            left = self.left_stack.currentWidget()
            right = self.right_stack.currentWidget()
            if left is not None and right is not None:
                self._install_scroll_sync(left, right)

    def _get_or_create_pane(
        self,
        registry: dict,
        stack: QStackedWidget,
        cls: type,
    ) -> QWidget:
        if cls in registry:
            return registry[cls]
        widget = cls()
        registry[cls] = widget
        stack.addWidget(widget)
        return widget

    def _load_into_pane(
        self, pane: QWidget, path: Path, events: list[dict], *, side: str
    ) -> None:
        if isinstance(pane, PdfRenderPane) and not isinstance(pane, OfficeRenderPane):
            ok = pane.load_pdf(path) if path.exists() else False
            if not ok:
                # PDF couldn't load (maybe still being written): leave
                # the scene empty. Highlights still pile up but with no
                # page background.
                pass
            pane.set_scheme(self._scheme)
            pane.set_category_filter(self._category_filter)
            pane.set_events(events, side=side)
        elif isinstance(pane, OfficeRenderPane):
            ok = pane.load_office(path) if path.exists() else False
            if not ok:
                pass
            pane.set_scheme(self._scheme)
            pane.set_category_filter(self._category_filter)
            pane.set_events(events, side=side)
        elif isinstance(pane, HtmlRenderPane):
            try:
                txt = path.read_text(encoding="utf-8") if path.exists() else ""
            except Exception:
                txt = ""
            highlight_values, color_keys = self._side_values_and_keys(events, side)
            pane.render_html(
                txt,
                highlight_values=highlight_values,
                scheme=self._scheme,
                color_keys=color_keys,
            )
        elif isinstance(pane, MarkdownRenderPane):
            try:
                txt = path.read_text(encoding="utf-8") if path.exists() else ""
            except Exception:
                txt = ""
            highlight_values, color_keys = self._side_values_and_keys(events, side)
            pane.render_markdown(
                txt,
                highlight_values=highlight_values,
                scheme=self._scheme,
                color_keys=color_keys,
            )
        elif isinstance(pane, SpreadsheetRenderPane):
            highlight_values, color_keys = self._side_values_and_keys(events, side)
            if path.exists():
                pane.render_spreadsheet(
                    path,
                    highlight_values=highlight_values,
                    scheme=self._scheme,
                    color_keys=color_keys,
                )
        elif isinstance(pane, PlainTextRenderPane):
            text, seg_offsets = self._extract_text_with_offsets(path)
            pane.set_text(text)

            def _abs(seg_id: str, off: int) -> int:
                return int(seg_offsets.get(seg_id, 0) + (off or 0))

            spans = [
                {
                    "off": _abs(
                        ev.get("seg_id", ""),
                        ev.get("orig_off") if side == "left" else ev.get("anon_off"),
                    ),
                    "len": ev.get("orig_len") if side == "left" else ev.get("anon_len"),
                    "value": ev.get("from") or "",
                    "to": ev.get("to") or "",
                    "category": ev.get("category") or "",
                    "mapping_id": ev.get("mapping_id") or "",
                }
                for ev in events
            ]
            pane.set_scheme(self._scheme)
            pane.set_category_filter(self._category_filter)
            pane.set_spans(spans)

    @staticmethod
    def _side_values_and_keys(
        events: list[dict], side: str
    ) -> tuple[list[str], list[str]]:
        """For text-based panes, return the strings to highlight on a
        given side together with a parallel list of color keys.

        The color key is always the original ``from`` value, so the
        same mapping ends up with the same colour on both sides of the
        diff (left highlights ``from``, right highlights ``to``)."""
        values: list[str] = []
        keys: list[str] = []
        for ev in events:
            display = ev.get("from") if side == "left" else ev.get("to")
            if not display:
                continue
            values.append(display)
            keys.append(ev.get("from") or display)
        return values, keys

    @staticmethod
    def _extract_text(path: Path) -> str:
        text, _ = DiffView._extract_text_with_offsets(path)
        return text

    @staticmethod
    def _extract_text_with_offsets(path: Path) -> tuple[str, dict[str, int]]:
        try:
            from anonymize.format_adapters import get_adapter

            ad = get_adapter(path)
            segs = ad.extract(path)
            offsets: dict[str, int] = {}
            parts: list[str] = []
            cursor = 0
            for s in segs:
                offsets[s.seg_id] = cursor
                parts.append(s.text)
                cursor += len(s.text) + 1
            return ("\n".join(parts), offsets)
        except Exception:
            return (f"<cannot extract: {path}>", {})

    # ---- toolbar handlers ---------------------------------------------------

    def _zoom_both(self, action: str) -> None:
        """Apply the same zoom action to whichever panes are currently mounted."""
        for stack in (self.left_stack, self.right_stack):
            pane = stack.currentWidget()
            if pane is None:
                continue
            fn = {
                "in": getattr(pane, "zoom_in", None),
                "out": getattr(pane, "zoom_out", None),
                "fit": getattr(pane, "fit_to_window", None),
                "reset": getattr(pane, "zoom_reset", None),
            }.get(action)
            if callable(fn):
                fn()

    def _on_scheme_changed(self) -> None:
        self._scheme = self.scheme_combo.currentData() or "per_mapping"
        self._render_current_file()

    def _on_cat_changed(self) -> None:
        self._category_filter = self.cat_combo.currentData() or ""
        self._render_current_file()


class _FakeScanned:
    """Minimal stand-in for ScannedFile used by Project.output_path_for."""

    def __init__(self, src: Path) -> None:
        self.path = src
        self.rel = Path(src.name)


__all__ = ["DiffView"]
