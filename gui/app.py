"""MainWindow: orchestrates state, views, workers, server panel, tray."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anonymize.bootstrap import is_first_run
from anonymize.candidates import Candidate
from anonymize.pipeline import (
    clear_pause_marker,
    mark_paused,
    reset_run_state,
)
from anonymize.project import Project
from anonymize.server_doctor import Diagnosis
from anonymize.server_profile import get_profile, save_project_profile, save_user_profile
from anonymize.sub_map import SubstitutionMap

from ._dismissible_dialog import dismissible_message, make_dismissible
from .about_dialog import AboutDialog
from .diff_view import DiffView
from .export_dialog import ExportDialog
from .first_run_wizard import FirstRunWizard
from .icons import app_icon, icon
from .import_dialog import ImportDialog
from .model_manager_dialog import ModelManagerDialog
from .pipeline_view import PipelineView
from .preset_editor import PresetEditor
from .review_view import ReviewView
from .server_error_dialog import ServerErrorDialog
from .server_panel import ServerPanel
from .server_widget import ServerStatusWidget
from .settings_dialog import SettingsDialog
from .shortcuts_overlay import ShortcutsOverlay
from .sidebar import Sidebar
from .state import AppState
from .toast import Toaster
from .tray import Tray
from .verifier_view import VerifierView
from .welcome_view import WelcomeView
from .workers import (
    ApplyWorker,
    AutoResolveWorker,
    BuildWorker,
    PromoteWorker,
    ScanWorker,
    VerifyWorker,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("document-anonymizer-production")
        self.setWindowIcon(app_icon(64))
        # Adaptive sizing: target a comfortable layout but never overflow the
        # available screen geometry (so the user can always reach the title
        # bar / corners on smaller displays).
        self.setMinimumSize(880, 600)
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(1400, max(900, int(avail.width() * 0.92)))
            target_h = min(900, max(620, int(avail.height() * 0.90)))
            self.resize(target_w, target_h)
        else:
            self.resize(1180, 760)
        self.state = AppState()
        Toaster.attach(self)

        # ---- sidebar + central stack ---------------------------------------
        self.sidebar = Sidebar()
        self.sidebar.view_changed.connect(self._switch_view)
        # Per-row "connect" indicator next to the Server entry: red
        # when offline, amber while booting, green when the health
        # endpoint is up. Click triggers a pre-flight + Start, or
        # routes to the Server panel with a specific error dialog
        # when the active preset can't possibly come up.
        self.sidebar.indicator_clicked.connect(self._on_sidebar_indicator)
        self._refresh_server_indicator()
        self.state.server_status_changed.connect(
            lambda *_: self._refresh_server_indicator()
        )
        self.state.server_starting_changed.connect(
            lambda *_: self._refresh_server_indicator()
        )

        self.stack = QStackedWidget()
        self.welcome = WelcomeView()
        self.welcome.open_paths.connect(self._open_paths)

        self.pipeline_view = PipelineView(self.state)
        self.review_view = ReviewView(self.state)
        self.diff_view = DiffView(self.state)
        self.verifier_view = VerifierView(self.state)
        # The Server view is built later (after ``ServerPanel`` is
        # instantiated) and added to the stack at that point. We
        # keep a placeholder reference for ``_VIEW_MAP_KEYS``.

        for w in (
            self.welcome,
            self.pipeline_view,
            self.review_view,
            self.diff_view,
            self.verifier_view,
        ):
            self.stack.addWidget(w)
        self.stack.setCurrentWidget(self.welcome)

        central = QWidget()
        clay = QHBoxLayout(central)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)
        clay.addWidget(self.sidebar)
        clay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.setAcceptDrops(True)

        # ---- log dock ------------------------------------------------------
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        # Keep the dock compact on first launch: Qt sizes empty docks
        # very generously and the previous default ate ~30% of the
        # window height with nothing in it, crowding the main UX.
        # ``setMinimumHeight`` floors it and ``resizeDocks`` sets the
        # initial vertical share once the dock is in the layout.
        self.log.setMinimumHeight(80)
        log_dock = QDockWidget("Log", self)
        log_dock.setWidget(self.log)
        log_dock.setObjectName("LogDock")
        self.log_dock = log_dock
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, log_dock)
        self.resizeDocks(
            [log_dock], [140], Qt.Orientation.Vertical
        )

        # ---- server dock ---------------------------------------------------
        self.server_panel = ServerPanel(
            manager=self.state.server,
            hardware=None,
            state=self.state,
        )
        self.server_panel.request_open_model_manager.connect(self._open_model_manager)
        self.server_panel.request_open_settings.connect(self._open_settings)
        self.server_panel.request_diagnostics.connect(self._show_server_error)
        self.server_panel.profile_used.connect(self._on_profile_changed)
        # The server panel is now a regular view in the sidebar Server
        # tab, same configuration surface, no floating dock.
        self.stack.addWidget(self.server_panel)

        # ---- status bar ----------------------------------------------------
        self.server_widget = ServerStatusWidget(self.state)
        self.server_widget.request_open_panel.connect(self._toggle_server_panel)
        sb = QStatusBar()
        sb.addPermanentWidget(self.server_widget)
        self.busy_label = QLabel("")
        self.busy_label.setObjectName("Muted")
        sb.addWidget(self.busy_label, 1)
        self.setStatusBar(sb)

        # ---- menu bar ------------------------------------------------------
        self._build_menu()
        self._install_shortcuts()

        # ---- tray ----------------------------------------------------------
        try:
            self.tray = Tray(self)
            self.tray.setVisible(True)
            self.tray.act_quit.triggered.connect(self.close)
        except Exception:
            self.tray = None  # type: ignore[assignment]

        # ---- wires ---------------------------------------------------------
        self.state.log_message.connect(self._append_log)
        self.state.busy_changed.connect(self._on_busy)
        self.state.hardware_changed.connect(
            lambda hw: self.server_panel.set_hardware(hw)
        )
        self.pipeline_view.run_requested.connect(self._run_stage)
        self.pipeline_view.run_all_requested.connect(self._run_all)
        self.pipeline_view.stop_all_requested.connect(self._stop_all)
        self.pipeline_view.stop_stage_requested.connect(self._stop_stage)
        self.pipeline_view.approve_continue_requested.connect(
            self._on_approve_continue
        )
        self.pipeline_view.reset_run_state_requested.connect(
            self._on_reset_run_state
        )
        self.review_view.promote_requested.connect(self._promote)
        # Image redactions: when the operator hits "Save and continue
        # to Apply" inside the Images tab, the panel has already
        # written ``image_redactions.yml`` atomically; here we just
        # re-fire the same auto-queue Promote already runs, so apply
        # picks the operator's image decisions up transparently.
        self.review_view.image_save_continue_requested.connect(
            self._on_image_save_and_continue
        )
        # The Build button on the Build-preview tab is the single
        # explicit "commit to disk" gate: it triggers the same apply /
        # build / verify queue Promote used to fire automatically.
        self.review_view.build_requested.connect(self._on_build_requested)
        self.verifier_view.open_in_diff_requested.connect(self._open_in_diff)
        self.verifier_view.send_to_review_requested.connect(
            self._on_hits_to_pending
        )
        # Inline residuals on the Pipeline summary (replaces the old
        # standalone Verifier sidebar tab).
        self.state.verifier_changed.connect(self._on_verifier_report_changed)
        self.pipeline_view.open_verifier_requested.connect(
            self._on_open_verifier_report
        )
        self.pipeline_view.send_residuals_to_review_requested.connect(
            self._send_all_residuals_to_review
        )
        # Surface the Build artefacts the moment BuildWorker finishes:
        # the green banner with file count + Open-folder button is
        # driven by ``state.build_report_changed``. Without this hook
        # the user had no visual cue that the redacted PDFs were
        # actually on disk; the pipeline silently advanced to Verify /
        # Auto-resolve and the Build card just went green.
        self.state.build_report_changed.connect(
            self.pipeline_view.set_build_artifacts
        )
        self.pipeline_view.open_build_folder_requested.connect(
            self._on_open_build_folder
        )
        self.pipeline_view.view_build_report_requested.connect(
            self._on_open_verifier_report
        )

        self._workers: list = []
        self._run_all_queue: list[str] = []
        self._all_failed: bool = False
        self._current_workers: dict[str, object] = {}
        # Re-entrancy guard for the build-time leak warning. The
        # QMessageBox is modal so duplicate clicks are queued by Qt,
        # but the click handler itself can fire multiple times
        # before the dialog opens; this flag debounces it so only one
        # confirmation is shown per Build click burst.
        self._build_dialog_active: bool = False
        # Once the operator has confirmed "Build anyway" on the
        # un-handled-leak dialog for the current run, suppress further
        # dialogs in this same Run-all chain: the Review-tab Build
        # button and the Pipeline-card Build / Run-all all eventually
        # call _run_stage("build"), and we don't want to ask twice.
        # Reset on every fresh Run-all / closeEvent / project change.
        self._build_leak_ack: bool = False
        # One-shot flag: when True, the next scan ignores the existing map
        # cache so every leak is re-detected. Reset by ``_on_stage_finished``
        # at the end of the scan stage and by ``_clear_pipeline_state``.
        self._fresh_rescan: bool = False

        # Detect hardware (cheap, but threaded by Qt event loop)
        self.state.detect_hardware()

        # First-run wizard (suppressed during smoke tests)
        import os as _os
        if is_first_run() and not _os.environ.get("ANONYMIZE_SKIP_WIZARD"):
            self._run_first_run_wizard()

        # Honour the persistent "auto-start server on launch" toggle
        # the operator can flip in the Server panel. Defaults to off
        # so the existing UX (manual Start) is preserved unless the
        # user opts in.
        self._maybe_autostart_server()

        # Pre-warm QtWebEngine so the first "Show preview" click in
        # Review (and the first time the Build-preview tab opens) does
        # not pay the 200-400 ms Chromium subprocess cold-start.
        # Without this, the first instantiation of QWebEngineView
        # attaches a new GL surface to the main window and the
        # compositor briefly re-composites it, which users perceive
        # as "the window closes and reopens".
        # Deferred via singleShot so app launch isn't slowed; safely
        # no-ops when QtWebEngine is missing (tests, minimal installs).
        self._webengine_prewarm = None
        QTimer.singleShot(0, self._prewarm_webengine)

    def _prewarm_webengine(self) -> None:
        """Instantiate a hidden ``QWebEngineView`` so Chromium spawns
        once at startup, not on first user click.

        The view is parented to ``self`` so it stays alive for the
        lifetime of the window; it carries
        ``WA_DontShowOnScreen`` so no surface is ever attached, and
        an ``about:blank`` load triggers the actual subprocess boot.
        """
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except Exception:
            return
        try:
            view = QWebEngineView(self)
            view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            view.resize(1, 1)
            view.hide()
            view.setUrl(QUrl("about:blank"))
            self._webengine_prewarm = view
        except Exception:
            self._webengine_prewarm = None

    # ---- menu --------------------------------------------------------------

    def _build_menu(self) -> None:
        m = self.menuBar()
        file_menu = m.addMenu("&File")
        a_open_file = QAction(icon("file"), "Open file…", self)
        a_open_file.setShortcut(QKeySequence("Ctrl+O"))
        a_open_file.triggered.connect(self._action_open_file)
        a_open_files = QAction("Open multiple files…", self)
        a_open_files.setShortcut(QKeySequence("Ctrl+Shift+O"))
        a_open_files.triggered.connect(self._action_open_files)
        a_close = QAction("Close project", self)
        a_close.triggered.connect(self._action_close_project)
        a_quit = QAction("Quit", self)
        a_quit.setShortcut(QKeySequence.StandardKey.Quit)
        a_quit.triggered.connect(self.close)
        for a in (a_open_file, a_open_files):
            file_menu.addAction(a)
        file_menu.addSeparator()
        file_menu.addAction(a_close)
        file_menu.addSeparator()
        file_menu.addAction(a_quit)

        edit_menu = m.addMenu("&Edit")
        a_settings = QAction(icon("settings"), "Settings…", self)
        a_settings.setShortcut(QKeySequence("Ctrl+,"))
        a_settings.triggered.connect(self._open_settings)
        edit_menu.addAction(a_settings)

        run_menu = m.addMenu("&Run")
        a_run_all = QAction(icon("play"), "Run all stages", self)
        a_run_all.setShortcut(QKeySequence("Ctrl+R"))
        a_run_all.triggered.connect(self._run_all)
        a_stop_all = QAction(icon("stop"), "Stop all", self)
        a_stop_all.setShortcut(QKeySequence("Esc"))
        a_stop_all.triggered.connect(self._stop_all)
        a_export = QAction(icon("file"), "Export anonymized to PDF…", self)
        a_export.setShortcut(QKeySequence("Ctrl+E"))
        a_export.triggered.connect(self._export_to_pdf)
        run_menu.addAction(a_run_all)
        run_menu.addAction(a_stop_all)
        run_menu.addSeparator()
        run_menu.addAction(a_export)

        srv_menu = m.addMenu("&Server")
        a_panel = QAction("Open server panel", self)
        a_panel.triggered.connect(self._toggle_server_panel)
        a_mm = QAction(icon("download"), "Model Manager…", self)
        a_mm.triggered.connect(self._open_model_manager)
        a_edit_preset = QAction("Edit current preset…", self)
        a_edit_preset.triggered.connect(self._edit_current_preset)
        srv_menu.addAction(a_panel)
        srv_menu.addAction(a_mm)
        srv_menu.addAction(a_edit_preset)

        help_menu = m.addMenu("&Help")
        a_keys = QAction(icon("help"), "Keyboard shortcuts", self)
        a_keys.setShortcut(QKeySequence("F1"))
        a_keys.triggered.connect(self._show_shortcuts)
        a_about = QAction("About", self)
        a_about.triggered.connect(self._about)
        help_menu.addAction(a_keys)
        help_menu.addSeparator()
        help_menu.addAction(a_about)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("?"), self, activated=self._show_shortcuts)

    # ---- view switching ----------------------------------------------------

    _VIEW_MAP_KEYS = (
        "pipeline",
        "review",
        "diff",
        "server",
    )

    def _switch_view(self, key: str) -> None:
        # The legacy ``"verifier"`` key still works (clicked from
        # the Pipeline summary "View report" button), it just no
        # longer has a sidebar entry.
        target = {
            "pipeline": self.pipeline_view,
            "review": self.review_view,
            "diff": self.diff_view,
            "verifier": self.verifier_view,
            "server": self.server_panel,
        }.get(key)
        if target is None:
            return
        if self.state.project is None and target is not self.welcome and key != "server":
            self.stack.setCurrentWidget(self.welcome)
            return
        self.stack.setCurrentWidget(target)

    def _toggle_server_panel(self, *, force_open: bool = False) -> None:
        """Backward-compat shim: switch the active view to the server
        panel. The dock has been retired; the panel now lives in the
        sidebar Server tab."""
        self.sidebar.select("server")
        self.stack.setCurrentWidget(self.server_panel)

    # ---- sidebar "connect" indicator ---------------------------------------

    def _refresh_server_indicator(self) -> None:
        """Sync the small dot next to the Server sidebar entry with
        the current server state. Tooltip walks the operator through
        what a click will do in the current state."""
        prof = self.state.server.profile
        host_port = f"{prof.host}:{prof.port}" if prof else "?"
        if self.state.server_starting:
            state = "starting"
            tip = "llama-server is starting…\nClick to open the Server panel."
        elif self.state.server_online:
            state = "on"
            tip = (
                f"llama-server is online ({host_port}).\n"
                "Click to open the Server panel."
            )
        else:
            state = "off"
            tip = (
                "llama-server is offline.\n"
                "Click to start the active preset."
            )
        self.sidebar.set_indicator("server", state, tooltip=tip)

    def _on_sidebar_indicator(self, key: str) -> None:
        """Click on the per-row status indicator. Server entry has
        the only one for now; future rows can register more."""
        if key != "server":
            return
        # Already up or in flight: just route to the Server tab so
        # the operator can see live state / log.
        if self.state.server_online or self.state.server_starting:
            self._toggle_server_panel(force_open=True)
            return
        prof = self.state.server.profile
        ok, title, msg = self._preflight_server(prof)
        if not ok:
            # Misconfigured: route to Server tab AND surface a
            # specific, actionable error dialog so the operator
            # knows exactly what to fix.
            self._toggle_server_panel(force_open=True)
            dismissible_message(self, "warning", title, msg)
            return
        # Pre-flight OK: kick off the same code path the in-panel
        # Start button uses (the worker, the starting-flag toggle,
        # the diagnosis emitter on failure all carry over).
        try:
            self.server_panel._start()
        except Exception as e:
            self._toggle_server_panel(force_open=True)
            dismissible_message(
                self,
                "critical",
                "Could not start llama-server",
                f"Unexpected error launching the server:\n\n{e}",
            )

    def _preflight_server(self, prof) -> tuple[bool, str, str]:
        """Best-effort check that ``prof`` can possibly start.

        Returns ``(ok, title, message)``. When ``ok`` is False the
        title/message describe the user-actionable problem so the
        caller can surface it in a dialog.
        """
        if prof is None:
            return (
                False,
                "No server profile",
                "There is no server profile loaded.\n\n"
                "Open the Server panel to pick a preset.",
            )
        mode = getattr(prof, "deployment_mode", "local_binary") or "local_binary"
        if mode == "local_binary":
            binary = (getattr(prof, "binary", "") or "").strip()
            if not binary:
                return (
                    False,
                    "llama-server binary not configured",
                    "The active preset has no binary path set.\n\n"
                    "Open the Server panel and edit the preset to point "
                    "at the llama-server executable.",
                )
            bpath = Path(binary).expanduser()
            if not bpath.exists():
                return (
                    False,
                    "llama-server binary not found",
                    f"The active preset points at:\n  {bpath}\n\n"
                    "That file does not exist. Install llama.cpp or "
                    "edit the preset in the Server panel to point at "
                    "the correct binary.",
                )
            if not os.access(str(bpath), os.X_OK):
                return (
                    False,
                    "llama-server binary not executable",
                    f"The binary at:\n  {bpath}\n\n"
                    "exists but is not marked executable. Run "
                    "`chmod +x` on it or pick a different binary.",
                )
            if not prof.is_model_present():
                mp = getattr(prof, "model_path", None) or "(none)"
                return (
                    False,
                    "Model GGUF not on disk",
                    f"The active preset '{prof.name}' references the GGUF:\n  {mp}\n\n"
                    "Open the Model Manager to download it, or pick a "
                    "different preset whose model is already on disk.",
                )
        elif mode == "docker":
            if shutil.which("docker") is None:
                return (
                    False,
                    "Docker CLI not found",
                    "The active preset uses the docker deployment mode "
                    "but the docker CLI is not in PATH.\n\n"
                    "Install Docker (or switch the preset to "
                    "local_binary in the Server panel).",
                )
        elif mode == "external":
            healthy = False
            try:
                healthy = self.state.server.health(timeout=2.0)
            except Exception:
                healthy = False
            if not healthy:
                return (
                    False,
                    "External server unreachable",
                    f"The active preset is in external mode, pointing at:\n"
                    f"  {prof.host}:{prof.port}\n\n"
                    "No health response was received. Make sure the "
                    "external llama-server is running and reachable.",
                )
        return True, "", ""

    def _maybe_autostart_server(self) -> None:
        """Honour the persistent 'autostart_server' user preference.
        Called once after the main window is constructed, before any
        user input. No-op when the flag is off (default), or when
        pre-flight already says the active preset can't start (we
        prefer a quiet boot to a startup-time error dialog)."""
        try:
            from anonymize.app_settings import get_bool
        except Exception:
            return
        if not get_bool("autostart_server", default=False):
            return
        if self.state.server_online or self.state.server_starting:
            return
        ok, _title, _msg = self._preflight_server(self.state.server.profile)
        if not ok:
            return
        try:
            self.server_panel._start()
        except Exception as e:
            self._append_log(f"[autostart] failed: {e}")

    # ---- file menu ---------------------------------------------------------

    def _action_open_file(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Open file")
        if p:
            self._open_paths([Path(p)])

    def _action_open_files(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Open multiple files")
        if ps:
            self._open_paths([Path(p) for p in ps])

    def _action_open_folder(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Open dossier folder")
        if p:
            self._open_paths([Path(p)])

    def _action_close_project(self) -> None:
        self.state.set_project(None)
        self.state.set_candidates(auto_t0=[], auto_t1=[], pending=[])
        self.state.set_apply_report(None)
        self.state.set_build_report(None)
        self.state.set_verifier_report(None)
        self.stack.setCurrentWidget(self.welcome)

    # ---- dialogs -----------------------------------------------------------

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.state)
        dlg.exec()
        self.server_widget._poll()

    def _open_model_manager(self, repo_id: str = "") -> None:
        """Open the Model Manager. ``repo_id`` (when non-empty)
        lands the dialog on the Curated downloads tab with the
        matching catalog row pre-selected — used by the "Model not on
        disk" prompt and the per-preset Download button so the
        operator does not have to re-navigate to the row they just
        clicked.
        """
        dlg = ModelManagerDialog(self, initial_repo_id=repo_id or "")
        # Live-refresh the preset gallery whenever the library changes
        # (download finishes, file imported, file deleted) so the
        # "Download" / "Re-download" button flips state immediately,
        # while the dialog is still open.
        dlg.library_changed.connect(self.server_panel.gallery.refresh)
        dlg.exec()
        self.server_panel.gallery.refresh()

    def _edit_current_preset(self) -> None:
        prof = self.state.profile
        dlg = PresetEditor(
            prof,
            project_dir=(self.state.project.output_dir if self.state.project else None),
            parent=self,
        )
        if dlg.exec():
            self.server_panel.gallery.refresh()

    def _show_shortcuts(self) -> None:
        ShortcutsOverlay(self).exec()

    def _about(self) -> None:
        AboutDialog(self).exec()

    def _show_server_error(self, diag: Diagnosis) -> None:
        log_tail = "\n".join(self.state.server.tail(120))
        dlg = ServerErrorDialog(diag, log_tail=log_tail, parent=self)
        dlg.action_requested.connect(self._handle_diagnosis_action)
        dlg.exec()

    def _handle_diagnosis_action(self, code: str) -> None:
        if code.startswith("switch_preset:"):
            name = code.split(":", 1)[1]
            prof = get_profile(name)
            if prof:
                self.state.set_profile(prof)
                self.server_panel.manager = self.state.server
                self.server_panel.gallery.refresh()
                Toaster.notify("Preset switched", f"Now using '{name}'", kind="ok")
        elif code in ("open_model_manager", "redownload_model"):
            self._open_model_manager()
        elif code in ("browse_binary",):
            p, _ = QFileDialog.getOpenFileName(self, "Select llama-server binary")
            if p:
                # Mutating ``state.profile.binary`` in place forgets the
                # change on restart and never propagates to the YAML
                # source. Persist it through the same save path the
                # PresetEditor uses so the load order stays the source
                # of truth (project override wins over user override
                # wins over builtin).
                self.state.profile.binary = p
                try:
                    proj = self.state.project
                    if proj is not None:
                        save_project_profile(self.state.profile, proj.output_dir)
                    else:
                        save_user_profile(self.state.profile)
                except Exception as e:
                    dismissible_message(
                        self,
                        "warning",
                        "Could not save binary path",
                        f"The binary path was applied for this session but "
                        f"could not be persisted:\n\n{e}",
                    )
        elif code == "open_log":
            dismissible_message(
                self,
                "information",
                "Server log",
                "\n".join(self.state.server.tail(200)),
            )

    def _run_first_run_wizard(self) -> None:
        wiz = FirstRunWizard(hardware=self.state.hardware, parent=self)
        wiz.finished_with_preset.connect(self._on_first_run_preset)
        wiz.exec()

    def _on_first_run_preset(self, name: str) -> None:
        prof = get_profile(name)
        if prof:
            self.state.set_profile(prof)
            self.server_panel.manager = self.state.server
            self.server_panel.gallery.refresh()
            Toaster.notify("Welcome", f"Default preset: {name}", kind="info")

    def _on_profile_changed(self, name: str) -> None:
        prof = get_profile(name, project_dir=(self.state.project.output_dir if self.state.project else None))
        if prof is None:
            return
        self.state.set_profile(prof)
        self.server_panel.manager = self.state.server
        self.server_widget._poll()
        Toaster.notify("Preset changed", f"Now using '{name}'", kind="info")

    # ---- drag & drop -------------------------------------------------------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        paths = [Path(u.toLocalFile()) for u in urls if u.toLocalFile()]
        paths = [p for p in paths if p.exists()]
        if not paths:
            return
        e.acceptProposedAction()
        # Same anti-SIGSEGV trick as WelcomeView.dropEvent: defer the
        # modal dialog open to the next event-loop tick so we never
        # invoke ``QDialog.exec`` from inside the drag-and-drop walk.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda ps=paths: self._open_paths(ps))

    # ---- project lifecycle -------------------------------------------------

    def _open_paths(self, paths: list[Path]) -> None:
        if not paths:
            return
        dlg = ImportDialog(paths, self)
        if not dlg.exec():
            return
        # Drop any leftover state from a previous project before swapping.
        self._clear_pipeline_state(reason="silent")
        proj = dlg.to_project()
        out_root = proj.output_dir
        proj.pending_path = out_root / "needs_review.yml"
        proj.auto_t0_path = out_root / "auto_promoted_t0.yml"
        proj.auto_t1_path = out_root / "auto_promoted_t1.yml"
        proj.applied_path = out_root / "applied_substitutions.json"
        proj.verifier_report_path = out_root / "verifier_report.md"
        proj.decisions_path = out_root / "decisions_history.jsonl"
        # Image-redaction state lives next to the textual state files;
        # rebased here for the same reason: the project's output_dir
        # owns its run state, no shared-mailbox across projects.
        proj.image_inventory_path = out_root / "image_inventory.yml"
        proj.image_redactions_path = out_root / "image_redactions.yml"
        proj.image_thumbs_dir = out_root / ".anon" / "img_thumbs"
        # The substitution map MUST live inside the project's output
        # directory, otherwise every new project would inherit the
        # patterns accumulated by every previous project (the previous
        # default pointed at the repo's ``config/substitution_map.yml``,
        # which acted as a shared global mailbox across runs).  The
        # category rules (leak patterns / safe terms) stay shipped from
        # the repo because they are read-only configuration.
        proj.map_path = out_root / "substitution_map.yml"
        proj.patterns_path = Path("config/leak_patterns.yml").resolve()
        proj.safe_terms_path = Path("config/safe_terms.yml").resolve()
        proj.llm_url = self.state.profile.base_url
        proj.server_profile_name = self.state.profile.name
        proj.concurrency = max(1, self.state.profile.parallel)
        # Detector mode (single vs multipass) is a user-scope
        # preference written by the Server tab's combo box; reflect it
        # into the Project so this run's detector picks the right
        # prompts. Default ``"single"`` preserves the historic
        # behaviour when the preference has never been set.
        try:
            from anonymize.app_settings import get_str

            mode = get_str("detector_mode", default="single")
            if mode in ("single", "multipass"):
                proj.detector_mode = mode  # type: ignore[assignment]
        except Exception:
            pass
        # Decide whether to reuse existing state in the output folder.
        # If any of the run-state files already exist, this is a
        # *re-open* of a project the user has already worked on, so we
        # MUST preserve substitution_map / auto-promoted lists /
        # pending list / decisions_history (the latter holds
        # stable_index assignments, wiping it would make the same
        # phone number resolve to a different placeholder on the next
        # run).  For pristine output folders (first time the user
        # imports this destination) we do nothing, the next scan
        # populates everything from scratch.
        existing_state = any(
            p.exists()
            for p in (
                proj.auto_t0_path,
                proj.auto_t1_path,
                proj.pending_path,
                proj.decisions_path,
            )
        )
        # Re-scan only when there's no existing state to honour.
        proj.force_rescan = not existing_state

        self.state.set_project(proj)
        self.server_panel.set_project_dir(out_root)
        # Empty the in-memory buffers first so the views never show
        # the *previous* project's data while the new state loads.
        self.state.set_candidates(auto_t0=[], auto_t1=[], pending=[])
        self.state.set_apply_report(None)
        self.state.set_build_report(None)
        self.state.set_verifier_report(None)
        # Reload the substitution map for the new project so the
        # Review view doesn't keep showing the previous project's entries.
        try:
            new_map = SubstitutionMap.load(proj.map_path)
            self.state.smap = new_map
            self.state.map_changed.emit(new_map)
        except Exception:
            pass
        # Re-hydrate the candidate buckets from disk so a re-opened
        # project shows the same Review state the user left it in.
        if existing_state:
            try:
                from anonymize.triage import read_candidates_yaml

                auto_t0 = (
                    read_candidates_yaml(proj.auto_t0_path)
                    if proj.auto_t0_path.exists()
                    else []
                )
                auto_t1 = (
                    read_candidates_yaml(proj.auto_t1_path)
                    if proj.auto_t1_path.exists()
                    else []
                )
                pending = (
                    read_candidates_yaml(proj.pending_path)
                    if proj.pending_path.exists()
                    else []
                )
                self.state.set_candidates(
                    auto_t0=auto_t0, auto_t1=auto_t1, pending=pending
                )
            except Exception as e:
                self._append_log(f"[reopen] failed to load candidate state: {e}")
        for c in self.pipeline_view.cards.values():
            c.reset()
        self.pipeline_view.reset_all_paused()
        self.pipeline_view.set_locked(False)
        self.pipeline_view.set_summary("Ready. Click Run to start.", percent=0)
        self.sidebar.select("pipeline")
        self.stack.setCurrentWidget(self.pipeline_view)
        self._append_log(f"opened project: mode={proj.mode}  output={proj.output_dir}")
        Toaster.notify("Project opened", f"{proj.mode}  ·  {proj.output_dir.name}", kind="info")

    # ---- workers / stages --------------------------------------------------

    def _start_worker(self, worker, *, label: str) -> None:
        self._workers.append(worker)
        self._current_workers[label] = worker
        self.state.set_busy(True, label)
        card = self.pipeline_view.card(label)
        if card is not None:
            card.set_running(True)
        worker.signals.log.connect(self._append_log)
        worker.signals.progress.connect(
            lambda d, t, lbl: self._on_stage_progress(label, d, t, lbl)
        )
        worker.signals.cancelled.connect(lambda: self._on_stage_cancelled(label))
        worker.signals.finished.connect(
            lambda ok, msg, extras: self._on_stage_finished(label, ok, msg, extras)
        )
        worker.start()

    # Per-stage span on the global summary bar. Each stage gets a
    # contiguous slice of [0, 100], advancing linearly as the stage's
    # internal ``progress`` callback fires. This is what makes the
    # simple-mode bar move smoothly instead of jumping in chunks at
    # stage-finish time only.
    _STAGE_PROGRESS_RANGE = {
        "scan": (0, 30),
        "promote": (30, 38),
        "apply": (38, 70),
        "build": (70, 80),
        "verify": (80, 92),
        "auto_resolve": (92, 100),
    }

    def _on_stage_progress(self, label: str, done: int, total: int, lbl: str) -> None:
        card = self.pipeline_view.card(label)
        if card is not None:
            card.set_progress(done, total, lbl)
        self.busy_label.setText(f"{label}: {lbl}")
        # Map per-stage progress into the global summary bar so the
        # simple-mode UI advances smoothly as each stage runs (instead
        # of staying flat until the stage finishes).
        if total > 0:
            lo, hi = self._STAGE_PROGRESS_RANGE.get(label, (0, 0))
            if hi > lo:
                pct = lo + (hi - lo) * (max(0, done) / total)
                self.pipeline_view.set_summary(
                    f"{label}: {lbl}", percent=int(pct)
                )

    def _on_stage_cancelled(self, label: str) -> None:
        card = self.pipeline_view.card(label)
        if card is not None:
            card.set_cancelled()
        self._append_log(f"[{label}] cancelled")
        Toaster.notify("Cancelled", f"{label} stopped", kind="warn", pipeline_event=True)
        self.state.set_busy(False, "")
        self._current_workers.pop(label, None)
        self._clear_pipeline_state(reason="cancelled")

    def _on_stage_finished(self, label: str, ok: bool, msg: str, extras: dict) -> None:
        card = self.pipeline_view.card(label)
        if card is not None and msg != "cancelled":
            card.set_finished(ok, msg)
        self._append_log(f"[{label}] {msg}")
        self.state.set_busy(False, "")
        self._current_workers.pop(label, None)
        # Mirror per-stage progress into the simple-mode summary so
        # the user sees a meaningful status without having to expand
        # the details panel.
        try:
            human = {
                "scan": "Scan & detect",
                "promote": "Approve & promote",
                "apply": "Apply",
                "build": "Build",
                "verify": "Verify",
                "auto_resolve": "Auto-resolve residuals",
            }.get(label, label)
            # Snap the summary bar to the high end of this stage's
            # range so the bar stays at 100% once the last stage
            # ends (auto_resolve.hi == 100).
            _lo, hi = self._STAGE_PROGRESS_RANGE.get(label, (0, 0))
            self.pipeline_view.set_summary(
                f"{human}: {msg}",
                percent=hi or None,
            )
        except Exception:
            pass
        if not ok and msg != "cancelled":
            self._all_failed = True
            Toaster.notify(label, msg, kind="err", pipeline_event=True)
        elif ok:
            Toaster.notify(label, msg, kind="ok", pipeline_event=True)
        self._workers = [w for w in self._workers if w.isRunning()]

        # We intentionally do NOT clear ``force_rescan`` after the scan
        # stage: every run must re-detect leaks from scratch so that
        # switching documents (or re-opening the same one) never picks up
        # stale results from the previous project's substitution_map cache.
        if label == "scan":
            self._fresh_rescan = False

        if not (self._run_all_queue and msg != "cancelled"):
            return

        # ---- approval gate ------------------------------------------------
        if label == "scan":
            self._enter_paused_state()
            return

        # After server-side ``promote`` (merge YAML into the map), refresh state.
        if label == "promote" and ok:
            proj = self.state.project
            if proj is not None:
                try:
                    new_map = SubstitutionMap.load(proj.map_path)
                    self.state.smap = new_map
                    self.state.map_changed.emit(new_map)
                except Exception:
                    pass

        # After auto_resolve, re-load the pending list from disk and
        # surface any leftover candidates the LLM auditor sent to
        # Review (low-confidence typos / concatenations the operator
        # must approve). The user explicitly asked: "quando rimangono
        # x candidati dovrei essere spostato su review e decidere
        # cosa fare".
        if label == "auto_resolve" and ok:
            self._refresh_pending_from_disk_and_prompt_review()

        next_stage = self._run_all_queue.pop(0) if self._run_all_queue else None
        if next_stage is None:
            # Queue drained: the pipeline (or this manual stage) is
            # truly finished. Without an explicit "done" message the
            # summary used to keep showing whichever ``Working: X``
            # status was last set by ``_on_busy``, leaving the user
            # unsure whether anything was still running. Mark the
            # state plainly so the inline residuals box is the only
            # remaining call-to-action.
            try:
                rep = self.state.verifier_report
                residuals = len(getattr(rep, "hits", []) or []) if rep else 0
                build_rep = self.state.build_report
                output_path = ""
                if build_rep is not None:
                    artefacts = list(
                        getattr(build_rep, "artefacts", []) or []
                    )
                    if artefacts:
                        from os.path import commonpath
                        try:
                            paths = [str(p) for p in artefacts]
                            output_path = (
                                commonpath(paths)
                                if len(paths) > 1
                                else str(artefacts[0].parent)
                            )
                        except Exception:
                            output_path = (
                                str(artefacts[0].parent)
                                if hasattr(artefacts[0], "parent")
                                else ""
                            )
                if not ok and msg != "cancelled":
                    self.pipeline_view.set_summary(
                        f"Pipeline finished with errors, see logs.",
                        percent=100,
                    )
                else:
                    path_chunk = (
                        f" · output: {output_path}" if output_path else ""
                    )
                    if residuals > 0:
                        residual_chunk = (
                            f" · {residuals} residual leak"
                            + ("s" if residuals != 1 else "")
                        )
                    else:
                        residual_chunk = " · clean"
                    self.pipeline_view.set_summary(
                        "✓ Pipeline complete" + path_chunk + residual_chunk,
                        percent=100,
                    )
            except Exception:
                pass
            return
        # ``verify`` and ``auto_resolve`` are deterministic housekeeping
        # passes that consume the substitution map and never write
        # destructive output, they must always run as the closing
        # stages of the pipeline so the user sees the residual report
        # and the auto-derived placeholders even after a partial
        # failure upstream. The other stages are gated on _all_failed
        # to avoid running build/apply on broken state.
        if not self._all_failed or next_stage in ("verify", "auto_resolve"):
            self._run_stage(next_stage, from_queue=True)
        else:
            self._run_all_queue.clear()

    def _refresh_pending_from_disk_and_prompt_review(self) -> None:
        """After ``stage_auto_resolve_residuals`` completes, surface
        whatever the loop produced so the operator can act on it.

        Two outcomes worth interrupting the user for:

        1. New candidates landed in ``needs_review.yml`` (typically
           the LLM auditor's lower-confidence proposals). We switch
           to the Review tab so they can be approved / rejected.
        2. No new candidates *but* the verifier still reports
           residual hits (e.g. text the in-place PDF redactor cannot
           reach because of font-fragmentation). We switch to the
           Verifier view so the operator can use "Send all to Review"
           and triage them by hand.

        Without these hooks the user would be left on the Pipeline
        view with a transient toast, the items would still be on
        disk but easy to miss.
        """
        proj = self.state.project
        if proj is None:
            return
        try:
            from anonymize.triage import read_candidates_yaml

            on_disk = read_candidates_yaml(proj.pending_path)
        except Exception:
            on_disk = []
        existing = {c.value for c in self.state.pending}
        new_candidates = [c for c in on_disk if c.value not in existing]
        if new_candidates:
            self.state.set_candidates(pending=on_disk)
            self._append_log(
                f"[auto_resolve] {len(new_candidates)} candidate(s) need "
                "your review, switching to the Review tab."
            )
            Toaster.notify(
                "Review needed",
                f"{len(new_candidates)} candidate(s) detected by the auditor "
                "need your decision",
                pipeline_event=True,
                kind="warn",
            )
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)
            return

        # No new audit candidates, the deterministic + char-level
        # PASS 3 in :class:`PdfInplaceAdapter` already handles
        # leftover regression hits (the user's already-approved leaks
        # the first ``search_for`` could not locate due to PDF
        # text-fragmentation). If anything still slips through it
        # will show up in ``verifier_report.md``; we don't pop the
        # user into the Verifier view because the operator already
        # green-lit those values in the post-scan Review and there is
        # nothing left for them to decide.

    # ---- paused / approval gate -------------------------------------------

    def _enter_paused_state(self) -> None:
        proj = self.state.project
        scan_card = self.pipeline_view.card("scan")
        promote_card = self.pipeline_view.card("promote")
        n_pending = len(self.state.pending)
        n_t0 = len(self.state.auto_t0)
        n_t1 = len(self.state.auto_t1)

        if proj is not None:
            try:
                mark_paused(
                    proj,
                    why=(
                        f"awaiting approval after scan: pending={n_pending} "
                        f"t0={n_t0} t1={n_t1}"
                    ),
                )
            except Exception:
                pass

        self.pipeline_view.set_locked(True)

        if scan_card is not None:
            scan_card.set_finished(
                True,
                f"done, {n_t0} T0 + {n_t1} T1 auto · {n_pending} pending review",
            )

        if n_pending > 0:
            self._append_log(
                f"[approval] {n_pending} candidate(s) require review "
                "before automatic apply. Triage them in the Review view."
            )
            Toaster.notify(
                "Review needed",
                f"{n_pending} candidate(s) await your approval",
                kind="warn",
                pipeline_event=True,
            )
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)
            if promote_card is not None:
                promote_card.set_paused_for_approval(
                    primary=False,
                    message=(
                        f"PAUSED, review {n_pending} item(s) in Review, "
                        "then use 'Promote approved' or click Approve & continue "
                        "below to merge what is on disk and continue"
                    ),
                )
        elif (n_t0 + n_t1) > 0:
            # Found auto-promoted items but nothing ambiguous to triage.
            # Pause anyway so the operator sees the count and clicks
            # Approve & continue explicitly, auto-merging silently makes
            # the Approve & promote stage feel "skipped" and erodes the
            # human-in-the-loop story.
            self._append_log(
                f"[approval] {n_t0 + n_t1} auto-promoted item(s) ready to "
                "merge; click Approve & continue when you're ready."
            )
            Toaster.notify(
                "Ready to merge",
                f"{n_t0 + n_t1} auto-approved item(s), click Approve & continue",
                kind="info",
                pipeline_event=True,
            )
            # Same auto-switch behaviour as the n_pending>0 branch: take
            # the operator straight to the Review view so they can see
            # what was auto-promoted and unpromote individual rows
            # before clicking Approve & continue.
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)
            if promote_card is not None:
                promote_card.set_paused_for_approval(
                    primary=True,
                    message=(
                        f"PAUSED, {n_t0 + n_t1} auto-promoted item(s) ready. "
                        "Nothing requires manual review. Click Approve & "
                        "continue to merge them into substitution_map.yml."
                    ),
                )
        else:
            # No new candidates AND no auto-promoted rows. Common case
            # when the project was already fully reviewed in a past
            # session: scan re-runs against the same input but every
            # value is already mapped, so nothing new surfaces. The
            # human-in-the-loop story is the same as for the other
            # branches: ALWAYS pause at Review so the operator can
            # confirm "yes, current state is what I want" and click
            # Approve & continue, even when the explicit ask is
            # zero. Auto-skipping made re-runs feel unsupervised
            # ("perchè va in automatico?"), which is the opposite of
            # the human-in-the-loop guarantee the rest of the
            # pipeline gives.
            self._append_log(
                "[approval] scan found 0 new candidates; pausing at "
                "Review so the operator can confirm the existing map."
            )
            Toaster.notify(
                "Review checkpoint",
                "0 new candidates, click Approve & continue to proceed",
                kind="info",
                pipeline_event=True,
            )
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)
            if promote_card is not None:
                promote_card.set_paused_for_approval(
                    primary=True,
                    message=(
                        "PAUSED, scan found 0 new candidates. Existing "
                        "substitution_map.yml is already up to date. "
                        "Click Approve & continue to proceed (or edit "
                        "the map first)."
                    ),
                )

    def _on_approve_continue(self, key: str) -> None:
        """User acknowledged the post-scan gate on the **Approve & promote** card."""
        if key != "promote" or not self._run_all_queue:
            return
        # Pre-promote leak gate: surface a confirmation dialog when the
        # Review queue still contains pending candidates the operator
        # has either not reviewed (decision in {None, "pending"}) or
        # explicitly skipped. stage_promote auto-merges every non-skip
        # pending into the substitution map, so the legacy pre-build
        # gate would never fire on the Run-all path — this is the only
        # place where a user who clicked Run and went straight to
        # Approve & continue can still be asked to confirm.
        if not self._build_leak_ack:
            if not self._gate_build_with_leaks(
                from_queue=True, stage="approval"
            ):
                return
        proj = self.state.project
        if proj is not None:
            try:
                clear_pause_marker(proj)
            except Exception:
                pass
        promote_card = self.pipeline_view.card("promote")
        if promote_card is not None:
            promote_card.reset_paused_state()
        self.pipeline_view.set_locked(False)
        next_stage = self._run_all_queue.pop(0)
        self._append_log(
            f"[approval] approved, resuming pipeline at '{next_stage}'"
        )
        Toaster.notify(
            "Pipeline resumed",
            f"continuing with {next_stage}",
            kind="info",
            pipeline_event=True,
        )
        self._run_stage(next_stage, from_queue=True)

    def _on_reset_run_state(self) -> None:
        proj = self.state.project
        if proj is None:
            dismissible_message(self, "warning", "No project", "Open a project first.")
            return
        ans = dismissible_message(
            self,
            "question",
            "Reset run state",
            (
                "Delete the previous run files (auto_promoted_t0/t1.yml, "
                "needs_review.yml, applied_substitutions.json, "
                "decisions_history.jsonl, verifier_report.md) and re-detect "
                "every leak as if this were the first run?\n\n"
                "The global substitution_map.yml is NOT touched."
            ),
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            default_button=QMessageBox.StandardButton.Ok,
            dismissible=False,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        try:
            rep = reset_run_state(proj)
            removed = rep.get("removed", [])
        except Exception as e:
            dismissible_message(self, "critical", "Reset failed", str(e))
            return
        self._append_log(
            f"[reset] cleared {len(removed)} state files: {', '.join(removed) or '(nothing to remove)'}"
        )
        Toaster.notify(
            "Run state reset",
            f"{len(removed)} files removed; click Run to re-scan",
            kind="info",
            pipeline_event=True,
        )
        self.state.set_candidates(auto_t0=[], auto_t1=[], pending=[])
        self.state.set_apply_report(None)
        self.state.set_build_report(None)
        self.state.set_verifier_report(None)
        self.pipeline_view.reset_all_paused()
        for card in self.pipeline_view.cards.values():
            card.reset()
        self.pipeline_view.set_summary("Ready. Click Run to start.", percent=0)
        self._fresh_rescan = True
        proj.force_rescan = True

    def _sync_runtime_from_profile(self, proj) -> None:
        """Mirror the active server profile into the project's runtime
        knobs (``llm_url`` + ``concurrency``).

        These two fields used to be set once at project-open time and
        then drift away from the live preset whenever the user edited
        it via Customize / Save. Calling this before every stage keeps
        the GUI's run faithful to whatever the preset currently says,
        with no separate Settings field to keep in sync.
        """
        try:
            prof = self.state.profile
        except Exception:
            return
        if proj is None or prof is None:
            return
        try:
            proj.llm_url = prof.base_url
            proj.concurrency = max(1, int(getattr(prof, "parallel", 1) or 1))
            proj.server_profile_name = prof.name
        except Exception:
            pass
        # Pick up the latest detector-mode preference too: the user
        # may have flipped Fast / High accuracy in the Server tab
        # between two stages of the same run, and we want the next
        # detector call to honour it without forcing a project
        # reopen.
        try:
            from anonymize.app_settings import get_str

            mode = get_str("detector_mode", default="single")
            if mode in ("single", "multipass"):
                proj.detector_mode = mode  # type: ignore[assignment]
        except Exception:
            pass

    def _run_stage(self, label: str, *, from_queue: bool = False) -> None:
        proj = self.state.project
        if proj is None:
            dismissible_message(self, "warning", "No project", "Open a project first.")
            return
        # When the user clicks "Run" on an individual stage card the
        # pending Run-all queue (left over from a previous Run all) must
        # be cleared so we don't auto-advance through every other stage
        # behind the user's back. The internal callers that *are*
        # draining the queue pass ``from_queue=True``.
        if not from_queue:
            self._run_all_queue.clear()
        # Re-sync the project's runtime knobs (LLM endpoint + worker
        # parallelism) from the currently-active server profile *every*
        # time a stage starts. Without this the values remain frozen at
        # the snapshot taken in _open_paths and the user can edit
        # ``parallel`` in the preset without the pipeline noticing.
        self._sync_runtime_from_profile(proj)
        # Build gate: refuse to spawn BuildWorker silently when the
        # review queue still contains un-handled leaks. The same
        # confirmation used to live behind the Review-tab Build button
        # only, so clicking Run-all or the Pipeline-card Build button
        # bypassed it entirely. _build_leak_ack is pre-set by
        # _on_build_requested so the dialog never fires twice when the
        # flow originates from the Review tab.
        if label == "build" and not self._build_leak_ack:
            if not self._gate_build_with_leaks(from_queue=from_queue):
                return
        if label == "scan":
            w = ScanWorker(proj, self)
            w.signals.result.connect(self._on_scan_result)
            self._start_worker(w, label="scan")
        elif label == "promote":
            w = PromoteWorker(proj, None, self)
            self._workers.append(w)
            self._current_workers[label] = w
            self.state.set_busy(True, "promote")
            pc = self.pipeline_view.card("promote")
            if pc is not None:
                pc.set_running(True)
            w.signals.log.connect(self._append_log)
            w.signals.finished.connect(
                lambda ok, msg, extras: self._on_stage_finished(
                    "promote", ok, msg, extras or {}
                )
            )
            w.start()
        elif label == "apply":
            w = ApplyWorker(proj, self)
            w.signals.result.connect(self.state.set_apply_report)
            self._start_worker(w, label="apply")
        elif label == "build":
            w = BuildWorker(proj, self)
            w.signals.result.connect(self.state.set_build_report)
            self._start_worker(w, label="build")
        elif label == "verify":
            w = VerifyWorker(proj, self)
            w.signals.result.connect(self.state.set_verifier_report)
            self._start_worker(w, label="verify")
        elif label == "auto_resolve":
            w = AutoResolveWorker(proj, self)
            w.signals.result.connect(self.state.set_verifier_report)
            self._start_worker(w, label="auto_resolve")

    def _stop_stage(self, label: str) -> None:
        w = self._current_workers.get(label)
        if w and hasattr(w, "request_stop"):
            try:
                w.request_stop()
                Toaster.notify("Stopping", f"{label}…", kind="warn", pipeline_event=True)
            except Exception:
                pass

    def _stop_all(self) -> None:
        active = [
            w for w in self._current_workers.values()
            if hasattr(w, "request_stop")
        ]
        for w in active:
            try:
                w.request_stop()
            except Exception:
                pass
        self._clear_pipeline_state(reason="stopped")
        # Surface "we asked the workers to stop, waiting for them to
        # ack" on the Pipeline summary. Stages that are mid-LLM-call
        # may take a couple of seconds to actually exit (we cancel
        # queued futures but the in-flight HTTP requests run to
        # completion). The summary updates again automatically when
        # ``_on_stage_finished`` fires for the stage that ack'd Stop.
        try:
            self.pipeline_view.set_summary(
                f"Stopping {len(active)} stage(s), waiting for "
                f"the in-flight LLM call(s) to ack…",
                percent=None,
            )
        except Exception:
            pass
        Toaster.notify(
            "Stopping pipeline",
            f"asked {len(active)} stage(s) to halt at next checkpoint",
            kind="warn",
            pipeline_event=True,
        )

    # ---- shared cleanup of pipeline state ---------------------------------

    def _clear_pipeline_state(self, *, reason: str) -> None:
        """Drop the run-all queue, paused-state visuals and the persisted
        ``paused`` marker in ``.anon/state.json``.

        Called on Stop, on stage cancellation, on closeEvent and when
        opening a new project so the UI never starts with leftover state.
        ``force_rescan`` is left ``True`` so every run keeps reprocessing
        from scratch instead of reusing the substitution_map cache.
        """
        self._run_all_queue.clear()
        self._build_leak_ack = False
        self._fresh_rescan = True
        proj = self.state.project
        if proj is not None:
            proj.force_rescan = True
            try:
                clear_pause_marker(proj)
            except Exception:
                pass
        try:
            self.pipeline_view.reset_all_paused()
            self.pipeline_view.set_locked(False)
        except Exception:
            pass
        if reason and reason != "silent":
            self._append_log(f"[pipeline] cleared paused state ({reason})")

    def _run_all(self) -> None:
        if self.state.project is None:
            dismissible_message(self, "warning", "No project", "Open a project first.")
            return
        # Defense-in-depth: PipelineView's Run button is already
        # disabled when the server is offline or the slot budget is
        # too tight, but a keyboard shortcut could still call this.
        # Refuse loudly so the user understands why.
        if not getattr(self.state, "server_online", False):
            dismissible_message(
                self,
                "warning",
                "llama-server offline",
                "The local llama-server is not responding. Start it "
                "from the <b>Server</b> view (sidebar) or wait for the "
                "auto-restart, then click Run again.",
            )
            return
        from anonymize.budget import check_slot_budget

        prof = self.state.profile
        budget = check_slot_budget(
            ctx_size=int(prof.ctx_size),
            parallel=int(prof.parallel),
        )
        if not budget.fits:
            dismissible_message(
                self,
                "warning",
                "Token budget too tight",
                "The active server preset cannot fit a single chunk in "
                "one slot, llama-server would OOM on the first "
                "request.<br><br>"
                f"<i>{budget.reason}</i><br><br>"
                "Edit the preset (Server → Customize) or pick a "
                "different one and click Run again.",
            )
            return
        self._all_failed = False
        # Fresh Run-all: clear the leak-ack so the build gate fires
        # exactly once for this chain (or not at all when the review
        # queue is empty).
        self._build_leak_ack = False
        # Drop the stale "Build complete" banner from the previous
        # run so the operator does not mistake the prior artefacts
        # for the ones this Run-all is about to produce.
        try:
            self.pipeline_view.set_build_artifacts(None)
        except Exception:
            pass
        # Reset the in-layout activity feed so the Pipeline tab does
        # not carry forward events from a previous run.
        try:
            self.pipeline_view.activity_feed.clear()
        except Exception:
            pass
        self.pipeline_view.set_locked(False)
        for c in self.pipeline_view.cards.values():
            c.reset()
        self.pipeline_view.reset_all_paused()
        self.pipeline_view.set_summary("Starting pipeline…", percent=0)
        # The queue is what runs AFTER scan; we always pause at the gate
        # before draining it. ``promote`` is mandatory: without it the
        # newly-found T0/T1 candidates would never make it into the map and
        # ``apply`` would not see them.
        proj = self.state.project
        queue = ["promote", "apply", "build", "verify"]
        if proj is not None and getattr(proj, "auto_resolve_residuals", True):
            queue.append("auto_resolve")
        self._run_all_queue = queue
        self._run_stage("scan", from_queue=True)

    def _on_scan_result(self, payload: dict) -> None:
        triage = payload.get("triage")
        if triage is None:
            return
        self.state.set_candidates(
            auto_t0=triage.auto_t0,
            auto_t1=triage.auto_t1,
            pending=triage.needs_review,
        )
        if triage.needs_review:
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)

    def _promote(self, approved: list[Candidate]) -> None:
        proj = self.state.project
        if proj is None:
            return
        w = PromoteWorker(proj, approved, self)
        w.signals.finished.connect(
            lambda ok, msg, extras: self._on_promote_done(ok, msg, approved)
        )
        w.signals.log.connect(self._append_log)
        self._workers.append(w)
        self.state.set_busy(True, "promote")
        w.start()

    def _on_promote_done(self, ok: bool, msg: str, approved: list[Candidate]) -> None:
        self.state.set_busy(False, "")
        self._append_log(f"[promote] {msg}")
        proj = self.state.project
        if proj is None:
            return
        try:
            new_map = SubstitutionMap.load(proj.map_path)
            self.state.smap = new_map
            self.state.map_changed.emit(new_map)
            # Diagnose silent drops: ``stage_promote`` happily skips a
            # candidate when (a) its ``from`` is already in the same
            # category, (b) its placeholder ended up identical to the
            # value (auto-derive failed), or (c) ``merge_candidates``
            # rejected it for any other reason. Count what's missing
            # so the user understands why the residuals box stays lit.
            if approved:
                map_keys = new_map.keys() if hasattr(new_map, "keys") else set()
                dropped = [c for c in approved if c.value not in map_keys]
                if dropped:
                    sample = ", ".join(f"`{d.value}`" for d in dropped[:3])
                    if len(dropped) > 3:
                        sample += f", … (+{len(dropped) - 3} more)"
                    self._append_log(
                        f"[promote] {len(dropped)} approved entr(ies) did NOT "
                        f"land in the map (already mapped under another "
                        f"category, or no usable placeholder): {sample}"
                    )
                    Toaster.notify(
                        "Promote: partial",
                        f"{len(dropped)} approved entr(ies) skipped, see log",
                        kind="warn",
                        pipeline_event=True,
                    )
        except Exception:
            pass
        # ``stage_promote`` prunes auto_*.yml + needs_review.yml of
        # entries that ended up in the map, so reload all three from
        # disk to keep the in-memory state in sync.  The earlier
        # set_candidates(pending=…) below was insufficient, the
        # Review tree was rendering pre-promote auto rows alongside
        # the freshly-merged map rows.
        try:
            from anonymize.triage import read_candidates_yaml

            new_auto_t0 = (
                read_candidates_yaml(proj.auto_t0_path)
                if proj.auto_t0_path.exists()
                else []
            )
            new_auto_t1 = (
                read_candidates_yaml(proj.auto_t1_path)
                if proj.auto_t1_path.exists()
                else []
            )
            new_pending = (
                read_candidates_yaml(proj.pending_path)
                if proj.pending_path.exists()
                else []
            )
            self.state.set_candidates(
                auto_t0=new_auto_t0,
                auto_t1=new_auto_t1,
                pending=new_pending,
            )
        except Exception as e:
            self._append_log(f"[promote] failed to reload candidate state: {e}")
        if self._run_all_queue and self._run_all_queue[0] == "promote":
            if ok:
                self._run_all_queue.pop(0)
            else:
                self._run_all_queue.clear()
        self.pipeline_view.set_locked(False)
        try:
            clear_pause_marker(proj)
        except Exception:
            pass
        promote_card = self.pipeline_view.card("promote")
        if promote_card is not None:
            promote_card.reset_paused_state()
            promote_card.set_finished(
                ok, f"Review: +{len(approved)} merged" if ok else msg
            )
        if not ok:
            # Failed promote: surface a stub log line and let the
            # operator look at the Pipeline card.  The run-all queue
            # is already cleared above (the dedup path).
            return
        # Successful promote: route the operator into the next
        # review stage instead of auto-applying.  Image review
        # and the build-preview tab are explicit pauses; the Build
        # button is the single "commit to disk" gate.  Any leftover
        # queue tail from a Run-all is dropped here on purpose: the
        # Build button rebuilds the canonical apply / build / verify
        # queue from project flags so nothing is lost.
        if self._run_all_queue:
            self._append_log(
                "[promote] queued stages (apply / build / verify) deferred "
                "to the Build button so the operator can review images "
                "first."
            )
            self._run_all_queue.clear()
        try:
            self.review_view.enable_image_tab()
        except Exception as e:
            self._append_log(f"[promote] image tab enable failed: {e}")
        try:
            self.sidebar.select("review")
            self.stack.setCurrentWidget(self.review_view)
        except Exception:
            pass
        has_images = self._project_has_images(proj)
        if has_images:
            try:
                self.review_view.focus_image_tab()
            except Exception as e:
                self._append_log(
                    f"[promote] focus image tab failed: {e}"
                )
            Toaster.notify(
                "Promote complete",
                "review the embedded images, then Build",
                kind="info",
                pipeline_event=True,
            )
        else:
            try:
                self.review_view.enable_build_tab()
                self.review_view.focus_build_tab()
            except Exception as e:
                self._append_log(
                    f"[promote] focus build-preview tab failed: {e}"
                )
            Toaster.notify(
                "Promote complete",
                "no embedded images, jumped to Build preview",
                kind="info",
                pipeline_event=True,
            )

    def _project_has_images(self, proj) -> bool:
        """``True`` when ``image_inventory.yml`` lists at least one
        embedded image. Falls back to ``False`` when the inventory
        cannot be loaded so the operator gets the safer "skip to
        Build" experience instead of staring at an empty Images tab.
        """
        try:
            from anonymize.image_inventory import load_inventory

            inv = load_inventory(Path(proj.image_inventory_path))
            return any(
                len(f.images) > 0 for f in (inv.files if inv else [])
            )
        except Exception:
            return False

    def _on_image_save_and_continue(self) -> None:
        """The operator saved their image redactions and asked to
        continue. Route them to the Build-preview tab so they can
        confirm the final look before the actual Apply runs.
        """
        proj = self.state.project
        if proj is None:
            return
        try:
            self.review_view.enable_build_tab()
            self.review_view.focus_build_tab()
        except Exception as e:
            self._append_log(
                f"[image-review] focus build-preview tab failed: {e}"
            )
        Toaster.notify(
            "Image review saved",
            "preview the build, then click Build to commit",
            kind="info",
        )

    def _on_build_requested(self) -> None:
        """Operator clicked Build on the Build-preview tab.

        Before kicking off ``apply / build / verify`` we re-check the
        Review state for un-handled leaks (pending detections that the
        operator has neither approved nor deleted). When any are
        present, a friendly confirmation dialog asks whether to
        proceed; clicking "Back to review" focuses the first leak so
        the operator can act on it. Clicking "Build anyway" proceeds
        with the user's current selection (no second pass).
        """
        proj = self.state.project
        if proj is None:
            return
        if self._build_dialog_active:
            # Debounce double-clicks: a modal dialog is already up.
            return
        # Fresh flow from the Build-preview tab: clear any stale ack
        # from a previous Run-all so the gate fires when appropriate.
        self._build_leak_ack = False
        if not self._gate_build_with_leaks(from_queue=False, stage="build"):
            return
        self._all_failed = False
        queue = ["apply", "build", "verify"]
        if getattr(proj, "auto_resolve_residuals", True):
            queue.append("auto_resolve")
        self._run_all_queue = queue
        next_stage = self._run_all_queue.pop(0)
        self._append_log(
            f"[build] queueing {queue} so the redacted output is "
            f"materialised on disk."
        )
        Toaster.notify(
            "Building output",
            "running apply / build / verify",
            kind="info",
            pipeline_event=True,
        )
        try:
            self.sidebar.select("pipeline")
            self.stack.setCurrentWidget(self.pipeline_view)
        except Exception:
            pass
        self._run_stage(next_stage, from_queue=True)

    def _gate_build_with_leaks(
        self, *, from_queue: bool, stage: str = "build"
    ) -> bool:
        """Confirm with the operator before kicking off promote/build
        when un-reviewed or explicitly-skipped review candidates would
        slip past the substitution map.

        Two oracles are consulted in order:

        * ``iter_unreviewed_pending`` (pending with ``decision`` left
          on ``None`` / ``"pending"``) — covers the Run-all "click
          Approve & continue without opening Review" case. Today
          ``stage_promote`` would silently auto-approve them, so the
          dialog wording asks whether to treat them as 'to substitute'
          or go back to Review.
        * ``iter_unhandled_leaks`` (everything not ``approve`` and not
          already in the substitution map) — covers the legacy
          pre-build path: candidates the operator explicitly skipped
          that will end up as residuals in the redacted output.

        Returns ``True`` when execution may proceed (no leaks, or
        operator clicked "Build anyway"), ``False`` when the operator
        bailed out (the run-all queue is cleared and Review is focused
        on the first leak).
        """
        try:
            unreviewed = self.state.iter_unreviewed_pending()
        except Exception:
            unreviewed = []
        try:
            unhandled = self.state.iter_unhandled_leaks()
        except Exception:
            unhandled = []
        if not unreviewed and not unhandled:
            return True
        # ``unhandled`` is the superset (anything not explicitly
        # approved AND not already in the substitution map); use its
        # length as the headline count so the dialog phrasing is
        # consistent regardless of whether items are merely
        # unreviewed or explicitly skipped. ``mode`` picks the framing:
        # if any unreviewed-pending is present we present the softer
        # "auto-approve on continue" copy; otherwise we fall back to
        # the stricter "these will leak into the output" copy.
        count = len(unhandled) if unhandled else len(unreviewed)
        mode = "unreviewed" if unreviewed else "skipped"
        if self._build_dialog_active:
            # A confirmation dialog is already open (debounce repeated
            # clicks or signal storms); treat the second attempt as a
            # cancel so we never spawn a worker behind a modal dialog.
            return False
        self._build_dialog_active = True
        try:
            if not self._confirm_build_with_leaks(count, mode=mode):
                self._append_log(
                    f"[{stage}] cancelled by operator: {count} "
                    f"{mode} candidate(s) in the review queue"
                    + (" (run-all aborted)." if from_queue else ".")
                )
                self._run_all_queue.clear()
                try:
                    self.sidebar.select("review")
                    self.stack.setCurrentWidget(self.review_view)
                    self.review_view.focus_first_leak()
                except Exception:
                    pass
                return False
            self._append_log(
                f"[{stage}] operator chose 'Build anyway' with "
                f"{count} {mode} candidate(s) still pending."
            )
            self._build_leak_ack = True
            return True
        finally:
            self._build_dialog_active = False

    def _confirm_build_with_leaks(
        self, leak_count: int, *, mode: str = "skipped"
    ) -> bool:
        """Show the "potential leaks" warning dialog. Returns ``True``
        when the operator clicks "Build anyway", ``False`` otherwise
        (including dialog dismissal via Esc / window close / outside
        click / focus loss).

        ``mode`` picks the wording:

        * ``"unreviewed"`` — candidates the operator never opened. The
          pipeline will auto-merge them into the substitution map (so
          they WILL be anonymised), but the dialog still gives the
          operator a chance to go back and triage them manually.
        * ``"skipped"`` — candidates explicitly rejected (or otherwise
          not approved). These will NOT be anonymised and will surface
          as residual leaks in the verifier output.

        The default focused button is the safer "Back to review" so a
        careless Enter does not bypass the warning. Outside-click and
        focus-loss dismissal are wired via :func:`make_dismissible`
        and map to ``reject`` (i.e. "Back to review"), matching the
        safe default.
        """
        if mode == "unreviewed":
            noun = "candidate" if leak_count == 1 else "candidates"
            count_phrase = f"<b>{leak_count}</b> {noun}"
            title = "Un-reviewed candidates detected"
            text = "There are candidates you have not reviewed yet."
            informative = (
                f"{count_phrase} in the Review queue have no explicit "
                f"decision. The pipeline will treat them as "
                f"<b>'to substitute'</b> and merge them into the "
                f"substitution map automatically.<br><br>"
                "Proceed with this auto-approval, or go back to the "
                "Review pane to triage them manually."
            )
            yes_text = "Build anyway"
            no_text = "Back to review"
        else:
            noun = (
                "item"
                if leak_count == 1
                else "items"
            )
            count_phrase = f"<b>{leak_count}</b> potentially-sensitive {noun}"
            title = "Potential leaks detected"
            text = "Potential leaks detected."
            informative = (
                f"{count_phrase} in the review queue have not been "
                f"approved, so they will <b>not</b> be anonymised by "
                f"the build.<br><br>"
                "You can build anyway with the current selection, or "
                "go back to the Review pane to approve or delete the "
                "remaining items."
            )
            yes_text = "Build anyway"
            no_text = "Back to review"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(informative)
        yes = box.addButton(yes_text, QMessageBox.ButtonRole.YesRole)
        no = box.addButton(no_text, QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        # Capture the clicked button via signal BEFORE the QMessageBox is
        # destroyed by WA_DeleteOnClose (installed by make_dismissible
        # below). Reading ``box.clickedButton()`` after exec() returned
        # was racy on PySide6: the C++ widget could be marked for
        # deletion already and the call returned ``None``, which made
        # the comparison ``clickedButton() is yes`` evaluate to False
        # even when the user had clicked "Build anyway" — the pipeline
        # silently treated it as "Back to review" and nothing started.
        captured: list = []
        try:
            box.buttonClicked.connect(lambda b: captured.append(b))
        except Exception:
            pass
        # Click-outside / focus-loss / Esc all map to reject() == "Back
        # to review", which is the safe default for an "are you sure?"
        # confirmation. WA_DeleteOnClose is set by make_dismissible so
        # the QMessageBox is reaped after exec() returns.
        make_dismissible(box, dismiss_action="reject")
        box.exec()
        return bool(captured) and captured[0] is yes

    def _on_verifier_report_changed(self, report) -> None:
        """Refresh the inline residuals row on the Pipeline summary."""
        count = 0
        if report is not None:
            try:
                count = len(getattr(report, "hits", []) or [])
            except Exception:
                count = 0
        self.pipeline_view.set_residuals(count)

    def _send_all_residuals_to_review(self) -> None:
        """Triggered by the Pipeline summary's "Send all to Review"
        button. Replays the same code path the (now hidden) Verifier
        view used: every hit becomes a pending candidate.

        After dispatching the hits we hide the inline residuals
        banner: leaving it visible after the user has acted on it
        confused users into clicking the same button repeatedly. The
        underlying ``verifier_report`` is preserved so the user can
        still open the Markdown report on disk if they need to.
        """
        rep = self.state.verifier_report
        hits = list(getattr(rep, "hits", []) or []) if rep else []
        if not hits:
            return
        self._on_hits_to_pending(hits)
        try:
            self.pipeline_view.set_residuals(0)
        except Exception:
            pass
        self.sidebar.select("review")
        self.stack.setCurrentWidget(self.review_view)

    def _on_open_build_folder(self, path: str) -> None:
        """Reveal the build's output folder in the OS file manager.

        Hooked to the green "Build complete" banner's *Open output
        folder* button. Uses ``QDesktopServices.openUrl`` so the
        right tool fires on every platform (Explorer on Windows,
        Finder on macOS, the user's default file manager on Linux).
        """
        if not path:
            return
        try:
            from pathlib import Path as _P
            from PySide6.QtGui import QDesktopServices

            target = _P(path)
            if not target.exists():
                Toaster.notify(
                    "Path missing",
                    f"Build output folder no longer exists: {target}",
                    kind="warn",
                )
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except Exception as exc:
            Toaster.notify(
                "Cannot open folder",
                f"{exc}",
                kind="err",
            )

    def _on_open_verifier_report(self) -> None:
        """Triggered by the Pipeline summary's "View report" button.

        Routes to the (still-supported but no-longer-pinned-in-the-
        sidebar) Verifier view and hides the inline banner: the
        operator has explicitly acknowledged the residual leaks by
        opening the report, so leaving the banner up is just visual
        clutter.
        """
        try:
            self.pipeline_view.set_residuals(0)
        except Exception:
            pass
        self._switch_view("verifier")

    def _on_hits_to_pending(self, hits: list) -> None:
        """Convert verifier residual hits into Review candidates.

        Each :class:`anonymize.verifier.LeakHit` becomes a :class:`Candidate`
        with ``tier='T3_verifier'`` so the user can edit a placeholder and
        promote it to the map; a follow-up Run all will then apply it.
        """
        if not hits:
            return
        proj = self.state.project
        if proj is None:
            Toaster.notify(
                "No project",
                "Open a project before sending verifier hits to Review.",
                kind="err",
            )
            return
        existing = {c.value for c in self.state.pending}
        added: list[Candidate] = []
        for h in hits:
            value = getattr(h, "match", "") or ""
            if not value or value in existing:
                continue
            cat = self._guess_category_from_pattern(getattr(h, "pattern", ""))
            cand = Candidate(
                value=value,
                category=cat,
                suggested_placeholder="",
                confidence=1.0,
                rationale=(
                    "residual leak from verifier "
                    f"({getattr(h, 'pattern', 'unknown')})"
                ),
                count=1,
                examples=[
                    f"{getattr(h, 'file', '')}:{(getattr(h, 'snippet', '') or '')[:40]}"
                ],
                tier="T3_verifier",
            )
            existing.add(value)
            added.append(cand)
        if not added:
            Toaster.notify(
                "Nothing to send",
                "all selected hits are already in Review",
                kind="info",
            )
            return
        new_pending = list(self.state.pending) + added
        self.state.set_candidates(pending=new_pending)
        try:
            from anonymize.triage import write_candidates_yaml
            write_candidates_yaml(proj.pending_path, new_pending)
        except Exception:
            pass
        self._append_log(
            f"[verifier→review] {len(added)} residual hit(s) added to pending"
        )
        Toaster.notify(
            "Sent to Review",
            f"{len(added)} hit(s) ready for triage",
            kind="info",
        )
        self.sidebar.select("review")
        self.stack.setCurrentWidget(self.review_view)
        # Land on the Text candidates tab so the operator sees the rows
        # that were just enqueued. Without this the inner tab strip
        # stays on whichever pane was active before (usually "Preview
        # of build" when residuals appear post-pipeline), which made
        # "Send all to Review" look like a no-op.
        try:
            self.review_view.focus_text_tab()
        except Exception:
            pass

    @staticmethod
    def _guess_category_from_pattern(pattern: str) -> str:
        p = (pattern or "").lower()
        if "phone" in p or "mobile" in p or "e164" in p:
            return "phones"
        if "email" in p or "mail" in p:
            return "emails"
        if "hex" in p or "credential" in p or "key" in p:
            return "keys"
        if "ip" in p:
            return "network"
        if (
            "package" in p
            or "bundle" in p
            or "android" in p
            or "ios" in p
            or "app_pkg" in p
        ):
            return "app_packages"
        if "header" in p:
            return "headers"
        if "user" in p and "agent" in p:
            return "user_agents"
        if "brand" in p or "vendor" in p:
            return "brand"
        if (
            "arn" in p
            or "ec2" in p
            or "uuid" in p
            or "sid" in p
            or "infra" in p
            or "cloud" in p
        ):
            return "infra_ids"
        return "other"

    def _open_in_diff(self, file_rel: str) -> None:
        self.sidebar.select("diff")
        self.stack.setCurrentWidget(self.diff_view)
        tree = self.diff_view.tree
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            if it.text(0) == file_rel:
                tree.setCurrentItem(it)
                break

    def _on_busy(self, busy: bool, label: str) -> None:
        if busy:
            self.busy_label.setText(label or "working…")
        else:
            self.busy_label.setText("")

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    # ---- self-test ---------------------------------------------------------

    def _run_self_test(self) -> None:
        from anonymize import env_check  # local import to avoid GUI import time
        report = env_check.run()
        text = report.summary()
        dismissible_message(self, "information", "Self-test", text)

    # ---- PDF export --------------------------------------------------------

    def _collect_export_files(self) -> list[Path]:
        """Pick the most useful set of anonymized files for the export dialog.

        Priority:
          1. If a project is open and ``output_dir`` exists, gather all
             ``*.md`` / ``*.txt`` produced by apply.
          2. Otherwise fall back to an empty list (user can still browse).
        """
        proj = self.state.project
        files: list[Path] = []
        if proj is not None and proj.output_dir.exists():
            for pat in ("**/*.md", "**/*.txt"):
                files.extend(sorted(proj.output_dir.rglob(pat)))
        return files

    def _export_to_pdf(self) -> None:
        files = self._collect_export_files()
        if not files:
            extra, _ = QFileDialog.getOpenFileNames(
                self,
                "Select anonymized files to export",
                "",
                "Documents (*.md *.markdown *.txt *.html)",
            )
            files = [Path(p) for p in extra]
        if not files:
            return
        proj = self.state.project
        # Pre-select the template the user picked at import time so the
        # Export dialog opens on the chosen card instead of the
        # ``pentest_modern`` default.
        default_tmpl = (
            getattr(proj, "export_template_id", None) or "pentest_modern"
        )
        dlg = ExportDialog(
            candidate_files=files,
            default_template_id=default_tmpl,
            parent=self,
        )
        if proj is not None:
            dlg.title_edit.setText(proj.output_dir.name)
            dlg.out_dir_edit.setText(str(proj.output_dir / "_export_pdf"))
        dlg.exec()

    # ---- shutdown / lifecycle ----------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Make sure the llama-server we spawned dies with us.

        Tuned for snappy shutdown: previous timeouts (2 s × N workers
        + 5 s server stop + a second 5 s pass from
        ``state.shutdown``) could keep the X-button frozen for 10-17 s
        when a pipeline was mid-LLM-call. The new flow hides the
        window first so the X click feels instant, then performs the
        cleanup with much tighter deadlines (≤2 s in the worst case):

        * Workers are asked to stop cooperatively and joined with a
          short 500 ms ceiling — they are QThreads on daemon-ish
          backing work, so a missed deadline just means Python's
          interpreter shutdown will reap them via the ``atexit`` hook
          registered in ``anonymize.server_manager``.
        * llama-server is stopped with a 1.5 s ``terminate`` window;
          the kernel will SIGKILL it via the process-group atexit
          hook anyway when the interpreter dies.
        * ``state.shutdown`` uses its own short timeout (1 s) so the
          double-stop pass never blocks the UI a second time.
        """
        # Hide the window straight away so the X click feels instant
        # even when the cleanup below has work to do. ``event.accept``
        # is enough for Qt to mark the window as closing; the actual
        # disappearance from the taskbar happens once this method
        # returns. Calling ``hide`` here makes the perceived latency
        # near-zero from the user's perspective.
        try:
            self.hide()
            QApplication.processEvents()
        except Exception:
            pass
        try:
            self._clear_pipeline_state(reason="silent")
        except Exception:
            pass
        try:
            self._stop_all()
        except Exception:
            pass
        try:
            for w in list(self._workers):
                try:
                    if hasattr(w, "request_stop"):
                        w.request_stop()
                    if hasattr(w, "wait"):
                        # 500 ms is enough for a cooperative stop_event
                        # check to fire between two HTTP calls; anything
                        # still busy after that is in a blocking
                        # ``recv()`` we cannot interrupt anyway.
                        w.wait(500)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            mgr = self.state.server
            if mgr.is_running():
                self._append_log("[shutdown] stopping spawned llama-server…")
                mgr.stop(timeout=1.5)
        except Exception:
            pass
        try:
            self.state.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


__all__ = ["MainWindow"]
