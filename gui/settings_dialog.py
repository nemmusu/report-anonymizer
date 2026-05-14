"""Settings dialog: thresholds, paths, build options.

Server-side configuration (binary / model / GPU layers / ctx / parallel) now
lives in the dedicated **Server panel + Preset editor**; this dialog only
keeps the per-project pipeline settings.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .state import AppState


def _file_picker(line: QLineEdit, *, dir_only: bool, parent) -> QPushButton:
    btn = QPushButton("Browse…")

    def run() -> None:
        if dir_only:
            p = QFileDialog.getExistingDirectory(parent, "Select", line.text())
        else:
            p, _ = QFileDialog.getOpenFileName(parent, "Select", line.text())
        if p:
            line.setText(p)

    btn.clicked.connect(run)
    return btn


class SettingsDialog(QDialog):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.setWindowTitle("Settings")
        self.setMinimumWidth(680)

        tabs = QTabWidget()
        tabs.addTab(self._llm_tab(), "Pipeline")
        tabs.addTab(self._scan_tab(), "Scan & limits")
        tabs.addTab(self._paths_tab(), "Paths")
        tabs.addTab(self._build_tab(), "Build")

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)

        info = QLabel(
            "Server / model / GPU / parallel parameters are configured in the "
            "Server panel via Presets. The active preset's <b>parallel</b> "
            "field controls both llama-server slots and the pipeline's LLM "
            "worker count, they are the same knob now."
        )
        info.setObjectName("Muted")
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)

        root = QVBoxLayout(self)
        root.addWidget(info)
        root.addWidget(tabs)
        root.addWidget(bb)

    def _llm_tab(self) -> QWidget:
        w = QWidget()
        proj = self.state.project
        self.t_high = QDoubleSpinBox()
        self.t_high.setRange(0.0, 1.0)
        self.t_high.setSingleStep(0.05)
        self.t_high.setValue(proj.t_high if proj else 0.92)
        self.t_low = QDoubleSpinBox()
        self.t_low.setRange(0.0, 1.0)
        self.t_low.setSingleStep(0.05)
        self.t_low.setValue(proj.t_low if proj else 0.75)
        self.n_vote = QSpinBox()
        self.n_vote.setRange(1, 9)
        self.n_vote.setValue(proj.n_vote if proj else 3)
        prof = self.state.profile if self.state else None
        prof_parallel = int(getattr(prof, "parallel", 0) or 0)
        prof_name = getattr(prof, "name", "default")
        self.concurrency_lbl = QLabel(
            f"<b>{prof_parallel}</b> "
            f"<span style='color:#9aa0a6'>(from preset "
            f"<code>{prof_name}</code>; edit in Server → Customize)</span>"
        )
        self.concurrency_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.auto_resolve = QCheckBox(
            "Auto-resolve residual leaks after verify"
        )
        self.auto_resolve.setToolTip(
            "When enabled, after the verify stage the pipeline runs a "
            "deterministic feedback loop: for every residual leak the "
            "verifier finds, derive the placeholder from the existing "
            "substitution_map (case-aware), promote it and re-apply. "
            "Catches occurrences the LLM detector missed because they "
            "were case variants or sub-tokens of a value already known "
            "to the map. Disable for repeatable scripted runs."
        )
        self.auto_resolve.setChecked(
            proj.auto_resolve_residuals if proj else True
        )
        self.audit_llm = QCheckBox(
            "Use LLM audit as final pass (typos / concatenations)"
        )
        self.audit_llm.setToolTip(
            "When enabled and an LLM is reachable, the auto-resolve "
            "loop falls back to an LLM auditor that spots residual "
            "leaks the deterministic regex cannot see (typos, "
            "concatenations, creative variants of values already in "
            "the map). Every audit candidate is grounded in an "
            "existing map entry, the auditor never invents a brand. "
            "Disable to skip the LLM call (faster but lower coverage)."
        )
        self.audit_llm.setChecked(
            proj.audit_residuals_with_llm if proj else True
        )

        f = QFormLayout(w)
        f.addRow("T_high (auto-promote):", self.t_high)
        f.addRow("T_low (review):", self.t_low)
        f.addRow("N self-consistency:", self.n_vote)
        f.addRow("Concurrency (parallel LLM):", self.concurrency_lbl)
        f.addRow("", self.auto_resolve)
        f.addRow("", self.audit_llm)
        return w

    def _scan_tab(self) -> QWidget:
        w = QWidget()
        proj = self.state.project
        self.max_size = QSpinBox()
        self.max_size.setRange(1, 4096)
        self.max_size.setValue(proj.max_file_size_mb if proj else 50)
        self.max_depth = QSpinBox()
        self.max_depth.setRange(0, 32)
        self.max_depth.setValue(proj.max_depth if (proj and proj.max_depth) else 0)
        self.respect_git = QCheckBox(".gitignore + .anonignore")
        self.respect_git.setChecked(proj.respect_gitignore if proj else True)
        self.follow_links = QCheckBox("Follow symlinks (off by default)")
        self.follow_links.setChecked(proj.follow_symlinks if proj else False)
        self.offline = QCheckBox("Offline mode (no HF / no network)")
        self.offline.setChecked(proj.offline_mode if proj else False)
        f = QFormLayout(w)
        f.addRow("Max file size (MB):", self.max_size)
        f.addRow("Max depth (0=unlimited):", self.max_depth)
        f.addRow("", self.respect_git)
        f.addRow("", self.follow_links)
        f.addRow("", self.offline)
        return w

    def _paths_tab(self) -> QWidget:
        w = QWidget()
        proj = self.state.project
        self.map_p = QLineEdit(str(proj.map_path) if proj else "config/substitution_map.yml")
        self.pat_p = QLineEdit(str(proj.patterns_path) if proj else "config/leak_patterns.yml")
        self.safe_p = QLineEdit(str(proj.safe_terms_path) if proj else "config/safe_terms.yml")

        f = QFormLayout(w)
        for label, le in (
            ("substitution_map.yml:", self.map_p),
            ("leak_patterns.yml:", self.pat_p),
            ("safe_terms.yml:", self.safe_p),
        ):
            r = QHBoxLayout()
            r.addWidget(le, 1)
            r.addWidget(_file_picker(le, dir_only=False, parent=self))
            f.addRow(label, r)
        return w

    def _build_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(QLabel("Pandoc / pdftotext are auto-detected on PATH; WeasyPrint is bundled as a Python dep."))
        return w

    def _save(self) -> None:
        if self.state.project is not None:
            p = self.state.project
            p.t_high = self.t_high.value()
            p.t_low = self.t_low.value()
            p.n_vote = self.n_vote.value()
            # Concurrency is owned by the active server preset
            # (parallel slot count) and re-synced into ``p.concurrency``
            # on every stage start, no editor here, just a read-out.
            p.auto_resolve_residuals = self.auto_resolve.isChecked()
            p.audit_residuals_with_llm = self.audit_llm.isChecked()
            p.max_file_size_mb = self.max_size.value()
            p.max_depth = self.max_depth.value() or None
            p.respect_gitignore = self.respect_git.isChecked()
            p.follow_symlinks = self.follow_links.isChecked()
            p.offline_mode = self.offline.isChecked()
            p.map_path = Path(self.map_p.text())
            p.patterns_path = Path(self.pat_p.text())
            p.safe_terms_path = Path(self.safe_p.text())
        self.accept()


__all__ = ["SettingsDialog"]
