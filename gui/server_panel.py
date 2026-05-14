"""Server panel: preset gallery + status + log + actions.

Lives as a regular ``QWidget`` (used to be a ``QDockWidget``). It is
embedded in the main sidebar Server tab so all server-related
configuration is in one place, a pro/user-friendly home for
preset selection, model manager, start/stop, and diagnostics.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anonymize.hardware import HardwareReport
from anonymize.server_doctor import diagnose
from anonymize.server_manager import ServerManager
from anonymize.server_profile import ServerProfile, get_profile, load_profiles

from .deployment_chooser_dialog import DeploymentChooserDialog
from .icons import icon
from .preset_editor import PresetEditor
from .preset_gallery import PresetGallery
from .theme import PALETTE


class _StartWorker(QThread):
    """Run ``ServerManager.start`` off the UI thread so loading a 27B
    GGUF with a 50K context (which can take several minutes) doesn't
    freeze the window. Emits ``finished_with_result`` with
    ``(ok, error_message)``.
    """

    finished_with_result = Signal(bool, str)

    def __init__(self, manager: ServerManager, *, wait_seconds: float) -> None:
        super().__init__()
        self.manager = manager
        self.wait_seconds = wait_seconds

    def run(self) -> None:
        try:
            ok = self.manager.start(wait_seconds=self.wait_seconds)
            self.finished_with_result.emit(bool(ok), "")
        except Exception as e:
            self.finished_with_result.emit(False, str(e))


class ServerPanel(QWidget):
    # Carries an optional ``repo_id`` so the Model Manager can land
    # on the Curated downloads tab + scroll to the missing repo when
    # the request originates from the "Model not on disk" prompt or
    # a per-preset Download click. Pass ``""`` to open the Library
    # tab (the historical default for the top-level Model Manager
    # button).
    request_open_model_manager = Signal(str)
    request_open_settings = Signal()
    request_diagnostics = Signal(object)  # Diagnosis
    profile_used = Signal(str)

    def __init__(
        self,
        *,
        manager: ServerManager,
        hardware: Optional[HardwareReport] = None,
        project_dir: Optional[Path] = None,
        state=None,
    ) -> None:
        super().__init__()
        self.setObjectName("ServerPanel")
        self.manager = manager
        self.hardware = hardware
        self._project_dir = project_dir
        # Optional ``AppState`` reference so the poll loop can push the
        # llama-server health into the global state, PipelineView's
        # Run gate reads it from there to refuse Run when the server
        # is offline.
        self._state = state

        body = self

        # ---- Header / status -----------------------------------------------
        self.led = QLabel("●")
        self.led.setStyleSheet(f"color: {PALETTE['err']}; font-size: 18px;")
        self.title = QLabel("llama-server")
        self.title.setObjectName("H2")
        self.preset_lbl = QLabel(f"preset: {manager.profile.name}")
        self.preset_lbl.setObjectName("Muted")
        # Scope provenance: which layer of the load order
        # (builtin → user → project) the active preset came from.
        # Makes it obvious whether a project-local override is in
        # effect or the user is looking at a global preset.
        self.scope_lbl = QLabel("")
        self.scope_lbl.setObjectName("Caption")

        head = QHBoxLayout()
        head.addWidget(self.led)
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.preset_lbl)

        sub_head = QHBoxLayout()
        sub_head.addStretch()
        sub_head.addWidget(self.scope_lbl)

        # ---- Action buttons -----------------------------------------------
        self.start_btn = QPushButton(icon("play"), " Start")
        self.start_btn.setObjectName("PrimaryButton")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton(icon("stop"), " Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.restart_btn = QPushButton(icon("refresh"), " Restart")
        self.restart_btn.clicked.connect(self._restart)
        self.test_btn = QPushButton(icon("info"), " Test")
        self.test_btn.setToolTip("Test connection to llama-server")
        self.test_btn.clicked.connect(self._test)

        # 2x2 grid keeps all four primary actions visible even at the
        # minimum dock width.
        actions = QGridLayout()
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        for i, b in enumerate(
            (self.start_btn, self.stop_btn, self.restart_btn, self.test_btn)
        ):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            actions.addWidget(b, i // 2, i % 2)

        # ---- Manager buttons ----------------------------------------------
        mm_btn = QPushButton(icon("download"), " Model Manager")
        mm_btn.clicked.connect(lambda: self.request_open_model_manager.emit(""))
        deploy_btn = QPushButton(icon("settings"), " Configure deployment")
        deploy_btn.setToolTip(
            "Switch how the GUI brings up llama-server: local binary, "
            "Docker (managed) or external (already running)."
        )
        deploy_btn.clicked.connect(lambda: self._show_deployment_chooser())
        settings_btn = QPushButton(icon("settings"), " Settings")
        settings_btn.clicked.connect(self.request_open_settings.emit)
        manage = QHBoxLayout()
        for b in (mm_btn, deploy_btn, settings_btn):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            manage.addWidget(b)

        # ---- Preset gallery toolbar (new profile) ---------------------------
        self.new_profile_btn = QPushButton(icon("more"), " New profile")
        self.new_profile_btn.setToolTip(
            "Create a new server preset by cloning the currently active "
            "one. Edit it in the dialog that opens, then choose where to "
            "save it (user-global or per-project)."
        )
        self.new_profile_btn.clicked.connect(self._on_new_profile)
        gallery_bar = QHBoxLayout()
        gallery_bar.addWidget(QLabel("Profiles"))
        gallery_bar.addStretch()
        gallery_bar.addWidget(self.new_profile_btn)

        # ---- Preset gallery + log -----------------------------------------
        self.gallery = PresetGallery(hw=hardware)
        self.gallery.use_clicked.connect(self._on_use)
        self.gallery.customize_clicked.connect(self._on_customize)
        self.gallery.download_clicked.connect(self._on_gallery_download_clicked)
        self.gallery.delete_clicked.connect(self._on_delete_profile)
        self.gallery.default_changed.connect(self._on_default_changed)
        gallery_box = QWidget()
        gallery_lay = QVBoxLayout(gallery_box)
        gallery_lay.setContentsMargins(0, 0, 0, 0)
        gallery_lay.setSpacing(4)
        gallery_lay.addLayout(gallery_bar)
        gallery_lay.addWidget(self.gallery, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("server log will appear here once the process starts…")

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(gallery_box)
        split.addWidget(self.log)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 3)

        # ---- Persistent toggles -------------------------------------------
        # The "auto-start at launch" preference is stored in the
        # user-scope config dir (Linux: ~/.config/report-anonymizer/,
        # Windows: %APPDATA%\report-anonymizer\, macOS: ~/Library/
        # Application Support/report-anonymizer/) so it survives
        # across runs. Loading is best-effort: a corrupt YAML or
        # missing file just means "off".
        # (Detection-mode picker lives on the Pipeline tab next to
        # Run, where the trade-off is most relevant; we don't
        # duplicate it here.)
        from PySide6.QtWidgets import QCheckBox
        from anonymize.app_settings import get_bool, set_bool

        self.autostart_chk = QCheckBox("Auto-start server on launch")
        self.autostart_chk.setToolTip(
            "When enabled, llama-server is started automatically with "
            "the active preset every time the GUI launches (same path "
            "as the Start button, with the pre-flight check)."
        )
        try:
            self.autostart_chk.setChecked(
                get_bool("autostart_server", default=False)
            )
        except Exception:
            self.autostart_chk.setChecked(False)
        self.autostart_chk.toggled.connect(
            lambda v: set_bool("autostart_server", bool(v))
        )

        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addLayout(head)
        lay.addLayout(sub_head)
        lay.addLayout(actions)
        lay.addLayout(manage)
        lay.addWidget(self.autostart_chk)
        lay.addWidget(split, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._poll()

    def set_hardware(self, hw: Optional[HardwareReport]) -> None:
        self.hardware = hw
        self.gallery.set_hardware(hw)

    def set_project_dir(self, p: Optional[Path]) -> None:
        self._project_dir = p

    def _poll(self) -> None:
        # Use the non-blocking probe: returns the last cached result
        # instantly and schedules a refresh on a background thread.
        # The synchronous variant pegged the UI for up to 1 s on each
        # tick while the server was offline (Windows loopback connect).
        ok = self.manager.health_nowait(timeout=1.0)
        # While a Start request is in flight, freeze the visible
        # state on "starting…" so a transient "health endpoint not
        # yet up" doesn't paint the LED red between ``starting`` and
        # ``online``. Once Start finishes (success or failure) the
        # flag is cleared and this branch lets the real health show
        # through again.
        starting = (
            bool(self._state and self._state.server_starting)
            if self._state is not None else False
        )
        if starting and not ok:
            self.led.setStyleSheet(
                f"color: {PALETTE['warn']}; font-size: 18px;"
            )
            self.title.setText("llama-server · starting…")
        else:
            self.led.setStyleSheet(
                f"color: {PALETTE['ok'] if ok else PALETTE['err']}; "
                "font-size: 18px;"
            )
            text = "online" if ok else "offline"
            self.title.setText(f"llama-server · {text}")
        # Broadcast health to the global state so other views can
        # gate their actions on it (Run button refuses to launch if
        # the server is offline).
        if self._state is not None:
            try:
                self._state.set_server_online(bool(ok))
            except Exception:
                pass
        for ln in self.manager.tail(80)[-80:]:
            if ln and (not self.log.toPlainText().endswith(ln + "\n")):
                self.log.appendPlainText(ln)
        self.gallery.select(self.manager.profile.name)
        self.preset_lbl.setText(f"preset: {self.manager.profile.name}")
        scope = getattr(self.manager.profile, "source", "builtin") or "builtin"
        if scope == "project" and self._project_dir is not None:
            self.scope_lbl.setText(
                f"scope: project · {self._project_dir / '.anon' / 'server.yml'}"
            )
        elif scope == "user":
            from anonymize.server_profile import USER_PROFILES_PATH
            self.scope_lbl.setText(f"scope: user · {USER_PROFILES_PATH}")
        else:
            self.scope_lbl.setText("scope: builtin (config/server_profiles.yml)")

    def _start(self) -> None:
        # Loading a big model (e.g. 27B at 50K ctx) can easily take
        # 2–5 minutes; running ``manager.start(wait_seconds=…)`` on
        # the UI thread blocks Qt's event loop and the user sees a
        # frozen window. Push it to a worker thread and surface a
        # "Starting…" state instead.
        if getattr(self, "_start_worker", None) and self._start_worker.isRunning():
            return
        # Pre-flight: if the active preset's GGUF isn't on disk
        # (the wizard was skipped, the user pinned a custom path
        # that doesn't exist, …) llama-server fails with a cryptic
        # "model file not found". Catch it here and offer to open
        # the Model Manager directly so the user has a one-click
        # path forward.
        prof = self.manager.profile
        if (
            getattr(prof, "deployment_mode", "local_binary") != "external"
            and not prof.is_model_present()
            and prof.model_repo
            and prof.model_filename
        ):
            from PySide6.QtWidgets import QMessageBox
            from ._dismissible_dialog import dismissible_message
            ans = dismissible_message(
                self,
                "question",
                "Model not on disk",
                (
                    f"<b>{prof.name}</b> needs the GGUF<br>"
                    f"<code>{prof.model_filename}</code><br>"
                    f"from <code>{prof.model_repo}</code>, but it isn't on "
                    f"disk yet.<br><br>Open the Model Manager to download it?"
                ),
                buttons=QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                default_button=QMessageBox.StandardButton.Open,
            )
            if ans == QMessageBox.StandardButton.Open:
                self.request_open_model_manager.emit(prof.model_repo or "")
            return
        # Bigger models need more headroom; 600 s (= 10 min) accommodates
        # 30B+ with large contexts on most hardware. The user can always
        # click Stop while we wait.
        self._start_worker = _StartWorker(self.manager, wait_seconds=600.0)
        self._start_worker.finished_with_result.connect(self._on_start_finished)
        self.start_btn.setEnabled(False)
        self.start_btn.setText(" Starting…")
        self.title.setText("llama-server · starting…")
        self.led.setStyleSheet(
            f"color: {PALETTE['warn']}; font-size: 18px;"
        )
        # Tell the global state we're booting so the polling loops in
        # ServerPanel + ServerStatusWidget keep the "starting…" label
        # visible instead of flickering through "offline" while the
        # binary loads weights and the health endpoint is still down.
        if self._state is not None:
            try:
                self._state.set_server_starting(True)
            except Exception:
                pass
        self.log.appendPlainText(
            "[start] launching server in background, this may take "
            "several minutes for large models / long contexts."
        )
        self._start_worker.start()

    def _on_start_finished(self, ok: bool, err: str) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText(" Start")
        # Lift the "starting" suppression no matter what the worker
        # returned: success → poll loop will flip to online; failure
        # → poll loop will surface "offline" honestly so the user
        # sees that something went wrong.
        if self._state is not None:
            try:
                self._state.set_server_starting(False)
            except Exception:
                pass
        if err:
            self.log.appendPlainText(f"[start error] {err}")
            # Common newbie failure: the active preset is in
            # ``local_binary`` mode but llama-server is not
            # installed; or in ``docker`` mode but the docker CLI
            # is missing.  Surface the deployment chooser instead
            # of the cryptic diagnostic dialog so the user can
            # switch mode in one click.
            if self._maybe_show_deployment_chooser(err):
                return
            self._emit_diagnosis()
            return
        if not ok:
            self.log.appendPlainText("[start] server did not become ready in time")
            # External mode: we never spawned a process, so
            # "didn't become ready" actually means "no health
            # response on host:port". Same deployment-chooser
            # remediation: maybe the user wanted ``docker`` /
            # ``local_binary`` instead.
            if (
                getattr(self.manager.profile, "deployment_mode", "local_binary")
                == "external"
            ):
                if self._maybe_show_deployment_chooser(
                    "External mode timed out: no health response from "
                    f"{self.manager.profile.host}:{self.manager.profile.port}."
                ):
                    return
            self._emit_diagnosis()
            return
        self.log.appendPlainText("[start] server is ready")
        self._poll()

    # --- deployment chooser hook -------------------------------------------
    _DEPLOYMENT_FAILURE_HINTS = (
        "binary not found",
        "cannot execute binary",
        "exec format error",
        "docker' cli is not on path",
        "docker cli is not on path",
        "docker pull",
        "docker run",
    )

    def _maybe_show_deployment_chooser(self, err: str) -> bool:
        """If ``err`` looks like 'the picked deployment mode can't run
        on this machine', open the deployment chooser dialog.
        Returns True when the dialog handled the situation (chooser
        shown + Start re-attempted), False otherwise so the caller
        falls back to the regular diagnostic flow."""
        haystack = (err or "").lower()
        if not any(h in haystack for h in self._DEPLOYMENT_FAILURE_HINTS):
            return False
        return self._show_deployment_chooser(reason=err.strip())

    def _show_deployment_chooser(self, *, reason: str = "") -> bool:
        """Open the chooser, persist the user's pick on the active
        preset, refresh the gallery, and re-attempt Start. Returns
        True if a mode was picked (so the caller skips the
        diagnostic dialog), False if the user dismissed."""
        dlg = DeploymentChooserDialog(self.manager.profile, reason=reason, parent=self)
        if not dlg.exec():
            return False
        new_mode = dlg.resulting_mode or self.manager.profile.deployment_mode
        self.log.appendPlainText(
            f"[deployment] switched to {new_mode}; retrying Start…"
        )
        # ``manager.profile`` was mutated in-place by the dialog;
        # refresh the gallery so the user sees the source flip
        # from "builtin" to "user" on the active card.
        try:
            self.gallery.refresh()
        except Exception:
            pass
        self._start()
        return True

    def _stop(self) -> None:
        self.manager.stop()
        self.log.appendPlainText("[stopped]")
        self._reset_start_button(force=True)

    def _reset_start_button(self, *, force: bool = False) -> None:
        """Bring the Start button back to its idle state.

        If a ``_StartWorker`` is still running (the user clicked
        Stop while ``manager.start()``'s health-wait loop was still
        polling), detach its ``finished_with_result`` signal so a
        late callback doesn't put the spinner back. The dangling
        worker thread eventually exits on its own once
        ``manager.stop()`` has killed the underlying process.
        """
        worker = getattr(self, "_start_worker", None)
        if worker is not None and worker.isRunning():
            try:
                worker.finished_with_result.disconnect(self._on_start_finished)
            except Exception:
                pass
            self._start_worker = None
        if force or worker is None or not worker.isRunning():
            self.start_btn.setEnabled(True)
            self.start_btn.setText(" Start")

    def _restart(self) -> None:
        # Restart can also block (it calls stop+start). Same trick:
        # stop synchronously (fast), then kick the worker for start.
        try:
            self.manager.stop()
        except Exception:
            pass
        self._start()

    def _test(self) -> None:
        ok = self.manager.health(timeout=2.0)
        self.log.appendPlainText("[test] " + ("OK" if ok else "no response"))

    def _emit_diagnosis(self) -> None:
        diag = self.manager.diagnose_failure()
        self.request_diagnostics.emit(diag)

    def _on_gallery_download_clicked(self, name: str) -> None:
        """User clicked Download on a preset card. Open the Model
        Manager on the Curated tab anchored on the preset's repo so
        the right entry is already selected (avoids the operator
        scrolling through the catalog to find the row that matched
        the preset they just clicked)."""
        try:
            prof = get_profile(name, project_dir=self._project_dir)
        except Exception:
            prof = None
        repo_id = ""
        if prof is not None:
            repo_id = getattr(prof, "model_repo", "") or ""
        self.request_open_model_manager.emit(repo_id)

    def _on_use(self, name: str) -> None:
        prof = get_profile(name, project_dir=self._project_dir)
        if prof is None:
            return
        try:
            self.manager.stop()
        except Exception:
            pass
        self.manager.profile = prof
        self.profile_used.emit(name)
        self._poll()

    def _on_customize(self, name: str) -> None:
        prof = get_profile(name, project_dir=self._project_dir)
        if prof is None:
            return
        dlg = PresetEditor(prof, project_dir=self._project_dir, parent=self)
        # Capture the saved profile so that 'Save as user preset' /
        # 'Save in project' also activates the new preset (the user
        # expectation is "save and use", but the editor only persisted
        # before, switching the active preset was a separate click).
        saved_holder: dict[str, str] = {}
        dlg.saved.connect(lambda p: saved_holder.update(name=p.name))
        if dlg.exec():
            self.gallery.refresh()
            saved_name = saved_holder.get("name") or name
            # ``_on_use`` reloads the profile, swaps the manager's
            # profile, emits ``profile_used`` and refreshes the
            # gallery selection, exactly what the user expects
            # after pressing one of the Save buttons.
            self._on_use(saved_name)
            self._poll()

    def _on_new_profile(self) -> None:
        """Create a new preset by cloning the active one. The preset
        editor handles the actual save target (user vs project)."""
        from PySide6.QtWidgets import QInputDialog

        base = get_profile(
            self.manager.profile.name, project_dir=self._project_dir
        ) or self.manager.profile
        new_name, ok = QInputDialog.getText(
            self,
            "New profile",
            "Profile name (must be unique):",
            text=f"{base.name}-copy",
        )
        if not ok or not new_name.strip():
            return
        new_prof = base.clone(name=new_name.strip())
        new_prof.is_builtin = False
        new_prof.source = "user"
        dlg = PresetEditor(new_prof, project_dir=self._project_dir, parent=self)
        if dlg.exec():
            self.gallery.refresh()
            self._poll()

    def _on_default_changed(self, name: str) -> None:
        """User picked a new default preset from the gallery, log it.
        The gallery already moves the ★ badge and disables the button on
        the chosen card, so no extra dialog is needed.
        """
        try:
            self.log.appendPlainText(
                f"[default] {name!r} will be auto-loaded on next start"
            )
        except Exception:
            pass

    def _on_delete_profile(self, name: str) -> None:
        """Delete a user / project preset after explicit confirmation."""
        from PySide6.QtWidgets import QMessageBox

        from anonymize.server_profile import (
            delete_project_profile,
            delete_user_profile,
            get_default_profile_name,
            set_default_profile_name,
        )

        prof = get_profile(name, project_dir=self._project_dir)
        if prof is None or prof.is_builtin:
            return
        scope = getattr(prof, "source", "user")
        from ._dismissible_dialog import dismissible_message
        ans = dismissible_message(
            self,
            "question",
            "Delete preset",
            f"Delete preset <b>{name}</b> ({scope} scope)? "
            f"This removes it from the YAML file; the model file on "
            f"disk is not touched.",
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            default_button=QMessageBox.StandardButton.Cancel,
            dismissible=False,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        deleted = False
        if scope == "project" and self._project_dir is not None:
            deleted = delete_project_profile(name, self._project_dir)
        if not deleted:
            deleted = delete_user_profile(name)
        if not deleted:
            dismissible_message(
                self,
                "warning",
                "Could not delete",
                f"Preset <b>{name}</b> was not found in any user or "
                f"project YAML, nothing to do.",
            )
            return
        # If the deleted profile was the active one, fall back to the
        # first available preset so the panel doesn't end up pointing
        # to a ghost.
        if self.manager.profile.name == name:
            remaining = load_profiles(project_dir=self._project_dir)
            if remaining:
                self.manager.profile = remaining[0]
                self.profile_used.emit(remaining[0].name)
        # Drop the default-preset preference if it pointed at the
        # deleted preset, otherwise the next launch would silently
        # fall back without any visible indication.
        if get_default_profile_name() == name:
            set_default_profile_name(None)
        self.gallery.refresh()
        self._poll()


__all__ = ["ServerPanel"]
