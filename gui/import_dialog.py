"""Dialog shown right after the operator drops files/folders.

Previews the file inventory (count per format), lets the operator pick the
output folder, the PDF strategy (in-place vs re-derive), and whether to also
build a PDF for ``.md`` inputs.

Also accepts incremental drops while open: drag one file in, then drop more
on the dialog to append them, then click Open to process all of them at
once. Useful when the user wants to assemble a multi-file batch by dropping
files in succession instead of all at once.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from anonymize.project import Project
from anonymize.templates import list_templates


def _classify_paths(paths: list[Path]) -> tuple[str, dict[str, int]]:
    """Return ('folder'/'multi'/'single', {ext: count})."""
    if len(paths) == 1 and paths[0].is_dir():
        files = [p for p in paths[0].rglob("*") if p.is_file()]
        ctr = Counter(p.suffix.lower() or "<noext>" for p in files)
        return "folder", dict(ctr)
    if len(paths) == 1 and paths[0].is_file():
        return "single", {paths[0].suffix.lower() or "<noext>": 1}
    files = [p for p in paths if p.is_file()]
    ctr = Counter(p.suffix.lower() or "<noext>" for p in files)
    return "multi", dict(ctr)


class ImportDialog(QDialog):
    def __init__(self, paths: list[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import project")
        self.setMinimumWidth(560)
        self.setAcceptDrops(True)
        self.paths = list(dict.fromkeys(paths))  # de-dup while preserving order
        self.mode, self.classification = _classify_paths(self.paths)

        self.title = QLabel("")
        self.title.setTextFormat(Qt.TextFormat.RichText)

        # Sources panel: a list widget so the user can see the staged
        # files and remove items individually. Drops on the dialog
        # append to this list (see dropEvent below).
        self.sources_list = QListWidget()
        self.sources_list.setMaximumHeight(160)
        self.sources_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        # The QListWidget defaults to accepting drops AND its own drag
        # actions. Two problems with that:
        #   1. The list's drop handler can fire BEFORE the dialog's,
        #      triggering segfault-prone reentry into the drop loop
        #      while we mutate the list contents inside our own
        #      dropEvent.
        #   2. We don't want internal item reordering anyway, drops
        #      only ever mean "add files".
        # So we turn it off explicitly and channel every file drop
        # through the dialog itself.
        self.sources_list.setAcceptDrops(False)
        self.sources_list.setDragEnabled(False)
        self.sources_list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self.sources_list.setToolTip(
            "Drop more files on this dialog to add them. "
            "Select rows and press Delete to remove them."
        )
        # Pressing Delete on the list removes the selected entries.
        self.sources_list.installEventFilter(self)

        add_files_btn = QPushButton("+ Add files…")
        add_files_btn.setToolTip(
            "Pick more files via the file picker, or drop them on "
            "the dialog to extend the batch."
        )
        add_files_btn.clicked.connect(self._on_add_files_clicked)
        remove_btn = QPushButton("Remove selected")
        remove_btn.setObjectName("DangerButton")
        remove_btn.clicked.connect(self._remove_selected)

        sources_bar = QHBoxLayout()
        sources_bar.addWidget(add_files_btn)
        sources_bar.addWidget(remove_btn)
        sources_bar.addStretch()
        self.cls_label = QLabel("")
        self.cls_label.setObjectName("Muted")
        sources_bar.addWidget(self.cls_label)

        # Output picker
        self.out_edit = QLineEdit("")
        # Track whether the user manually edited the output path; if
        # they did, the auto-computed default no longer overrides it
        # when they add/remove files from the staged list.
        self._user_set_output = False
        self.out_edit.textEdited.connect(lambda _t: self._mark_output_dirty())
        out_btn = QPushButton("Browse…")
        out_btn.clicked.connect(self._pick_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_btn)

        # PDF strategy
        self.pdf_combo = QComboBox()
        self.pdf_combo.addItem("In-place (preserve layout)", "inplace")
        self.pdf_combo.addItem("Re-derive (clean re-render)", "rederive")

        # Template picker for any styled output the run produces
        # (Re-derive PDFs, Build's Markdown→PDF, "Also export as PDF/
        # HTML"). Lets the user pick the look here once instead of
        # opening Export… afterwards. Stays as "(none)" by default so
        # legacy projects keep the plain DEFAULT_CSS rendering.
        self.template_combo = QComboBox()
        self.template_combo.addItem("(none, default style)", None)
        try:
            for t in list_templates():
                label = t.name or t.id
                if t.source == "user":
                    label += "  · user"
                self.template_combo.addItem(label, t.id)
        except Exception:
            # Template index missing or malformed → user just gets the
            # "(none)" entry, which is still functional.
            pass
        self.template_combo.setToolTip(
            "Template applied to any rendered PDF / HTML produced by "
            "the run (Re-derive, Build, Also export). Manage templates "
            "via Export… → New template."
        )
        # Live hint right under the Template combo, updates as the
        # operator changes PDF strategy / extra exports / source type
        # so the templates' applicability is never silently surprising.
        self.template_hint = QLabel("")
        self.template_hint.setObjectName("Muted")
        self.template_hint.setWordWrap(True)
        self.pdf_combo.currentIndexChanged.connect(self._refresh_template_hint)
        self.template_combo.currentIndexChanged.connect(self._refresh_template_hint)
        for cb_ in ("export_pdf",):
            pass  # placeholder so future export checkboxes plug in here

        # Extra output formats, only meaningful when the source is a
        # text-based document that pandoc can convert (md, html, rst,
        # txt, …). Stays hidden in multi-file or PDF-only mode where
        # mass conversion would not have a single sensible target.
        self.export_pdf = QCheckBox("Also export as PDF")
        self.export_html = QCheckBox("Also export as HTML")
        self.export_md = QCheckBox("Also export as Markdown")
        for cb in (self.export_pdf, self.export_html, self.export_md):
            cb.setChecked(False)
        # When the source is a single .md file, the historical default
        # was to also emit a PDF, keep that behaviour.
        if self.mode == "single":
            ext = self.paths[0].suffix.lower()
            if ext in {".md", ".markdown"}:
                self.export_pdf.setChecked(True)

        form = QFormLayout()
        form.addRow("Source(s):", self.sources_list)
        form.addRow("", sources_bar)
        form.addRow("Output:", out_row)
        form.addRow("PDF strategy:", self.pdf_combo)
        form.addRow("Template:", self.template_combo)
        form.addRow("", self.template_hint)
        # Only single-file flow gets the extra-format pickers (multi /
        # folder modes already use ``stage_build`` to fan out PDFs).
        if self._source_is_convertible():
            form.addRow("Also export:", self._extra_formats_row())
            for cb in (self.export_pdf, self.export_html):
                cb.toggled.connect(lambda _checked: self._refresh_template_hint())
        self._refresh_template_hint()

        self._bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._bb.accepted.connect(self.accept)
        self._bb.rejected.connect(self.reject)
        self._ok_btn = self._bb.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Open project")
        self._ok_btn.setObjectName("PrimaryButton")

        # A small drop-zone hint at the bottom so the user knows
        # they can keep dragging files in.
        drop_hint = QLabel(
            "🠓  Drop additional files anywhere on this dialog to "
            "add them to the batch."
        )
        drop_hint.setObjectName("Muted")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        root = QVBoxLayout(self)
        root.addWidget(self.title)
        root.addLayout(form)
        root.addWidget(drop_hint)
        root.addWidget(self._bb)
        self._refresh_sources_view()

    # ---- staging list -------------------------------------------------------

    def _refresh_sources_view(self) -> None:
        """Re-populate the QListWidget + recompute mode / classification
        / default output / OK enable state from ``self.paths``.
        """
        self.sources_list.clear()
        for p in self.paths:
            it = QListWidgetItem(str(p))
            it.setToolTip(str(p))
            self.sources_list.addItem(it)
        self.mode, self.classification = _classify_paths(self.paths)
        self.title.setText(
            f"<b>Mode:</b> {self.mode}  <span style='color:#9aa0a6'>"
            f"({len(self.paths)} source(s))</span>"
        )
        cls_lines = [
            f"{ext} × {n}"
            for ext, n in sorted(self.classification.items(), key=lambda x: -x[1])
        ]
        self.cls_label.setText(" · ".join(cls_lines) or "no files")
        # Update default output suggestion only if the user has not
        # touched the field, preserve any manual edit.
        if not getattr(self, "_user_set_output", False):
            self.out_edit.setText(self._default_output())
        self._ok_btn.setEnabled(bool(self.paths))

    def _add_paths(self, new_paths: Iterable[Path]) -> None:
        """Append paths to the staged list, de-duping and preserving
        the original order. The mode is recomputed automatically:
        going from 1 → ≥2 entries flips ``single`` to ``multi``."""
        existing = {str(p) for p in self.paths}
        added = 0
        for p in new_paths:
            if not p.exists():
                continue
            key = str(p.resolve())
            if key in existing:
                continue
            existing.add(key)
            self.paths.append(p)
            added += 1
        if added:
            self._refresh_sources_view()

    def _remove_selected(self) -> None:
        rows = sorted(
            {self.sources_list.row(it) for it in self.sources_list.selectedItems()},
            reverse=True,
        )
        if not rows:
            return
        for r in rows:
            if 0 <= r < len(self.paths):
                self.paths.pop(r)
        self._refresh_sources_view()

    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        if obj is self.sources_list and ev.type() == QEvent.Type.KeyPress:
            if isinstance(ev, QKeyEvent) and ev.key() in (
                Qt.Key.Key_Delete,
                Qt.Key.Key_Backspace,
            ):
                self._remove_selected()
                return True
        return super().eventFilter(obj, ev)

    def _on_add_files_clicked(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Add files to batch")
        if ps:
            self._add_paths(Path(p) for p in ps)

    # ---- drag & drop on the dialog ------------------------------------------

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        new_paths: list[Path] = []
        for u in e.mimeData().urls():
            local = u.toLocalFile()
            if local:
                new_paths.append(Path(local))
        if not new_paths:
            return
        e.acceptProposedAction()
        # Defer the actual list mutation to the next event-loop tick.
        # Mutating widgets (clearing the QListWidget, recomputing the
        # mode label) from INSIDE Qt's drop dispatch path was causing
        # a SIGSEGV when the user dropped multiple files on the dialog
        #, Qt is still walking the widget tree and a synchronous
        # rebuild can leave dangling C++ pointers.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda paths=new_paths: self._add_paths(paths))

    # ---- output picking -----------------------------------------------------

    def _default_output(self) -> str:
        if self.mode == "folder":
            d = self.paths[0]
            return str(d.parent / ("Anonymized_" + d.name))
        if self.mode == "single":
            f = self.paths[0]
            # A dedicated sibling directory holds the anonymized file plus
            # the run state files (applied_substitutions.json, ...). Keeping
            # them out of the *input* folder avoids polluting the source.
            return str(f.parent / f"{f.stem}.anonymized")
        # multi
        return str(self.paths[0].parent / "Anonymized_multi")

    _CONVERTIBLE_EXTS = {
        ".md", ".markdown", ".html", ".htm", ".rst", ".txt",
    }

    def _source_is_convertible(self) -> bool:
        if self.mode != "single" or not self.paths:
            return False
        return self.paths[0].suffix.lower() in self._CONVERTIBLE_EXTS

    def _extra_formats_row(self) -> QWidget:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.export_pdf)
        row.addWidget(self.export_html)
        row.addWidget(self.export_md)
        row.addStretch()
        return wrap

    def _pick_output(self) -> None:
        # Always a directory: ``Project.for_single_file`` knows how to extract
        # the desired basename from a file-like path too, so power users can
        # still type ``foo.anonymized.pdf`` directly into the field.
        path = QFileDialog.getExistingDirectory(
            self, "Select output folder", self.out_edit.text()
        )
        if path:
            self.out_edit.setText(path)
            self._user_set_output = True

    def _mark_output_dirty(self) -> None:
        self._user_set_output = True

    # ---- result -------------------------------------------------------------

    def _refresh_template_hint(self) -> None:
        """Tell the operator whether the chosen template will actually
        be applied for the current strategy / source / extra-export
        combination.

        Templates apply when the run produces a *new* PDF/HTML render:
          * Re-derive PDF (any PDF source).
          * stage_build's MD/HTML -> PDF (folder mode or "Also export
            as PDF/HTML" on a single MD/HTML source).

        Templates do NOT apply to the in-place PDF edit path: that
        flow rewrites text inside the original PDF and never reaches
        a renderer. Without this hint operators silently picked a
        template + In-place and saw no visual change.
        """
        # No-op when there's no real selection
        tid = self.template_combo.currentData()
        strat = self.pdf_combo.currentData() or "inplace"
        if not tid:
            self.template_hint.setText(
                "No template selected, outputs use the default plain "
                "style. Pick a template if you want a styled PDF."
            )
            return

        # The template applies if any branch of the pipeline renders a
        # PDF/HTML (rederive, stage_build's extras, folder build).
        produces_render = False
        notes: list[str] = []
        if strat == "rederive":
            produces_render = True
            notes.append("PDF re-derive will use the template (no cover).")
        if self.mode == "folder":
            produces_render = True
            notes.append("Folder Build will style each section with the template.")
        if self._source_is_convertible() and (
            self.export_pdf.isChecked() or self.export_html.isChecked()
        ):
            produces_render = True
            notes.append("'Also export as PDF/HTML' will use the template.")

        if produces_render:
            self.template_hint.setText("✓ " + " ".join(notes))
        else:
            self.template_hint.setText(
                "ⓘ The template won't apply to this run: in-place PDF edits "
                "keep the original layout, and no extra render targets are "
                "selected. Switch PDF strategy to Re-derive, or check 'Also "
                "export as PDF/HTML', for the template to take effect."
            )

    def to_project(self) -> Project:
        out = Path(self.out_edit.text()).expanduser()
        pdf_strategy = self.pdf_combo.currentData() or "inplace"
        extras: list[str] = []
        if self._source_is_convertible():
            if self.export_pdf.isChecked():
                extras.append("pdf")
            if self.export_html.isChecked():
                extras.append("html")
            if self.export_md.isChecked():
                extras.append("md")
        # ``also_build_pdf_for_md`` mirrors ``"pdf"`` in the new list
        # (it predates the multi-format selector and is still consumed
        # by ``stage_build`` for backward compatibility).
        also_md = "pdf" in extras

        if self.mode == "folder":
            proj = Project.for_folder(
                self.paths[0],
                out,
                pdf_strategy=pdf_strategy,
                also_build_pdf_for_md=also_md,
            )
        elif self.mode == "single":
            proj = Project.for_single_file(
                self.paths[0],
                out,
                pdf_strategy=pdf_strategy,
                also_build_pdf_for_md=also_md,
            )
        else:
            proj = Project.for_multi_file(
                self.paths,
                out,
                pdf_strategy=pdf_strategy,
                also_build_pdf_for_md=also_md,
            )
        proj.extra_export_formats = extras
        proj.export_template_id = self.template_combo.currentData()
        return proj


__all__ = ["ImportDialog"]
