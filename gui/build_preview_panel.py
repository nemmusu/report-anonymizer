"""Build-preview tab: 'this is what Build will produce, OK?'

Last stop before the operator clicks Build. The panel renders the
current input file(s) with BOTH the textual substitutions from the
substitution map AND the image redactions from
``image_redactions.yml`` baked in, so the operator sees the actual
final-output pixels (modulo pandoc round-trips for the optional
extra export formats).

Two action buttons at the bottom:

* **Build** runs ``apply -> build -> verify`` so the redacted
  output lands on disk.
* **Back** lets the operator hop back to the Text or Images tab to
  add a missing mapping or tweak a redaction rect, then re-enter the
  preview to confirm.

The preview is read-only (no editing here). Multi-file projects get
a file picker on top so the operator can scrub through every input
file before approving.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anonymize.format_adapters import get_adapter
from anonymize.format_adapters.base import SubstitutionRule
from anonymize.image_inventory import (
    ImageDecision,
    ImageInventory,
    ImageRedactions,
    load_decisions,
    load_inventory,
)
from anonymize.sub_map import SubstitutionMap

from ._render_panes import (
    HtmlRenderPane,
    MarkdownRenderPane,
    OfficeRenderPane,
    PdfRenderPane,
    PlainTextRenderPane,
    SelectableOfficeRenderPane,
    SelectablePdfRenderPane,
    SpreadsheetRenderPane,
    pick_selectable_pane_for,
)
from .icons import icon
from .state import AppState
from .theme import PALETTE


# Cache directory for preview artefacts. Lives next to the existing
# /tmp/anondiff/preview tree the Text tab already uses, but with a
# different leaf to avoid collisions on the cache key (we add the
# image-decisions hash to the key).
_BUILD_PREVIEW_CACHE = Path(tempfile.gettempdir()) / "anondiff" / "build_preview"


class BuildPreviewPanel(QWidget):
    """Final preview before Build."""

    build_requested = Signal()
    back_to_text_requested = Signal()
    back_to_images_requested = Signal()

    def __init__(self, state: AppState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.state = state
        self._render_panes: dict[type, QWidget] = {}
        # External callable that returns the live, in-memory image
        # redactions (the Image-review panel sets this so the
        # preview reflects unsaved edits). Falls back to the YAML
        # on disk when the hook is not wired or returns None.
        self._live_decisions_provider: Optional[
            Callable[[], Optional[ImageRedactions]]
        ] = None
        # Coalesce rapid state changes (text rule edits, rect
        # drags) into a single render so we don't burn CPU on each
        # keystroke.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self.refresh)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Header banner: "Preview of build" + short copy.
        title = QLabel("Preview of build")
        title.setStyleSheet(
            f"color: {PALETTE['text']}; font-size: 16px; font-weight: 600;"
        )
        subtitle = QLabel(
            "Review what Apply will write. If it looks right, click "
            "Build to materialise the output. To change something, hop "
            "back to Text candidates (add or edit substitutions) or to "
            "Images (re-edit a redaction)."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"color: {PALETTE['text_dim']}; padding: 0 0 6px 0;"
        )
        root.addWidget(title)
        root.addWidget(subtitle)

        # File picker for multi-file projects. Hidden in single-file
        # mode for the same reason the image panel hides its file
        # filter: a picker with one option is just clutter.
        file_row = QHBoxLayout()
        self._file_label = QLabel("File:")
        self._file_combo = QComboBox(self)
        self._file_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._file_combo.currentIndexChanged.connect(self._on_file_changed)
        file_row.addWidget(self._file_label)
        file_row.addWidget(self._file_combo, 1)
        root.addLayout(file_row)

        # Render-pane stack.
        self._stack = QStackedWidget(self)
        self._stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._stack, 1)

        # Status / errors label between preview and action buttons.
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {PALETTE['text_dim']}; padding: 4px;"
        )
        root.addWidget(self._status)

        # Bottom action bar: Back-to-text / back-to-images on the
        # left, Build on the right.
        bottom = QHBoxLayout()
        self._back_text_btn = QPushButton(icon("history"), "Back to text candidates")
        self._back_text_btn.clicked.connect(
            lambda: self.back_to_text_requested.emit()
        )
        self._back_imgs_btn = QPushButton(icon("history"), "Back to images")
        self._back_imgs_btn.clicked.connect(
            lambda: self.back_to_images_requested.emit()
        )
        self._refresh_btn = QPushButton(icon("refresh"), "Refresh preview")
        self._refresh_btn.setToolTip(
            "Rebuild the preview from the latest state on disk. Useful "
            "if you edited substitution_map.yml or image_redactions.yml "
            "outside the GUI."
        )
        self._refresh_btn.clicked.connect(self.refresh)
        self._build_btn = QPushButton(icon("play"), "  Build")
        self._build_btn.setObjectName("PrimaryButton")
        self._build_btn.setToolTip(
            "Run apply -> build -> verify so the redacted output lands "
            "on disk."
        )
        self._build_btn.clicked.connect(lambda: self.build_requested.emit())

        bottom.addWidget(self._back_text_btn)
        bottom.addWidget(self._back_imgs_btn)
        bottom.addWidget(self._refresh_btn)
        bottom.addStretch()
        bottom.addWidget(self._build_btn)
        root.addLayout(bottom)

    # ---- public API ----

    def refresh(self) -> None:
        """Rebuild the file picker + the preview pane."""
        self._populate_file_combo()
        self._render_current()

    def schedule_refresh(self) -> None:
        """Coalesce rapid state changes into a single render. Connect
        upstream signals (state.candidates_changed, image rect
        edits, …) here so the preview stays live as the operator
        works without burning CPU on every event.

        No-op while the panel is disabled (i.e. before the operator
        has reached the Build-preview stage at least once). This
        keeps the apply pipeline cold during the early text-review
        phase where the preview would be wasted work.
        """
        if not self.isEnabled():
            return
        self._refresh_timer.start()

    def set_live_decisions_provider(
        self, provider: Optional[Callable[[], Optional[ImageRedactions]]]
    ) -> None:
        """Register a callable that returns the operator's in-memory
        image redactions. The build preview reads from this in place
        of the on-disk YAML so it reflects unsaved edits."""
        self._live_decisions_provider = provider

    def _on_add_to_map_requested(self, value: str) -> None:
        """Right-click "Add to substitution map" handler.

        Inserts ``value -> XXXX`` under the ``other`` category, since
        we don't ask the operator to classify on the fly. Map
        entries are the source of truth for the preview, so the
        next refresh (debounced via the live-preview timer) shows
        the new substitution in place AND highlights it with the
        same per-mapping colour as everything else. A toast confirms
        the action since the context menu disappears with no other
        side-effect.
        """
        from .toast import Toaster

        proj = self.state.project
        if proj is None:
            return
        clean = (value or "").strip()
        if not clean:
            return
        smap = self.state.smap
        if smap is None:
            try:
                smap = SubstitutionMap.load(proj.map_path)
                self.state.smap = smap
            except Exception:
                return
        existing = smap.find(clean)
        if existing is not None:
            Toaster.notify(
                "Already mapped",
                f"'{clean[:60]}' is already under '{existing[0]}'",
                kind="info",
            )
            return
        smap.add("other", clean, "XXXX")
        try:
            smap.save()
        except Exception as e:
            Toaster.notify("Save failed", str(e)[:120], kind="err")
            return
        self.state.map_changed.emit(smap)
        Toaster.notify(
            "Added to substitution map",
            f"'{clean[:60]}' → XXXX (category: other)",
            kind="ok",
        )

    def _populate_file_combo(self) -> None:
        proj = self.state.project
        prev = self._file_combo.currentData() if self._file_combo.count() else None
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        files = self._discover_input_files()
        for p in files:
            self._file_combo.addItem(p.name, str(p))
        if prev:
            idx = self._file_combo.findData(prev)
            if idx >= 0:
                self._file_combo.setCurrentIndex(idx)
        single = len(files) <= 1
        self._file_combo.setVisible(not single)
        self._file_label.setVisible(not single)
        self._file_combo.blockSignals(False)

    def _on_file_changed(self) -> None:
        self._render_current()

    def _current_file(self) -> Optional[Path]:
        proj = self.state.project
        if proj is None:
            return None
        idx = self._file_combo.currentIndex()
        if idx >= 0:
            data = self._file_combo.itemData(idx)
            if data:
                return Path(data)
        files = self._discover_input_files()
        return files[0] if files else None

    def _discover_input_files(self) -> list[Path]:
        proj = self.state.project
        if proj is None or not proj.input_paths:
            return []
        if proj.mode == "folder":
            root = proj.input_paths[0]
            if root.is_dir():
                return sorted(
                    p for p in root.rglob("*")
                    if p.is_file()
                    and p.suffix.lower() in {
                        ".pdf", ".md", ".txt", ".html", ".docx",
                        ".pptx", ".odt", ".rtf",
                    }
                )
            return []
        if proj.mode == "multi":
            return [p for p in proj.input_paths if p.is_file()]
        return [proj.input_paths[0]]

    # ---- rendering ----

    def _render_current(self) -> None:
        src = self._current_file()
        if src is None or not src.exists():
            self._status.setText("No input file to preview.")
            return
        try:
            preview_path = self._build_preview_file(src)
        except Exception as e:
            self._status.setText(f"Preview generation failed: {e}")
            return
        if preview_path is None:
            self._status.setText("Could not build preview for this file.")
            return
        self._show_in_pane(preview_path)
        self._status.setText(
            f"Showing preview of {src.name}.  "
            "If this is what you want as output, click Build."
        )

    def _show_in_pane(self, path: Path) -> None:
        # Use the selectable PDF / Office variants so the operator
        # can drag-select text in the rendered preview and Ctrl+C
        # it to the clipboard. Other formats (HTML / Markdown /
        # Spreadsheet / PlainText) already select natively via
        # QTextEdit / QWebEngineView.
        pane_cls = pick_selectable_pane_for(path)
        pane = self._render_panes.get(pane_cls)
        if pane is None:
            pane = pane_cls()
            self._render_panes[pane_cls] = pane
            self._stack.addWidget(pane)
            # Right-click "Add to substitution map" → forward the
            # selected string to AppState. Every selectable pane
            # (PDF / Office / HTML / Markdown / Spreadsheet /
            # plaintext) exposes the same ``add_to_map_requested``
            # signal, so we connect by capability rather than class.
            if hasattr(pane, "add_to_map_requested"):
                pane.add_to_map_requested.connect(
                    self._on_add_to_map_requested
                )
        self._stack.setCurrentWidget(pane)

        if isinstance(pane, SelectableOfficeRenderPane):
            pane.load_office(path)
            try:
                pane.fit_to_window()
            except Exception:
                pass
        elif isinstance(pane, SelectablePdfRenderPane):
            pane.load_pdf(path)
            try:
                pane.fit_to_window()
            except Exception:
                pass
        elif isinstance(pane, OfficeRenderPane):
            pane.load_office(path)
            try:
                pane.fit_to_window()
            except Exception:
                pass
        elif isinstance(pane, PdfRenderPane):
            pane.load_pdf(path)
            try:
                pane.fit_to_window()
            except Exception:
                pass
        elif isinstance(pane, HtmlRenderPane):
            try:
                txt = path.read_text(encoding="utf-8")
            except Exception:
                txt = ""
            pane.render_html(txt, highlight_values=[], scheme="per_mapping")
        elif isinstance(pane, MarkdownRenderPane):
            try:
                txt = path.read_text(encoding="utf-8")
            except Exception:
                txt = ""
            pane.render_markdown(txt, highlight_values=[], scheme="per_mapping")
        elif isinstance(pane, SpreadsheetRenderPane):
            pane.render_spreadsheet(path, highlight_values=[], scheme="per_mapping")
        elif isinstance(pane, PlainTextRenderPane):
            try:
                txt = path.read_text(encoding="utf-8")
            except Exception:
                txt = f"<cannot read: {path}>"
            pane.set_text(txt)
            pane.set_spans([])

    # ---- preview file generation ----

    def _build_preview_file(self, src: Path) -> Optional[Path]:
        """Materialise a temp file with text + image redactions
        applied, returning its path. Cached by
        ``(src, mtime, text_rules_hash, image_decisions_hash)``.
        """
        proj = self.state.project
        if proj is None:
            return None

        rules = self._build_rules(proj)
        decisions = self._load_image_decisions(proj)

        try:
            mtime_ns = src.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        cache_key = self._cache_key(src, mtime_ns, rules, decisions)
        try:
            _BUILD_PREVIEW_CACHE.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        cached = _BUILD_PREVIEW_CACHE / f"{cache_key}{src.suffix.lower()}"
        if cached.exists():
            return cached

        # Step 1: text substitutions via the format adapter. Always
        # write to ``cached`` (it becomes the apply destination).
        try:
            adapter = get_adapter(src)
        except Exception:
            return None
        try:
            adapter.write(src, cached, rules)
        except Exception:
            # Adapter refused; fall back to a straight copy so the
            # operator at least sees the raw input + can still apply
            # image redactions on top.
            try:
                shutil.copy2(src, cached)
            except Exception:
                return None

        # Step 2: image redactions on top. Decisions point at
        # image_ids; the adapter walks the file and replaces blobs in
        # place. Failures here downgrade to "preview without image
        # redactions" rather than killing the preview entirely.
        if decisions and decisions.decisions:
            try:
                adapter.apply_image_redactions(cached, decisions.decisions)
            except Exception as e:
                self._status.setText(
                    f"Image redactions could not be applied to the preview: {e}\n"
                    "(text substitutions ARE shown.)"
                )
        return cached

    def _build_rules(self, proj) -> list[SubstitutionRule]:
        """Combine substitution_map + auto_promoted + approved
        pending into the rule list the preview should apply.
        """
        rules: list[SubstitutionRule] = []
        seen: set[str] = set()
        if self.state.smap is not None:
            for r in self.state.smap.to_rules(tier="preview"):
                if r.from_ and r.to and r.from_ != r.to and r.from_ not in seen:
                    rules.append(r)
                    seen.add(r.from_)
        for c in list(self.state.auto_t0) + list(self.state.auto_t1):
            v = (c.value or "").strip()
            p = (c.suggested_placeholder or "").strip()
            if not v or not p or v == p or v in seen:
                continue
            rules.append(
                SubstitutionRule(
                    from_=v, to=p,
                    category=c.category or "other",
                    tier="preview",
                )
            )
            seen.add(v)
        return rules

    def _load_image_decisions(self, proj) -> Optional[ImageRedactions]:
        # Live in-memory decisions take priority so the preview
        # reflects edits the operator has not saved to disk yet.
        if self._live_decisions_provider is not None:
            try:
                live = self._live_decisions_provider()
            except Exception:
                live = None
            if live is not None:
                return live
        try:
            return load_decisions(proj.image_redactions_path)
        except Exception:
            return None

    def _cache_key(
        self,
        src: Path,
        mtime_ns: int,
        rules: list[SubstitutionRule],
        decisions: Optional[ImageRedactions],
    ) -> str:
        h = hashlib.sha256()
        h.update(str(src).encode("utf-8"))
        h.update(str(mtime_ns).encode("utf-8"))
        for r in sorted(rules, key=lambda r: (r.from_, r.to)):
            h.update(f"{r.from_}->{r.to}".encode("utf-8"))
        if decisions is not None:
            for image_id in sorted(decisions.decisions.keys()):
                d = decisions.decisions[image_id]
                h.update(f"|{image_id}:{d.decision}".encode("utf-8"))
                for r in d.rects:
                    h.update(
                        f"|{r.x},{r.y},{r.w},{r.h},{r.tool},{r.intensity},"
                        f"{r.text},{r.font_size},{r.fg},{r.bg}".encode("utf-8")
                    )
        return h.hexdigest()[:24]


__all__ = ["BuildPreviewPanel"]
