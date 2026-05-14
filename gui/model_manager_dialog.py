"""Model Manager dialog: Library + Download + HF Search + Queue."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anonymize.download_queue import DownloadJob, DownloadQueue
from anonymize.hf_models import (
    CURATED_REPOS,
    GatedRepoError,
    OfflineMode,
    RepoFile,
    SearchFilters,
    delete_local,
    download_model,
    list_repo_files,
    best_local_per_repo,
    local_models,
    quant_tag,
    repo_metadata,
    repo_models_dir,
    search_repos,
)


def _cleanup_partial(repo: str, filename: str) -> None:
    """Delete the ``.part`` file (and any now-empty per-repo subdir)
    that ``download_model`` leaves behind when the user cancels a
    transfer. Without this the stopped download silently keeps disk
    space until the user notices it via Library → Delete.
    """
    try:
        dst = repo_models_dir(repo) / filename
        part = dst.with_suffix(dst.suffix + ".part")
        if part.exists():
            part.unlink()
        d = part.parent
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except Exception:
        pass


def _human(n: int) -> str:
    if n <= 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    f = float(n)
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.1f} {units[i]}"


def _cell(text: str) -> QTableWidgetItem:
    """Build a QTableWidgetItem with the full text exposed via tooltip
    so truncated names stay readable when the user hovers, the model
    manager dialog is often opened at a small size."""
    it = QTableWidgetItem(text)
    if text:
        it.setToolTip(text)
    return it


def _make_table(
    cols: list[str],
    *,
    initial_widths: list[int] | None = None,
    word_wrap: bool = False,
) -> QTableWidget:
    """Build a QTableWidget with all columns user-resizable
    (Interactive) and sensible initial widths. Stretch on the last
    column so the table fills the available width."""
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    h = t.horizontalHeader()
    for i in range(len(cols)):
        h.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
    h.setStretchLastSection(True)
    if initial_widths:
        for i, w in enumerate(initial_widths):
            t.setColumnWidth(i, w)
    t.setWordWrap(word_wrap)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    return t


class _DownloadSignals(QObject):
    # ``done`` and ``total`` are byte counts that routinely exceed 2 GB
    # (the 32-bit signed int upper bound), so we declare them as
    # ``qint64`` to avoid PySide6 silently overflowing the value while
    # marshalling the signal across thread boundaries. The ``eta`` field
    # stays ``int`` (always seconds, fits comfortably).
    progress = Signal(str, str, "qint64", "qint64", float, int)
    phase = Signal(str, str, str)
    finished = Signal(str, str, bool, str)


class _DownloadHub(QObject):
    """Process-wide owner of running download threads + their live
    signals + the cached last-known progress per job.

    The Model Manager dialog used to own these directly; closing the
    dialog therefore detached every running thread from any UI
    consumer and the next time the user reopened the dialog the
    progress bar appeared frozen even though the download was still
    going. Promoting them to a singleton lets the dialog be opened
    and closed freely while the download keeps reporting progress to
    one persistent listener; the dialog just re-reads the cached
    last-known values on construction.
    """

    _instance: "Optional[_DownloadHub]" = None

    def __init__(self) -> None:
        super().__init__()
        self.signals = _DownloadSignals()
        self.workers: dict[tuple[str, str], _DownloadThread] = {}
        self.stop_events: dict[tuple[str, str], threading.Event] = {}
        # Last-known progress per (repo, filename), so a freshly
        # opened dialog can paint the bar at the right %.
        self.last_progress: dict[tuple[str, str], dict] = {}
        self.last_phase: dict[tuple[str, str], str] = {}
        # Persisted queue is owned by the hub so concurrent dialog
        # instances cannot race on update().
        self.queue = DownloadQueue.load()
        self.signals.progress.connect(self._cache_progress)
        self.signals.phase.connect(self._cache_phase)
        self.signals.finished.connect(self._on_finished_internal)

    @classmethod
    def instance(cls) -> "_DownloadHub":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_running(self, repo: str, fn: str) -> bool:
        t = self.workers.get((repo, fn))
        return bool(t and t.isRunning())

    def enqueue_and_start(self, repo: str, filename: str) -> None:
        # If a worker is already running for this (repo, file), let it
        # finish, re-enqueueing while in flight would corrupt the
        # ``.part`` file with two writers.
        if self.is_running(repo, filename):
            return
        job = self.queue.enqueue(repo, filename)
        job.status = "running"
        self.queue.update(job)
        ev = threading.Event()
        self.stop_events[(repo, filename)] = ev
        t = _DownloadThread(job, self.signals, ev)
        self.workers[(repo, filename)] = t
        t.start()

    def cancel(self, repo: str, filename: str) -> None:
        ev = self.stop_events.get((repo, filename))
        if ev:
            ev.set()
        job = self.queue.find(repo, filename)
        if job:
            job.status = "cancelled"
            self.queue.update(job)
        # Try a fast-path cleanup; the worker thread's _on_finished
        # callback will run another pass once it actually exits.
        _cleanup_partial(repo, filename)

    def _cache_progress(
        self,
        repo: str,
        fn: str,
        done: int,
        total: int,
        speed: float,
        eta: int,
    ) -> None:
        self.last_progress[(repo, fn)] = {
            "done": done,
            "total": total,
            "speed": speed,
            "eta": eta,
        }
        job = self.queue.find(repo, fn)
        if job is not None:
            job.progress_bytes = done
            job.total_bytes = total
            self.queue.update(job)

    def _cache_phase(self, repo: str, fn: str, phase: str) -> None:
        self.last_phase[(repo, fn)] = phase
        job = self.queue.find(repo, fn)
        if job is not None:
            job.status = phase
            self.queue.update(job)

    def _on_finished_internal(
        self, repo: str, fn: str, ok: bool, err: str
    ) -> None:
        job = self.queue.find(repo, fn)
        cancelled = err == "cancelled"
        if job is not None:
            job.status = "done" if ok else ("cancelled" if cancelled else "error")
            job.error = "" if ok else err
            self.queue.update(job)
        self.workers.pop((repo, fn), None)
        self.stop_events.pop((repo, fn), None)
        self.last_progress.pop((repo, fn), None)
        self.last_phase.pop((repo, fn), None)
        if not ok:
            _cleanup_partial(repo, fn)


class _DownloadThread(QThread):
    def __init__(
        self,
        job: DownloadJob,
        signals: _DownloadSignals,
        stop_event: threading.Event,
    ) -> None:
        super().__init__()
        self.job = job
        self.signals = signals
        self.stop_event = stop_event

    def run(self) -> None:
        repo = self.job.repo_id
        fn = self.job.filename
        try:
            res = download_model(
                repo,
                fn,
                dst=Path(self.job.dst) if self.job.dst else None,
                progress_cb=lambda d, t, s, e: self.signals.progress.emit(repo, fn, d, t, s, e),
                phase_cb=lambda p: self.signals.phase.emit(repo, fn, p),
                stop_event=self.stop_event,
            )
            if res.cancelled:
                self.signals.finished.emit(repo, fn, False, "cancelled")
            elif res.ok:
                self.signals.finished.emit(repo, fn, True, "")
            else:
                self.signals.finished.emit(repo, fn, False, res.error)
        except GatedRepoError as e:
            self.signals.finished.emit(repo, fn, False, f"gated: {e}")
        except OfflineMode:
            self.signals.finished.emit(repo, fn, False, "offline mode")
        except Exception as e:
            self.signals.finished.emit(repo, fn, False, str(e))


class ModelManagerDialog(QDialog):
    """Tabbed dialog for managing local models, curated downloads, and HF search."""

    model_picked = Signal(str)
    # Emitted whenever a download finishes successfully (or a local
    # ``Import GGUF…`` writes a new file). The MainWindow connects this
    # to ``server_panel.gallery.refresh()`` so preset cards update their
    # "Download / Re-download" buttons live, without waiting for the
    # user to close the dialog.
    library_changed = Signal()

    def __init__(self, parent=None, *, initial_repo_id: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Model Manager")
        self.resize(1180, 760)
        self.setMinimumSize(960, 600)
        # When the dialog is opened from a context that already knows
        # which catalog repo the operator was looking for (e.g. the
        # "Model not on disk" prompt or the per-preset Download
        # button), we land on the Curated tab and pre-select the repo
        # in the tree. Empty string falls back to the historical
        # default (Library tab, no pre-selection).
        self._initial_repo_id: str = (initial_repo_id or "").strip()
        # The hub is a process-wide singleton: closing/reopening
        # this dialog must NOT lose live download progress, and a
        # newly opened dialog must show whatever the hub already
        # knows. We connect to the hub's signals here and stay
        # connected for the lifetime of THIS dialog instance only;
        # the hub itself owns the running threads.
        self._hub = _DownloadHub.instance()
        self._hub.signals.progress.connect(self._on_progress)
        self._hub.signals.phase.connect(self._on_phase)
        self._hub.signals.finished.connect(self._on_finished)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_library_tab(), "Library")
        self.tabs.addTab(self._build_download_tab(), "Curated downloads")
        self.tabs.addTab(self._build_search_tab(), "Search Hugging Face")
        self._queue_tab_widget = self._build_queue_tab()
        self.tabs.addTab(self._queue_tab_widget, "Queue")
        # Track Queue tab index so post-enqueue we can jump there.
        self._queue_tab_index = 3
        # Status banner shared across tabs (one toast can land while
        # the user is on Search and they need to see it without
        # leaving). Lives at the bottom of the dialog.
        self.status_banner = QLabel("")
        self.status_banner.setObjectName("Muted")
        self.status_banner.setWordWrap(True)
        self.status_banner.setMinimumHeight(20)
        tabs = self.tabs

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs, 1)
        lay.addWidget(self.status_banner)
        lay.addLayout(bottom)

        self._refresh_library()
        self._refresh_queue_table()
        # If the hub already had progress for an in-flight download
        # before the dialog was opened, paint it immediately so the
        # progress bar isn't stuck at the last persisted value.
        for (repo, fn), pg in self._hub.last_progress.items():
            self._on_progress(repo, fn, pg["done"], pg["total"], pg["speed"], pg["eta"])
        for (repo, fn), phase in self._hub.last_phase.items():
            self._on_phase(repo, fn, phase)
        # Honor the ``initial_repo_id`` hint after the tabs have been
        # populated: jumps to the Curated tab and scrolls / selects
        # the matching row.
        if self._initial_repo_id:
            self._jump_to_curated_repo(self._initial_repo_id)

    def _jump_to_curated_repo(self, repo_id: str) -> None:
        """Switch to the Curated downloads tab and pre-select the row
        whose ``UserRole`` data matches ``repo_id``. Best-effort:
        silently no-ops when the tree or item is missing (the dialog
        still opens, just on the Library tab if the switch fails).
        """
        try:
            self.tabs.setCurrentIndex(1)
        except Exception:
            return
        tree = getattr(self, "repo_tree", None)
        if tree is None:
            return
        try:
            for i in range(tree.topLevelItemCount()):
                parent = tree.topLevelItem(i)
                for j in range(parent.childCount()):
                    ch = parent.child(j)
                    rid = ch.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(rid, str) and rid == repo_id:
                        tree.setCurrentItem(ch)
                        tree.scrollToItem(ch)
                        return
        except Exception:
            pass

    def closeEvent(self, ev) -> None:
        """Disconnect this dialog's slots from the hub on close so
        reopening doesn't end up with N copies of the handler firing
        for every event. The hub itself + its threads keep running.
        """
        try:
            self._hub.signals.progress.disconnect(self._on_progress)
            self._hub.signals.phase.disconnect(self._on_phase)
            self._hub.signals.finished.disconnect(self._on_finished)
        except Exception:
            pass
        super().closeEvent(ev)

    def reject(self) -> None:
        # QDialog.reject is what fires when the user hits Escape /
        # the [x] button, closeEvent handles cleanup but we go
        # through the same path explicitly for accept() symmetry.
        super().reject()

    # ---- Library tab -------------------------------------------------------

    def _build_library_tab(self) -> QWidget:
        w = QWidget()
        self.lib_table = _make_table(
            ["", "File", "Size", "Path"],
            initial_widths=[28, 260, 90, 380],
        )

        import_btn = QPushButton("Import GGUF…")
        import_btn.clicked.connect(self._import_gguf)
        delete_btn = QPushButton("Delete selected")
        delete_btn.setObjectName("DangerButton")
        delete_btn.clicked.connect(self._delete_selected_library)
        use_btn = QPushButton("Use selected")
        use_btn.setObjectName("PrimaryButton")
        use_btn.clicked.connect(self._use_selected_library)

        bar = QHBoxLayout()
        bar.addWidget(import_btn)
        bar.addWidget(delete_btn)
        bar.addStretch()
        bar.addWidget(use_btn)

        lay = QVBoxLayout(w)
        lay.addWidget(self.lib_table, 1)
        lay.addLayout(bar)
        return w

    def _refresh_library(self) -> None:
        self.lib_table.setRowCount(0)
        models = local_models()
        # Best-quality file per repo gets ★. Priority order:
        # BF16/F16/FP16 > unsloth UD-Q*_K_XL > Q8_0 > Q6_K > Q5/Q4 > IQ4 > Q3/Q2.
        recommended = best_local_per_repo(models)
        for p in models:
            r = self.lib_table.rowCount()
            self.lib_table.insertRow(r)
            self.lib_table.setItem(r, 0, _cell("★" if p in recommended else ""))
            self.lib_table.setItem(r, 1, _cell(p.name))
            try:
                size = p.stat().st_size
            except Exception:
                size = 0
            self.lib_table.setItem(r, 2, _cell(_human(size)))
            self.lib_table.setItem(r, 3, _cell(str(p)))

    def _import_gguf(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Import GGUF model(s)", filter="GGUF (*.gguf)")
        if not ps:
            return
        from anonymize.hf_models import MODELS_DIR

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        import shutil

        for p in ps:
            src = Path(p)
            dst = MODELS_DIR / src.name
            if dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
        self._refresh_library()
        self.library_changed.emit()

    def _delete_selected_library(self) -> None:
        rows = sorted(
            {i.row() for i in self.lib_table.selectedItems()},
            reverse=True,
        )
        if not rows:
            QMessageBox.information(
                self,
                "No selection",
                "Select one or more rows in the Library table first, "
                "then click Delete selected.",
            )
            return
        targets: list[Path] = []
        for r in rows:
            it = self.lib_table.item(r, 3)
            if it:
                targets.append(Path(it.text()))
        if not targets:
            return
        names = "\n".join(f"  • {t.name}" for t in targets[:8])
        if len(targets) > 8:
            names += f"\n  • … (+{len(targets) - 8} more)"
        ans = QMessageBox.question(
            self,
            "Delete model(s)",
            f"Permanently delete {len(targets)} GGUF file(s) from "
            f"the on-disk library?\n\n{names}\n\nThis cannot be "
            f"undone.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        deleted = 0
        failed: list[tuple[Path, str]] = []
        for r, target in zip(rows, targets):
            try:
                if delete_local(target):
                    self.lib_table.removeRow(r)
                    deleted += 1
                else:
                    # delete_local returns False for two reasons: the
                    # path is outside MODELS_DIR, or unlink raised.
                    # Surface a useful message so the user understands.
                    from anonymize.hf_models import MODELS_DIR

                    if not str(target.resolve()).startswith(
                        str(MODELS_DIR.resolve())
                    ):
                        failed.append(
                            (target, f"path is outside {MODELS_DIR}")
                        )
                    elif not target.exists():
                        failed.append((target, "file no longer exists"))
                    else:
                        failed.append((target, "delete failed"))
            except Exception as e:
                failed.append((target, str(e)))
        if deleted:
            self._refresh_library()
            self.library_changed.emit()
            self.status_banner.setText(
                f"Deleted {deleted} model file(s) from the library."
            )
        if failed:
            details = "\n".join(f"  • {p.name}: {msg}" for p, msg in failed)
            QMessageBox.warning(
                self,
                "Some deletions failed",
                f"{len(failed)} file(s) could not be removed:\n\n"
                f"{details}",
            )

    def _use_selected_library(self) -> None:
        rows = {i.row() for i in self.lib_table.selectedItems()}
        if not rows:
            return
        r = next(iter(rows))
        path = self.lib_table.item(r, 3).text()
        self.model_picked.emit(path)
        self.accept()

    # ---- Curated download tab ---------------------------------------------

    # Quality bands used to group the curated dropdown into sections
    # the user can scan at a glance. Boundaries match BENCHMARKS.md.
    _QUALITY_BANDS = [
        # (label,                       min_inclusive_f1)
        ("🥇  Excellent  (Quality 80+)", 0.80),
        ("🥈  Good  (Quality 65-79)",    0.65),
        ("⭐  Usable  (Quality 50-64)",   0.50),
    ]

    @staticmethod
    def _band_for(f1: float | None) -> str:
        """Return the band label a CuratedRepo falls under, or a
        catch-all bucket for anything below the Usable cut."""
        for label, lo in ModelManagerDialog._QUALITY_BANDS:
            if (f1 or 0.0) >= lo:
                return label
        return "⚠️  Below Usable cut"

    def _build_download_tab(self) -> QWidget:
        from PySide6.QtWidgets import QScrollArea

        w = QWidget()

        # Filter row: free-text search, kept above the splitter so it
        # always covers both panes.
        self.repo_filter = QLineEdit()
        self.repo_filter.setPlaceholderText(
            "Search by name, family or repo id…"
        )
        self.repo_filter.setClearButtonEnabled(True)
        self.repo_filter.textChanged.connect(self._apply_repo_filter)

        # Tree replaces the flat dropdown: top-level rows are the
        # quality bands (🥇 / 🥈 / ⭐), each holding the curated repos
        # that scored in that band. Five columns surface the trade-off
        # at a glance so the user does not have to expand each entry
        # to know if it fits.
        self.repo_tree = QTreeWidget()
        self.repo_tree.setColumnCount(5)
        self.repo_tree.setHeaderLabels(
            ["Model", "Quality", "Disk", "VRAM", "Speed"]
        )
        self.repo_tree.setRootIsDecorated(True)
        self.repo_tree.setAlternatingRowColors(True)
        # Taller rows so the badge / size cells breathe and the
        # quality label colour reads at a glance.
        self.repo_tree.setUniformRowHeights(True)
        self.repo_tree.setStyleSheet(
            "QTreeWidget::item { padding: 6px 4px; }"
        )
        self.repo_tree.setColumnWidth(0, 360)
        self.repo_tree.setColumnWidth(1, 120)
        self.repo_tree.setColumnWidth(2, 80)
        self.repo_tree.setColumnWidth(3, 90)
        self.repo_tree.setColumnWidth(4, 80)
        self.repo_tree.header().setStretchLastSection(False)
        self.repo_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        # Right-align the metric columns (Quality / Disk / VRAM /
        # Speed) so figures with different magnitudes line up.
        self.repo_tree.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft
        )
        self.repo_tree.itemSelectionChanged.connect(self._refresh_files)
        self._populate_repo_tree()

        # Benchmark badge, surfaces a plain-language quality verdict
        # + leak-catch ratio + GPU/runtime cost for the currently
        # selected repo, plus a ⚠️/❌ warning for incompatible
        # models, so the user knows what to expect *before*
        # committing to a multi-GB download.
        self.bench_badge = QLabel("")
        self.bench_badge.setObjectName("BenchBadge")
        self.bench_badge.setWordWrap(True)
        self.bench_badge.setTextFormat(Qt.TextFormat.RichText)
        # Long-form description from the curated entry (one-liner
        # explaining what the model is good for).
        self.repo_desc = QLabel("")
        self.repo_desc.setObjectName("Muted")
        self.repo_desc.setWordWrap(True)

        self.files_table = _make_table(
            ["File", "Size", "Tag"],
            initial_widths=[260, 80, 140],
        )

        dl_btn = QPushButton("Download selected")
        dl_btn.setObjectName("PrimaryButton")
        dl_btn.clicked.connect(self._download_selected_curated)

        info = QLabel("Tip: tap *Download* to enqueue. Files resume across restarts.")
        info.setObjectName("Muted")

        # Compact one-line dropdown kept as a backward-compat hook
        # (other code paths and the bench harness reference
        # ``self.repo_combo`` to read the active repo id). Hidden
        # from view; the tree drives the selection now.
        self.repo_combo = QComboBox()
        for r in CURATED_REPOS:
            self.repo_combo.addItem(f"{r.display_name}  ·  {r.repo_id}", r.repo_id)
        self.repo_combo.setVisible(False)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Curated models:"))
        filter_row.addStretch()
        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(self.repo_filter, 1)

        actions = QHBoxLayout()
        actions.addWidget(info, 1)
        actions.addWidget(dl_btn)

        # ---- Right pane: details for the selected repo ----------------
        # Wrap badge + description in a scroll area so dense multi-build
        # cards never crowd out the file table.
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 0, 0, 0)
        right_lay.setSpacing(8)
        right_lay.addWidget(self.bench_badge)
        right_lay.addWidget(self.repo_desc)
        right_lay.addWidget(QLabel("Recommended files:"))
        right_lay.addWidget(self.files_table, 1)
        right_lay.addLayout(actions)

        # ---- Horizontal splitter: tree (left) | details (right) -------
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.repo_tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)  # tree gets ~60 % of the width
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([640, 440])

        lay = QVBoxLayout(w)
        lay.addLayout(filter_row)
        lay.addWidget(splitter, 1)
        # Auto-select the first curated repo so the badge + file
        # table never start empty.
        self._select_first_repo()
        self._refresh_files()
        return w

    def _populate_repo_tree(self) -> None:
        """Build (or rebuild) the curated repo tree, grouped by
        quality band and sorted by Quality desc within each band."""
        self.repo_tree.clear()
        # Sort all repos by Quality desc.
        repos = sorted(
            CURATED_REPOS,
            key=lambda r: -(r.benchmark_f1 or 0),
        )
        # Bucket by band label, preserving the sorted order.
        buckets: dict[str, list] = {}
        for r in repos:
            band = self._band_for(r.benchmark_f1)
            buckets.setdefault(band, []).append(r)
        # Emit groups in the canonical order (band labels list order).
        ordered_bands = [lbl for lbl, _ in self._QUALITY_BANDS] + [
            "⚠️  Below Usable cut"
        ]
        from PySide6.QtGui import QBrush, QColor, QFont

        BAND_BG = {
            "🥇  Excellent  (Quality 80+)": QColor(63, 185, 80, 60),
            "🥈  Good  (Quality 65-79)":    QColor(94, 168, 233, 55),
            "⭐  Usable  (Quality 50-64)":   QColor(245, 166, 35, 55),
            "⚠️  Below Usable cut":         QColor(248, 81, 73, 55),
        }
        for band in ordered_bands:
            entries = buckets.get(band, [])
            if not entries:
                continue
            count = len(entries)
            header = QTreeWidgetItem([f"{band}  -  {count} model{'s' if count != 1 else ''}", "", "", "", ""])
            font = QFont()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 1)
            header.setFont(0, font)
            # The selection is constrained to leaf items; mark the
            # group header un-selectable.
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            bg = BAND_BG.get(band, QColor(60, 60, 60, 60))
            for col in range(5):
                header.setBackground(col, QBrush(bg))
            self.repo_tree.addTopLevelItem(header)
            header.setFirstColumnSpanned(True)
            for r in entries:
                quality = int(round((r.benchmark_f1 or 0) * 100))
                disk = self._disk_label_for(r)
                vram = (
                    f"~{r.benchmark_peak_vram_mb / 1024:.1f} GB"
                    if r.benchmark_peak_vram_mb
                    else "—"
                )
                speed = self._speed_label(r.benchmark_total_seconds or 0)
                child = QTreeWidgetItem(
                    [r.display_name, f"{quality} / 100", disk, vram, speed]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, r.repo_id)
                child.setToolTip(0, r.repo_id)
                # Colour-code the Quality column by band so the user
                # can spot the top picks even without reading the
                # number; matches the badge palette.
                quality_colour = QColor("#3FB950") if quality >= 80 else (
                    QColor("#5DA4EC") if quality >= 65
                    else QColor("#F5A623") if quality >= 50
                    else QColor("#F85149")
                )
                child.setForeground(1, QBrush(quality_colour))
                f = QFont()
                f.setBold(True)
                child.setFont(1, f)
                # Right-align the metric columns so figures with
                # different magnitudes line up.
                for col in (1, 2, 3, 4):
                    child.setTextAlignment(
                        col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    )
                header.addChild(child)
            header.setExpanded(True)

        # Header tooltips so column headers explain what each metric
        # means without crowding the header text.
        hdr = self.repo_tree.header()
        try:
            hdr.model().setHeaderData(
                1, Qt.Orientation.Horizontal,
                "F1 x 100 on the 5-PDF anonymization corpus. Higher is better.",
                Qt.ItemDataRole.ToolTipRole,
            )
            hdr.model().setHeaderData(
                2, Qt.Orientation.Horizontal,
                "Approximate on-disk size of the recommended GGUF file.",
                Qt.ItemDataRole.ToolTipRole,
            )
            hdr.model().setHeaderData(
                3, Qt.Orientation.Horizontal,
                "Peak GPU memory used by llama-server while serving requests (nvidia-smi).",
                Qt.ItemDataRole.ToolTipRole,
            )
            hdr.model().setHeaderData(
                4, Qt.Orientation.Horizontal,
                "Wall-clock time to anonymise the 5-PDF corpus end-to-end.",
                Qt.ItemDataRole.ToolTipRole,
            )
        except Exception:
            pass

    def _select_first_repo(self) -> None:
        """Pick the highest-quality entry as the initial selection."""
        for i in range(self.repo_tree.topLevelItemCount()):
            grp = self.repo_tree.topLevelItem(i)
            if grp.childCount() > 0:
                self.repo_tree.setCurrentItem(grp.child(0))
                return

    def _current_repo_id(self) -> str | None:
        """Return the repo_id of the currently-selected tree item,
        if any (group headers return None)."""
        it = self.repo_tree.currentItem()
        if it is None:
            return None
        rid = it.data(0, Qt.ItemDataRole.UserRole)
        return rid if isinstance(rid, str) else None

    @staticmethod
    def _disk_label_for(r) -> str:
        """Best-effort disk-size summary for a curated repo.

        Prefers the accurate ``params × quantisation factor`` estimate
        computed by :func:`anonymize.model_size.estimate_gguf_disk_label`
        on the recommended filename / repo id / display name. Falls
        back to the legacy ``vram_mb / 1.2`` heuristic only when the
        helper cannot find both the parameter count and a known
        quantisation tag (e.g. an orphan repo with no recommended
        files and a vague display name).
        """
        from anonymize.model_size import estimate_gguf_disk_label

        first_file = ""
        try:
            files = list(getattr(r, "recommended_files", []) or [])
            if files:
                first_file = files[0] or ""
        except Exception:
            first_file = ""
        label = estimate_gguf_disk_label(
            first_file,
            getattr(r, "repo_id", "") or "",
            getattr(r, "display_name", "") or "",
            getattr(r, "family", "") or "",
        )
        if label:
            return label
        # Legacy fallback: VRAM-derived ballpark when we cannot parse
        # quantisation from any hint. Better than an empty column.
        vram_mb = getattr(r, "benchmark_peak_vram_mb", None)
        if vram_mb:
            gb = vram_mb / 1024 / 1.2
            if gb < 1.0:
                return f"~{gb * 1024:.0f} MB"
            return f"~{gb:.1f} GB"
        return "—"

    @staticmethod
    def _speed_label(seconds: float) -> str:
        if seconds <= 0:
            return "—"
        if seconds < 90:
            return f"{seconds:.0f} s ⚡"
        mins = seconds / 60
        return f"~{mins:.0f} min"

    def _apply_repo_filter(self, text: str) -> None:
        """Live-filter the repo tree: match by display name + family
        + repo_id. Empty groups collapse automatically."""
        needle = (text or "").strip().lower()
        for i in range(self.repo_tree.topLevelItemCount()):
            grp = self.repo_tree.topLevelItem(i)
            visible_children = 0
            for j in range(grp.childCount()):
                child = grp.child(j)
                repo_id = child.data(0, Qt.ItemDataRole.UserRole) or ""
                hay = " ".join([
                    child.text(0).lower(),
                    repo_id.lower(),
                ])
                show = needle in hay if needle else True
                child.setHidden(not show)
                if show:
                    visible_children += 1
            grp.setHidden(visible_children == 0)

    def _refresh_files(self) -> None:
        repo = self._current_repo_id()
        # Keep the hidden combo in sync so the rest of the dialog
        # (download enqueue, etc.) finds the right repo id.
        if repo is not None:
            idx = self.repo_combo.findData(repo)
            if idx >= 0:
                self.repo_combo.setCurrentIndex(idx)
        self.files_table.setRowCount(0)
        if not repo:
            self.bench_badge.setText("")
            self.repo_desc.setText("")
            return
        # Surface the curated description + benchmark numbers so the
        # user knows what they're about to download.  Multi-build
        # entries embed the description into the badge (one block
        # per variant), so we don't duplicate it in ``repo_desc``.
        cur = next((c for c in CURATED_REPOS if c.repo_id == repo), None)
        if cur is not None:
            self.bench_badge.setText(self._format_bench_badge(cur))
            if getattr(cur, "alt_benchmarks", None):
                self.repo_desc.setText("")
            else:
                self.repo_desc.setText(cur.description)
        else:
            self.repo_desc.setText("")
            self.bench_badge.setText("")
        files = list_repo_files(repo) or []
        for f in files:
            r = self.files_table.rowCount()
            self.files_table.insertRow(r)
            self.files_table.setItem(r, 0, _cell(f.filename))
            self.files_table.setItem(r, 1, _cell(_human(f.size_bytes)))
            tag = "★ recommended" if f.is_recommended else ""
            self.files_table.setItem(r, 2, _cell(tag))

    @staticmethod
    def _quality_label(f1: float) -> tuple[str, str]:
        """Map the underlying quality score to a plain-language verdict
        + colour.  The score itself never reaches the user, these
        bins were calibrated against our anonymization corpus so the
        word *is* the message ("excellent" / "good" / ...).
        """
        if f1 >= 0.80:
            return ("excellent", "#3FB950")
        if f1 >= 0.65:
            return ("good", "#3FB950")
        if f1 >= 0.50:
            return ("usable", "#F5A623")
        if f1 >= 0.40:
            return ("poor", "#F5A623")
        return ("not recommended", "#F85149")

    @staticmethod
    def _recall_phrase(recall: float) -> str:
        """Translate a recall fraction into something a non-ML user
        immediately understands.  ``0.91`` → "Catches about 9 of
        every 10 leaks"."""
        out_of_ten = max(0, min(10, round(recall * 10)))
        if recall >= 0.85:
            return "Catches almost every leak"
        if recall >= 0.65:
            return f"Catches about {out_of_ten} of every 10 leaks"
        if recall >= 0.45:
            return f"Catches roughly {out_of_ten} of every 10 leaks"
        if recall <= 0.05:
            return "Doesn't return any leaks (regex fallback only)"
        return f"Misses most leaks (catches only {out_of_ten} of every 10)"

    @staticmethod
    def _format_bench_row(
        f1,
        recall,
        vram_mb,
        seconds,
        *,
        prefix: str = "",
    ) -> str:
        """Render one bench score row (Quality + Catches + GPU +
        Time) as an HTML pill string.  Used both for the primary
        curated benchmark numbers and for each entry of
        ``CuratedRepo.alt_benchmarks``; the per-variant heading
        and prose blurb live one level up in
        :func:`_format_bench_badge`."""
        if f1 is None:
            return ""
        bits: list[str] = []
        quality_label, label_colour = ModelManagerDialog._quality_label(f1)
        score = round(f1 * 100)
        bits.append(
            f'<span style="color:{label_colour}; font-weight:700;" '
            f'title="Quality score on our 5-PDF anonymization '
            f'corpus. Combines two things: how many real leaks '
            f'the model catches, and how often its alerts are '
            f'real. 0 = everything went wrong, 100 = perfect.">'
            f'{prefix}Quality: {quality_label} ({score}/100)</span>'
        )
        if recall is not None:
            phrase = ModelManagerDialog._recall_phrase(recall)
            bits.append(
                f'<span style="color:#9aa0a6;" '
                f'title="Of the real leaks in the document, how many '
                f'the model finds. Measured: {recall*100:.0f}%.">'
                f'{phrase}</span>'
            )
        if vram_mb:
            gb = vram_mb / 1024
            bits.append(
                f'<span style="color:#5da4ec; font-weight:600;" '
                f'title="Peak GPU memory used by llama-server while '
                f'serving requests, measured with nvidia-smi.">'
                f'Needs ~{gb:.1f} GB GPU</span>'
            )
        if seconds:
            mins = seconds / 60
            speed = (
                f'{seconds:.0f}s'
                if seconds < 90
                else f'~{mins:.1f} min'
            )
            bits.append(
                f'<span style="color:#9aa0a6;" '
                f'title="Total wall-clock time to anonymise the 5-PDF '
                f'benchmark corpus end-to-end (scan + LLM + apply + '
                f'verify).">'
                f'{speed} on the 5-PDF test</span>'
            )
        return "  ·  ".join(bits)

    @staticmethod
    def _variant_heading(label: str, filename: str) -> str:
        """Format the per-variant heading: ``"<label> (<quant>)"``
        when both are known, ``"<label>"`` or ``"<quant>"`` when
        only one is, empty otherwise."""
        tag = quant_tag(filename) if filename else ""
        if label and tag:
            return f"{label.capitalize()} ({tag})"
        if label:
            return label.capitalize()
        if tag:
            return tag
        return ""

    @staticmethod
    def _format_bench_badge(cur) -> str:
        """User-friendly HTML block summarising the curated bench
        numbers.  No raw F1 / scores in the visible text, just a
        plain-language verdict, what fraction of leaks the model
        catches, GPU need, and runtime.  Numeric details live in
        hover tooltips for power users.  ``compatibility_status``
        upgrades the row to a ⚠️/❌ warning so users don't burn a
        download on a broken model.

        Layout for multi-build repos (Ministral 3 8B Reasoning
        BF16 + Q5_K_M, today): an intro paragraph (the
        ``description`` field of the curated entry) followed by
        one block per variant, each block containing a bold
        heading, a score row, and a per-variant prose blurb.
        Single-build repos render only the score row plus the
        footer notes, the long description for those is shown
        separately in ``repo_desc``."""
        status = getattr(cur, "compatibility_status", "ok")
        reason = getattr(cur, "compatibility_reason", "") or ""

        if status == "incompatible":
            # Hard ❌: the model doesn't produce candidates at all in
            # this pipeline.  Show the reason inline so the user
            # immediately understands *why* it doesn't work.
            bits = [
                f'<span style="color:#F85149; font-weight:700;" '
                f'title="The pipeline falls back to the regex-only '
                f'baseline because this model never returns usable '
                f'JSON candidates.">'
                f'❌ Doesn\'t work in this pipeline</span>'
            ]
            if reason:
                bits.append(
                    f'<span style="color:#9aa0a6;">{reason}</span>'
                )
            return "  ·  ".join(bits)

        if cur.benchmark_f1 is None:
            return ""

        prefix = "⚠️ " if status == "low_quality" else ""
        alts = list(getattr(cur, "alt_benchmarks", []) or [])
        rows: list[str] = []

        # Multi-build entries get the full grouped layout: an
        # intro line, then per-variant heading + score + summary.
        if alts:
            if cur.description:
                rows.append(
                    f'<span style="color:#9aa0a6;">{cur.description}</span>'
                )
            primary_filename = (
                cur.recommended_files[0] if cur.recommended_files else ""
            )
            variants = [
                {
                    "label": getattr(cur, "primary_label", "") or "",
                    "filename": primary_filename,
                    "f1": cur.benchmark_f1,
                    "recall": cur.benchmark_recall,
                    "vram_mb": cur.benchmark_peak_vram_mb,
                    "seconds": cur.benchmark_total_seconds,
                    "summary": getattr(cur, "primary_summary", "") or "",
                },
                *[
                    {
                        "label": a.get("label", ""),
                        "filename": a.get("filename", ""),
                        "f1": a.get("f1"),
                        "recall": a.get("recall"),
                        "vram_mb": a.get("vram_mb"),
                        "seconds": a.get("seconds"),
                        "summary": a.get("summary", ""),
                    }
                    for a in alts
                ],
            ]
            for v in variants:
                heading = ModelManagerDialog._variant_heading(
                    v["label"], v["filename"]
                )
                score_row = ModelManagerDialog._format_bench_row(
                    v["f1"], v["recall"], v["vram_mb"], v["seconds"],
                    prefix=prefix if v is variants[0] else "",
                )
                if not score_row:
                    continue
                if heading:
                    rows.append(
                        f'<span style="color:#e6e6e6; font-weight:700;">'
                        f'{heading}</span>'
                    )
                rows.append(score_row)
                if v["summary"]:
                    rows.append(
                        f'<span style="color:#9aa0a6;">{v["summary"]}</span>'
                    )
        else:
            # Single-build entries keep the old compact layout: one
            # score row in the badge, full description shown in the
            # repo_desc label below.
            rows.append(
                ModelManagerDialog._format_bench_row(
                    cur.benchmark_f1,
                    cur.benchmark_recall,
                    cur.benchmark_peak_vram_mb,
                    cur.benchmark_total_seconds,
                    prefix=prefix,
                )
            )

        # Footer (notes / compatibility reason) describes the repo
        # as a whole and sits at the bottom of the badge.
        footer_bits: list[str] = []
        if status == "low_quality" and reason:
            footer_bits.append(
                f'<span style="color:#F5A623;" '
                f'title="Why this model is not in the curated set.">'
                f'{reason}</span>'
            )
        elif cur.benchmark_notes:
            footer_bits.append(
                f'<span style="color:#f5a623;">{cur.benchmark_notes}</span>'
            )
        if footer_bits:
            rows.append("  ·  ".join(footer_bits))

        return "<br>".join(rows)

    def _download_selected_curated(self) -> None:
        repo = self.repo_combo.currentData()
        rows = sorted({i.row() for i in self.files_table.selectedItems()})
        if not rows:
            return
        for r in rows:
            fname = self.files_table.item(r, 0).text()
            self._enqueue_and_start(repo, fname)

    # ---- Search tab --------------------------------------------------------

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search Hugging Face for GGUF models (e.g. qwen 7b q5)"
        )
        self.search_input.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Search")
        search_btn.setObjectName("PrimaryButton")
        search_btn.clicked.connect(self._do_search)

        # The ``!`` column is the per-row warning marker: ⚠️ for
        # low-quality, ❌ for incompatible. Painted by ``_do_search``
        # via ``repo_metadata`` so the user sees the problem without
        # having to click the row first.
        self.results_table = _make_table(
            ["!", "Repo", "Family", "Downloads", "License"],
            initial_widths=[28, 260, 90, 100, 110],
        )
        # Single-click to load files (was double-click, confusing).
        self.results_table.itemSelectionChanged.connect(self._on_repo_selection_changed)

        self.results_files = _make_table(
            ["File", "Size", "Tag"],
            initial_widths=[300, 90, 90],
        )
        # Right-pane info shows the currently-loaded repo for context.
        self.search_repo_lbl = QLabel(
            "Select a repository on the left to browse its files."
        )
        self.search_repo_lbl.setObjectName("Muted")
        self.search_repo_lbl.setWordWrap(True)
        # Compatibility / benchmark badge, populated when the user
        # opens a repo we already tested (curated top 5 or the
        # known-problematic list, including community republishers
        # matched via ``CuratedRepo.name_patterns``). Lets the user
        # see a ⚠️/❌ before downloading several GB.
        self.search_bench_badge = QLabel("")
        self.search_bench_badge.setObjectName("BenchBadge")
        self.search_bench_badge.setWordWrap(True)
        self.search_bench_badge.setTextFormat(Qt.TextFormat.RichText)
        self.search_bench_badge.hide()
        # Repo description, same source as the Curated tab. Shows
        # the per-file blurb when ``repo_metadata`` recognises the
        # selected repo, so users get the same context here as
        # they would landing on the Curated dropdown.
        self.search_repo_desc = QLabel("")
        self.search_repo_desc.setObjectName("Muted")
        self.search_repo_desc.setWordWrap(True)
        self.search_repo_desc.setTextFormat(Qt.TextFormat.RichText)
        self.search_repo_desc.hide()
        # Explicit Download button (replaces invisible double-click).
        self.search_dl_btn = QPushButton("Download selected")
        self.search_dl_btn.setObjectName("PrimaryButton")
        self.search_dl_btn.setEnabled(False)
        self.search_dl_btn.clicked.connect(self._search_download_selected)
        self.results_files.itemSelectionChanged.connect(
            lambda: self.search_dl_btn.setEnabled(
                bool(self.results_files.selectedItems())
            )
        )

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.addWidget(self.search_repo_lbl)
        right_lay.addWidget(self.search_bench_badge)
        right_lay.addWidget(self.search_repo_desc)
        right_lay.addWidget(self.results_files, 1)
        right_lay.addWidget(self.search_dl_btn)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.results_table)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        # Save handle width so the user can find the splitter to drag.
        split.setHandleWidth(6)

        head = QHBoxLayout()
        head.addWidget(self.search_input, 1)
        head.addWidget(search_btn)

        lay = QVBoxLayout(w)
        lay.addLayout(head)
        lay.addWidget(split, 1)
        return w

    def _do_search(self) -> None:
        q = self.search_input.text().strip()
        cards = search_repos(q, filters=SearchFilters(), limit=40)
        self.results_table.setRowCount(0)
        for c in cards:
            r = self.results_table.rowCount()
            self.results_table.insertRow(r)
            self.results_table.setItem(r, 0, self._search_warning_cell(c.repo_id))
            self.results_table.setItem(r, 1, _cell(c.repo_id))
            self.results_table.setItem(r, 2, _cell(c.family))
            self.results_table.setItem(r, 3, _cell(f"{c.downloads:,}"))
            self.results_table.setItem(r, 4, _cell(c.license))
        self.results_files.setRowCount(0)
        self.search_repo_lbl.setText(
            f"{len(cards)} result(s). Click a repository to load its files."
            if cards
            else "No results."
        )
        self.search_bench_badge.clear()
        self.search_bench_badge.hide()
        self.search_repo_desc.clear()
        self.search_repo_desc.hide()
        self.search_dl_btn.setEnabled(False)

    @staticmethod
    def _search_warning_cell(repo_id: str) -> QTableWidgetItem:
        """Build the status cell for the Search-tab results table.

        Looks up the repo in the curated + known-problematic catalogs
        (substring patterns only, network-free) so painting all 40
        rows after a search stays cheap.  Three outcomes:

        - **★** (white), repo is in the curated catalogue or is a
          recognised mirror of one.  Lets the user spot a tested,
          trusted model in a long search list before they click.
        - **⚠️** (amber), benchmarked but below the curated cut.
        - **❌** (red), architecturally / behaviourally
          incompatible, will fall back to the regex baseline only.

        Repos we have no opinion on get an empty cell.
        """
        meta = repo_metadata(repo_id, follow_base_model=False)
        if meta is None:
            return _cell("")
        if meta.compatibility_status == "incompatible":
            it = QTableWidgetItem("❌")
            tip = (
                "Doesn't work in this pipeline.\n"
                f"{meta.compatibility_reason}"
            )
        elif meta.compatibility_status == "low_quality":
            it = QTableWidgetItem("⚠️")
            tip = (
                "Not recommended.\n"
                f"{meta.compatibility_reason}"
            )
        else:  # "ok", curated entry or a recognised mirror.
            it = QTableWidgetItem("★")
            it.setForeground(Qt.GlobalColor.yellow)
            label = (meta.display_name or meta.repo_id).strip()
            tip = (
                "Already benchmarked: "
                f"{label}\n"
                "See the right pane for the score and recommended "
                "files."
            )
        it.setToolTip(tip)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return it

    def _on_repo_selection_changed(self) -> None:
        rows = sorted({i.row() for i in self.results_table.selectedItems()})
        if not rows:
            return
        # Repo id moved to column 1 after the warning column was
        # added at index 0.
        item = self.results_table.item(rows[0], 1)
        if not item:
            return
        self._open_repo_files_by_id(item.text())

    def _open_repo_files_by_id(self, repo: str) -> None:
        files = list_repo_files(repo) or []
        self.results_files.setRowCount(0)
        for f in files:
            r = self.results_files.rowCount()
            self.results_files.insertRow(r)
            self.results_files.setItem(r, 0, _cell(f.filename))
            self.results_files.setItem(r, 1, _cell(_human(f.size_bytes)))
            tag = "★ recommended" if f.is_recommended else ""
            self.results_files.setItem(r, 2, _cell(tag))
        self.results_files.setProperty("repo", repo)
        self.search_repo_lbl.setText(
            f"<b>{repo}</b>: {len(files)} file(s). "
            f"Select one and click <b>Download</b>."
        )
        self.search_repo_lbl.setTextFormat(Qt.TextFormat.RichText)
        # Surface the benchmark / compatibility note for any repo we
        # have data on. ``repo_metadata`` matches the canonical
        # owner/repo first, then falls back to substring patterns so
        # community republishers of the same broken model still
        # trigger the ⚠️/❌ warning.
        meta = repo_metadata(repo)
        badge_html = self._format_bench_badge(meta) if meta else ""
        if badge_html:
            self.search_bench_badge.setText(badge_html)
            self.search_bench_badge.show()
        else:
            self.search_bench_badge.clear()
            self.search_bench_badge.hide()
        # Mirror the Curated tab: surface the repo description
        # under the badge.  For multi-build entries the description
        # is already embedded into the badge (one block per
        # variant), so we hide the standalone description label to
        # avoid duplicating the intro line.
        if meta and meta.description and not getattr(meta, "alt_benchmarks", None):
            self.search_repo_desc.setText(meta.description)
            self.search_repo_desc.show()
        else:
            self.search_repo_desc.clear()
            self.search_repo_desc.hide()
        self.search_dl_btn.setEnabled(False)

    def _search_download_selected(self) -> None:
        repo = self.results_files.property("repo")
        if not repo:
            return
        rows = sorted({i.row() for i in self.results_files.selectedItems()})
        if not rows:
            return
        for r in rows:
            fname = self.results_files.item(r, 0).text()
            self._enqueue_and_start(repo, fname)

    # ---- Queue tab ---------------------------------------------------------

    def _build_queue_tab(self) -> QWidget:
        w = QWidget()
        self.queue_table = _make_table(
            ["Repo", "File", "Status", "Progress", "Actions"],
            initial_widths=[180, 220, 130, 140, 200],
        )
        clr = QPushButton("Clear finished")
        clr.setToolTip(
            "Remove rows in 'done', 'cancelled' or 'error' state from "
            "this list. The cancelled / errored rows leave the queue "
            "but partial files on disk are also removed when you Stop."
        )
        clr.clicked.connect(self._clear_finished)
        bar = QHBoxLayout()
        bar.addStretch()
        bar.addWidget(clr)
        lay = QVBoxLayout(w)
        lay.addWidget(self.queue_table, 1)
        lay.addLayout(bar)
        return w

    def _refresh_queue_table(self) -> None:
        self.queue_table.setRowCount(0)
        for j in self._hub.queue.jobs:
            r = self.queue_table.rowCount()
            self.queue_table.insertRow(r)
            self.queue_table.setItem(r, 0, _cell(j.repo_id))
            self.queue_table.setItem(r, 1, _cell(j.filename))
            self.queue_table.setItem(r, 2, _cell(j.status))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(j.percent)
            self.queue_table.setCellWidget(r, 3, bar)
            actions = QWidget()
            al = QHBoxLayout(actions)
            al.setContentsMargins(0, 0, 0, 0)
            al.setSpacing(4)
            is_active = j.status in ("running", "lookup", "downloading", "finalizing")
            is_finished = j.status in ("done", "cancelled", "error")
            if is_active:
                stop = QPushButton("Stop")
                stop.setObjectName("DangerButton")
                stop.setToolTip(
                    "Cancel the download and delete the partial file "
                    "from disk. The row will be removed from the queue."
                )
                stop.clicked.connect(
                    lambda _=False, repo=j.repo_id, fn=j.filename: self._cancel(
                        repo, fn
                    )
                )
                al.addWidget(stop)
            else:
                retry = QPushButton("Retry")
                retry.setToolTip("Re-enqueue this download.")
                retry.clicked.connect(
                    lambda _=False, repo=j.repo_id, fn=j.filename: self._enqueue_and_start(
                        repo, fn
                    )
                )
                al.addWidget(retry)
            if is_finished:
                rm = QPushButton("Remove")
                rm.setToolTip(
                    "Remove this row from the queue list. (For "
                    "cancelled / errored rows the partial file on "
                    "disk was already deleted when you Stopped.)"
                )
                rm.clicked.connect(
                    lambda _=False, repo=j.repo_id, fn=j.filename: self._remove_row(
                        repo, fn
                    )
                )
                al.addWidget(rm)
            self.queue_table.setCellWidget(r, 4, actions)

    def _clear_finished(self) -> None:
        # Mutate the hub's queue (the singleton) so the change
        # persists across dialog open/close cycles. Without this the
        # next reopen reloaded the queue from disk and the cleared
        # rows came back.
        self._hub.queue.jobs = [
            j
            for j in self._hub.queue.jobs
            if j.status not in ("done", "cancelled", "error")
        ]
        self._hub.queue.save()
        self._refresh_queue_table()

    def _remove_row(self, repo: str, filename: str) -> None:
        """Remove a single queue row + clean any leftover ``.part``."""
        self._hub.queue.jobs = [
            j
            for j in self._hub.queue.jobs
            if not (j.repo_id == repo and j.filename == filename)
        ]
        self._hub.queue.save()
        _cleanup_partial(repo, filename)
        self._refresh_queue_table()

    # ---- download orchestration -------------------------------------------

    def _enqueue_and_start(self, repo: str, filename: str) -> None:
        self._hub.enqueue_and_start(repo, filename)
        self._refresh_queue_table()
        # Make it obvious to the user that the click did something:
        # show a banner and switch to the Queue tab automatically so
        # they can see the progress bar fill up. Without this they
        # could click Download multiple times not knowing the request
        # was already accepted.
        self.status_banner.setText(
            f"Downloading <b>{filename}</b> from <code>{repo}</code>, "
            f"see Queue for progress."
        )
        self.status_banner.setTextFormat(Qt.TextFormat.RichText)
        try:
            self.tabs.setCurrentIndex(self._queue_tab_index)
        except Exception:
            pass

    def _cancel(self, repo: str, filename: str) -> None:
        self._hub.cancel(repo, filename)
        self._refresh_queue_table()
        self.status_banner.setText(
            f"Cancelled <b>{filename}</b> · partial file removed."
        )

    def _on_progress(self, repo: str, fn: str, done: int, total: int, speed: float, eta: int) -> None:
        # The hub has already cached the latest progress + persisted
        # the queue; we only need to repaint the right row.
        job = self._hub.queue.find(repo, fn)
        if job is None:
            return
        for r in range(self.queue_table.rowCount()):
            if self.queue_table.item(r, 0).text() == repo and self.queue_table.item(r, 1).text() == fn:
                bar = self.queue_table.cellWidget(r, 3)
                if isinstance(bar, QProgressBar):
                    bar.setValue(job.percent)
                self.queue_table.item(r, 2).setText(
                    f"{job.percent}% · {_human(int(speed))}/s · ETA {eta}s"
                )
                break

    def _on_phase(self, repo: str, fn: str, phase: str) -> None:
        for r in range(self.queue_table.rowCount()):
            if self.queue_table.item(r, 0).text() == repo and self.queue_table.item(r, 1).text() == fn:
                self.queue_table.item(r, 2).setText(phase)
                break

    def _on_finished(self, repo: str, fn: str, ok: bool, err: str) -> None:
        cancelled = err == "cancelled"
        # Hub already updated the queue + cleaned partials; we
        # repaint and notify the rest of the app.
        self._refresh_queue_table()
        self._refresh_library()
        if ok:
            self.status_banner.setText(
                f"Downloaded <b>{fn}</b> from <code>{repo}</code>."
            )
            # Tell the MainWindow that the on-disk library changed so
            # the preset gallery flips ``Download`` -> ``Re-download``
            # immediately, without waiting for the user to close us.
            self.library_changed.emit()
        elif cancelled:
            self.status_banner.setText(
                f"Cancelled <b>{fn}</b> · partial file removed."
            )
        else:
            self.status_banner.setText(
                f"<b>{fn}</b> failed: {err}"
            )


__all__ = ["ModelManagerDialog"]
