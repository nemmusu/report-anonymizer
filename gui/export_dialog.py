"""Export anonymized files to PDF using a chosen template.

The dialog lets the operator:

- pick a template (built-in or user) from a polished gallery,
- preview it (description + which CSS file backs it),
- fill in metadata (title / subtitle / engagement / author / classification /
  footer) that gets injected into the cover page,
- choose which files to export (single or batch),
- choose an output directory,
- launch a background worker that renders each PDF with progress.

User templates can be created/edited via :class:`TemplateEditorDialog` (button
"New template…" inside the dialog).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from anonymize.templates import (
    TemplateContext,
    TemplateMeta,
    add_user_template,
    delete_user_template,
    export_files_to_pdf,
    get_template,
    list_templates,
)


class _ExportSignals(QObject):
    progress = Signal(int, int, str)  # done, total, label
    finished = Signal(list, str)       # results, error_message ("" on success)


class _ExportThread(QThread):
    def __init__(
        self,
        *,
        files: list[Path],
        template: TemplateMeta,
        ctx: TemplateContext,
        out_dir: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.files = list(files)
        self.template = template
        self.ctx = ctx
        self.out_dir = out_dir
        self.signals = _ExportSignals()
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            results: list[Path] = []
            total = len(self.files)
            for i, f in enumerate(self.files, 1):
                if self._stop.is_set():
                    break
                self.signals.progress.emit(i - 1, total, f.name)
                produced = export_files_to_pdf(
                    [f],
                    template=self.template,
                    ctx=self.ctx,
                    out_dir=self.out_dir,
                    stop_event=self._stop,
                )
                results.extend(produced)
                self.signals.progress.emit(i, total, f.name)
            self.signals.finished.emit(results, "")
        except Exception as e:
            self.signals.finished.emit([], str(e))


class TemplateEditorDialog(QDialog):
    """Create / edit a user template (wrapper.html + style.css + metadata)."""

    def __init__(self, *, existing: Optional[TemplateMeta] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Template editor")
        self.resize(900, 700)

        self._existing = existing

        form = QFormLayout()
        self.id_edit = QLineEdit(existing.id if existing else "")
        self.id_edit.setPlaceholderText("snake_case_id (folder name)")
        if existing:
            self.id_edit.setReadOnly(True)
        self.name_edit = QLineEdit(existing.name if existing else "")
        self.desc_edit = QLineEdit(existing.description if existing else "")
        form.addRow("Id:", self.id_edit)
        form.addRow("Name:", self.name_edit)
        form.addRow("Description:", self.desc_edit)

        self.html_edit = QPlainTextEdit()
        self.html_edit.setFont(QFont("monospace"))
        self.html_edit.setPlainText(
            existing.wrapper_path.read_text(encoding="utf-8") if existing else _DEFAULT_WRAPPER
        )
        self.css_edit = QPlainTextEdit()
        self.css_edit.setFont(QFont("monospace"))
        self.css_edit.setPlainText(
            existing.style_path.read_text(encoding="utf-8") if existing else _DEFAULT_STYLE
        )

        gb_html = QGroupBox("wrapper.html")
        v1 = QVBoxLayout(gb_html); v1.addWidget(self.html_edit)
        gb_css = QGroupBox("style.css")
        v2 = QVBoxLayout(gb_css); v2.addWidget(self.css_edit)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(gb_html)
        split.addWidget(gb_css)
        split.setSizes([1, 1])

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(split, 1)
        root.addWidget(bb)

    def _save(self) -> None:
        tid = self.id_edit.text().strip()
        if not tid:
            QMessageBox.warning(self, "Missing id", "Please give the template a unique id.")
            return
        try:
            add_user_template(
                tid,
                name=self.name_edit.text().strip() or tid,
                description=self.desc_edit.text().strip(),
                wrapper_html=self.html_edit.toPlainText(),
                style_css=self.css_edit.toPlainText(),
            )
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self.accept()


class ExportDialog(QDialog):
    """Pick template + metadata + files, then run the export."""

    def __init__(
        self,
        *,
        candidate_files: list[Path],
        default_template_id: str = "pentest_modern",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export anonymized → PDF")
        self.setMinimumSize(960, 640)
        self._files = list(candidate_files)
        self._thread: Optional[_ExportThread] = None

        # ---- left: template gallery ----
        self.gallery = QListWidget()
        self.gallery.itemSelectionChanged.connect(self._refresh_preview)
        for t in list_templates():
            it = QListWidgetItem(t.name)
            it.setData(Qt.ItemDataRole.UserRole, t.id)
            tip = f"{t.description}\n\nsource: {t.source}"
            it.setToolTip(tip)
            self.gallery.addItem(it)
        self._select_template(default_template_id)

        btn_new = QPushButton("New template…")
        btn_new.clicked.connect(self._new_template)
        btn_edit = QPushButton("Edit")
        btn_edit.clicked.connect(self._edit_template)
        btn_del = QPushButton("Delete")
        btn_del.clicked.connect(self._delete_template)

        gallery_actions = QHBoxLayout()
        gallery_actions.addWidget(btn_new)
        gallery_actions.addWidget(btn_edit)
        gallery_actions.addWidget(btn_del)
        gallery_actions.addStretch()

        gallery_box = QGroupBox("Template")
        v = QVBoxLayout(gallery_box)
        v.addWidget(self.gallery, 1)
        v.addLayout(gallery_actions)

        # ---- right: metadata + files + output ----
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setObjectName("Muted")

        form = QFormLayout()
        self.title_edit = QLineEdit("Anonymized report")
        self.subtitle_edit = QLineEdit("")
        self.engagement_edit = QLineEdit("")
        self.author_edit = QLineEdit("")
        self.date_edit = QLineEdit()
        from datetime import date as _d
        self.date_edit.setText(_d.today().isoformat())
        self.classification_edit = QLineEdit("ANONYMIZED · INTERNAL")
        self.footer_edit = QLineEdit("This document has been anonymized for redistribution.")
        form.addRow("Title:", self.title_edit)
        form.addRow("Subtitle:", self.subtitle_edit)
        form.addRow("Engagement:", self.engagement_edit)
        form.addRow("Author:", self.author_edit)
        form.addRow("Date:", self.date_edit)
        form.addRow("Classification:", self.classification_edit)
        form.addRow("Footer:", self.footer_edit)

        # Output dir
        self.out_dir_edit = QLineEdit("")
        btn_browse_out = QPushButton("Browse…")
        btn_browse_out.clicked.connect(self._pick_out_dir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir_edit, 1)
        out_row.addWidget(btn_browse_out)
        form.addRow("Output dir:", out_row)

        # Files (read-only, shown for transparency)
        self.files_view = QListWidget()
        for f in self._files:
            self.files_view.addItem(str(f))

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.status = QLabel("")
        self.status.setObjectName("Muted")

        right = QVBoxLayout()
        right.addWidget(self.preview)
        right.addLayout(form)
        right.addWidget(QLabel("Files to export:"))
        right.addWidget(self.files_view, 1)
        right.addWidget(self.progress)
        right.addWidget(self.status)
        right_box = QGroupBox("Export")
        right_wrap = QVBoxLayout(right_box)
        right_wrap.addLayout(right)

        # Buttons
        self.bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Close
        )
        self.bb.button(QDialogButtonBox.StandardButton.Apply).setText("Export PDFs")
        self.bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._run_export)
        self.bb.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)

        layout = QGridLayout(self)
        layout.addWidget(gallery_box, 0, 0)
        layout.addWidget(right_box, 0, 1)
        layout.addWidget(self.bb, 1, 0, 1, 2)
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)

        self._refresh_preview()

    # ---- helpers --------------------------------------------------------

    def _select_template(self, tid: str) -> None:
        for i in range(self.gallery.count()):
            it = self.gallery.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == tid:
                self.gallery.setCurrentRow(i)
                return
        if self.gallery.count():
            self.gallery.setCurrentRow(0)

    def _selected_template(self) -> Optional[TemplateMeta]:
        it = self.gallery.currentItem()
        if it is None:
            return None
        tid = it.data(Qt.ItemDataRole.UserRole)
        return get_template(tid)

    def _refresh_preview(self) -> None:
        t = self._selected_template()
        if t is None:
            self.preview.setText("No template selected.")
            return
        self.preview.setText(
            f"<b>{t.name}</b> &middot; <i>{t.source}</i><br>"
            f"{t.description}<br>"
            f"<small>wrapper: {t.wrapper_path.name} &middot; style: {t.style_path.name}</small>"
        )

    def _new_template(self) -> None:
        dlg = TemplateEditorDialog(parent=self)
        if dlg.exec():
            self._reload_gallery()

    def _edit_template(self) -> None:
        t = self._selected_template()
        if t is None:
            return
        if t.builtin:
            QMessageBox.information(
                self,
                "Read-only",
                "Built-in templates can't be edited. Use 'New template…' to clone.",
            )
            return
        dlg = TemplateEditorDialog(existing=t, parent=self)
        if dlg.exec():
            self._reload_gallery(select_id=t.id)

    def _delete_template(self) -> None:
        t = self._selected_template()
        if t is None:
            return
        if t.builtin:
            QMessageBox.information(self, "Built-in", "Built-in templates can't be deleted.")
            return
        if QMessageBox.question(self, "Delete template", f"Delete '{t.name}' permanently?") != QMessageBox.StandardButton.Yes:
            return
        delete_user_template(t.id)
        self._reload_gallery()

    def _reload_gallery(self, *, select_id: Optional[str] = None) -> None:
        self.gallery.clear()
        for t in list_templates():
            it = QListWidgetItem(t.name)
            it.setData(Qt.ItemDataRole.UserRole, t.id)
            it.setToolTip(f"{t.description}\n\nsource: {t.source}")
            self.gallery.addItem(it)
        if select_id:
            self._select_template(select_id)
        self._refresh_preview()

    def _pick_out_dir(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Output directory")
        if p:
            self.out_dir_edit.setText(p)

    # ---- export ---------------------------------------------------------

    def _build_ctx(self) -> TemplateContext:
        return TemplateContext(
            title=self.title_edit.text().strip() or "Anonymized report",
            subtitle=self.subtitle_edit.text().strip(),
            engagement=self.engagement_edit.text().strip(),
            author=self.author_edit.text().strip(),
            date=self.date_edit.text().strip(),
            classification=self.classification_edit.text().strip(),
            footer=self.footer_edit.text().strip(),
        )

    def _run_export(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        t = self._selected_template()
        if t is None:
            QMessageBox.warning(self, "No template", "Pick a template first.")
            return
        out = self.out_dir_edit.text().strip()
        if not out:
            QMessageBox.warning(self, "No output directory", "Pick an output directory first.")
            return
        out_path = Path(out)
        if not self._files:
            QMessageBox.warning(self, "No files", "No input files to export.")
            return
        self.progress.setRange(0, max(1, len(self._files)))
        self.progress.setValue(0)
        self.status.setText(f"Exporting {len(self._files)} file(s)…")
        self.bb.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)
        self._thread = _ExportThread(
            files=self._files,
            template=t,
            ctx=self._build_ctx(),
            out_dir=out_path,
            parent=self,
        )
        self._thread.signals.progress.connect(self._on_progress)
        self._thread.signals.finished.connect(self._on_finished)
        self._thread.start()

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self.progress.setValue(done)
        self.status.setText(f"{done}/{total} · {label}")

    def _on_finished(self, results: list, error: str) -> None:
        self.bb.button(QDialogButtonBox.StandardButton.Apply).setEnabled(True)
        if error:
            QMessageBox.critical(self, "Export failed", error)
            self.status.setText("failed")
            return
        self.status.setText(f"done · {len(results)} PDF(s) created")
        QMessageBox.information(
            self,
            "Export complete",
            f"{len(results)} PDF(s) written to:\n{self.out_dir_edit.text()}",
        )


_DEFAULT_WRAPPER = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ title }}</title>
<style>{{ style }}</style></head>
<body>
  <header class="cover">
    <h1>{{ title }}</h1>
    <p>{{ subtitle }}</p>
    <table>
      <tr><th>Engagement</th><td>{{ engagement }}</td></tr>
      <tr><th>Author</th><td>{{ author }}</td></tr>
      <tr><th>Date</th><td>{{ date }}</td></tr>
      <tr><th>Classification</th><td>{{ classification }}</td></tr>
    </table>
  </header>
  <main class="content">
    {{ body }}
  </main>
  <footer>{{ footer }}</footer>
</body></html>
"""

_DEFAULT_STYLE = """\
@page { size: A4; margin: 18mm; }
body { font: 11pt/1.5 'Inter', sans-serif; color: #111; }
.cover { page-break-after: always; }
h1 { font-size: 22pt; }
"""


__all__ = ["ExportDialog", "TemplateEditorDialog"]
