"""5-step first-run wizard: welcome / hardware / preset / model / summary.

Shown on first launch (when ``CONFIG_DIR/.bootstrapped`` doesn't exist).
The wizard hardware-detects llama-server vs Docker, lets the user pick a
preset based on the detected GPU, then **downloads the model file** so
the software is fully ready to use when the wizard closes, no Model
Manager round-trip required for the canonical happy path.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from anonymize.bootstrap import mark_bootstrapped, materialize_default_model
from anonymize.hardware import (
    HardwareReport,
    _read_installer_sentinel,
    report,
    suggest_deployment_mode,
)
from anonymize.hf_models import (
    GatedRepoError,
    OfflineMode,
    download_model,
)
from anonymize.server_profile import (
    DEFAULT_DOCKER_IMAGE,
    get_profile,
    load_profiles,
)

from .icons import welcome_hero_pixmap


class _Welcome(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Welcome")
        hero = QLabel()
        hero.setPixmap(welcome_hero_pixmap(640, 360))
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg = QLabel(
            "document-anonymizer-production runs entirely on your machine.\n"
            "The only optional network call is downloading a model from Hugging Face."
        )
        msg.setObjectName("Muted")
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.addWidget(hero)
        lay.addWidget(msg)


class _DockerPullWorker(QThread):
    """Run ``docker pull <image>`` in a background thread, streaming
    each stdout line to ``progress`` so the wizard's log box stays
    responsive while a multi-GB layer download is in progress."""

    progress = Signal(str)
    finished_with_result = Signal(bool, str)

    def __init__(self, image: str) -> None:
        super().__init__()
        self._image = image

    def run(self) -> None:  # noqa: D401 - Qt override
        try:
            proc = subprocess.Popen(
                ["docker", "pull", self._image],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )
        except Exception as exc:
            self.finished_with_result.emit(False, f"docker pull could not start: {exc}")
            return
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.progress.emit(line)
        rc = proc.wait()
        self.finished_with_result.emit(rc == 0, "" if rc == 0 else f"exit code {rc}")


class _Hardware(QWizardPage):
    def __init__(self, hw: HardwareReport) -> None:
        super().__init__()
        self.setTitle("Hardware detected")
        self.hw = hw
        body = QLabel(hw.short() or "No hardware report available.")
        body.setWordWrap(True)
        more = QLabel(
            "We use this to recommend the right preset. You can override at any time."
        )
        more.setObjectName("Muted")
        more.setWordWrap(True)
        # Inline deployment hint: tells the newbie which Start mode
        # the GUI will use by default and why. The actual default is
        # applied later by ``FirstRunWizard.accept`` so the user can
        # still override per preset.
        #
        # Installer-aware short-circuit: when the Windows Setup wizard
        # has just bundled and verified ``llama-server.exe`` (sentinel
        # present in user_config_dir), force ``local_binary`` and
        # render a dedicated message that mentions the chosen variant.
        # This skips the Docker-pull button rendering further down.
        installer_sentinel = _read_installer_sentinel()
        installer_variant = (
            installer_sentinel.get("variant")
            if isinstance(installer_sentinel, dict)
            else None
        )
        if isinstance(installer_variant, str) and installer_variant in (
            "cpu",
            "cuda",
            "vulkan",
        ):
            self.deployment_mode = "local_binary"
            hint_text = (
                f"Llama-server installed by Setup ({installer_variant.upper()} variant). "
                "Use the Model Manager to download a GGUF, and the Server "
                "tab to switch between the bundled CPU / CUDA / Vulkan "
                "binaries if you want a different backend."
            )
        else:
            self.deployment_mode, hint_text = suggest_deployment_mode()
        deploy = QLabel(
            "<b>Server deployment.</b> "
            f"{hint_text} "
            "You can change this per preset later in <i>Server → "
            "Configure deployment</i>."
        )
        deploy.setObjectName("Muted")
        deploy.setWordWrap(True)
        deploy.setTextFormat(Qt.TextFormat.RichText)
        lay = QVBoxLayout(self)
        lay.addWidget(body)
        lay.addSpacing(8)
        lay.addWidget(more)
        lay.addSpacing(8)
        lay.addWidget(deploy)

        # Docker-bootstrap helper: when the recommended mode is
        # Docker we offer a one-click "pull the image now" so the
        # very first Start in the GUI doesn't have to wait for a
        # multi-GB download. If Docker isn't installed at all we
        # link to the official install guide instead, without that
        # the user has no path forward and would only discover the
        # gap on Start failure.
        self._pull_worker: Optional[_DockerPullWorker] = None
        if self.deployment_mode == "docker":
            actions = QHBoxLayout()
            if shutil.which("docker") is not None:
                self.pull_btn = QPushButton(
                    "Pull llama.cpp image now (recommended)"
                )
                self.pull_btn.setToolTip(
                    "Run 'docker pull' once so the very first Start "
                    "is instant. About 5 GB on a fresh machine."
                )
                self.pull_btn.clicked.connect(self._on_pull_clicked)
                actions.addWidget(self.pull_btn)
            else:
                # Docker missing entirely. We can't install it for
                # them but at least give the official link.
                doc_btn = QPushButton("Open Docker installation guide")
                doc_btn.setToolTip(
                    "Open https://docs.docker.com/get-docker/ in your "
                    "browser. After Docker is installed, restart this "
                    "wizard or click Start in the main window."
                )
                doc_btn.clicked.connect(
                    lambda: QDesktopServices.openUrl(
                        QUrl("https://docs.docker.com/get-docker/")
                    )
                )
                actions.addWidget(doc_btn)
            actions.addStretch()
            lay.addSpacing(6)
            lay.addLayout(actions)
            self.pull_log = QPlainTextEdit()
            self.pull_log.setReadOnly(True)
            self.pull_log.setMaximumBlockCount(400)
            self.pull_log.setPlaceholderText(
                "docker pull output will appear here…"
            )
            self.pull_log.setVisible(False)
            lay.addWidget(self.pull_log)
        lay.addStretch()

    # ------------------------------------------------------------------
    def _on_pull_clicked(self) -> None:
        if self._pull_worker is not None and self._pull_worker.isRunning():
            return
        self.pull_btn.setEnabled(False)
        self.pull_btn.setText("Pulling…  (you can keep clicking Next; it runs in background)")
        self.pull_log.setVisible(True)
        self.pull_log.appendPlainText(f"$ docker pull {DEFAULT_DOCKER_IMAGE}")
        self._pull_worker = _DockerPullWorker(DEFAULT_DOCKER_IMAGE)
        self._pull_worker.progress.connect(self.pull_log.appendPlainText)
        self._pull_worker.finished_with_result.connect(self._on_pull_finished)
        self._pull_worker.start()

    def _on_pull_finished(self, ok: bool, err: str) -> None:
        self.pull_btn.setEnabled(True)
        if ok:
            self.pull_btn.setText("Image cached ✓")
            self.pull_log.appendPlainText("[done] image cached locally")
        else:
            self.pull_btn.setText("Pull failed, click to retry")
            self.pull_log.appendPlainText(f"[error] {err}")


def _recommend_preset(hw: HardwareReport) -> str:
    """Pick the best built-in preset for the detected hardware.

    Pure heuristic over advertised VRAM tiers, no per-preset benchmark
    re-run required because the curated catalog already states each
    preset's peak VRAM in its description (see ``server_profiles.yml``).
    Falls back to ``default`` (CPU + Q5_K_M) whenever nothing better
    fits, which matches the new policy: the lightest setup runs
    everywhere.
    """
    vram = 0
    if hw.gpus:
        # Use the largest detected card; multi-GPU users get the
        # benefit of the strongest one.
        vram = max((g.vram_total_mb for g in hw.gpus), default=0)
    # Tiers anchored on each preset's description in
    # ``config/server_profiles.yml``:
    #   ministral-3-8b-reasoning-bf16  ~18.5 GB
    #   ministral-3-8b-reasoning-q5    ~9.2 GB
    #   default (Q5 + CPU)             RAM-only
    if vram >= 19000:
        return "ministral-3-8b-reasoning-bf16"
    if vram >= 10000:
        return "ministral-3-8b-reasoning-q5"
    return "default"


def _estimate_preset_disk_label(prof) -> str:
    """Return a short ``"~X GB"`` size label for the preset's GGUF.

    Thin wrapper around :func:`anonymize.model_size.estimate_gguf_disk_label`
    that feeds it the profile's filename / repo / name hints plus the
    resolved on-disk path so an already-downloaded file gets its exact
    ``stat()`` size rather than the heuristic estimate.
    """
    from anonymize.model_size import estimate_gguf_disk_label

    on_disk = None
    try:
        on_disk = prof.model_path
    except Exception:
        on_disk = None
    return estimate_gguf_disk_label(
        getattr(prof, "model_filename", "") or "",
        getattr(prof, "model_repo", "") or "",
        getattr(prof, "name", "") or "",
        on_disk=on_disk,
    )


class _Preset(QWizardPage):
    def __init__(self, hw: HardwareReport) -> None:
        super().__init__()
        self.setTitle("Choose a preset")
        self.list = QListWidget()
        self.hw = hw
        recommended = _recommend_preset(hw)
        # Keep the recommendation visible above the list so the user
        # understands *why* one row is pre-selected, and can override
        # by clicking another. Non-blocking, no popup.
        if hw.gpus:
            g = hw.gpus[0]
            self._reco_text = (
                f"Detected GPU: {g.name} ({g.vram_total_mb // 1024} GB), "
                f"recommended preset: {recommended}"
            )
        else:
            self._reco_text = (
                "No GPU detected, recommended preset: default "
                "(Qwen 3.5 4B Q5_K_M on CPU, lightest setup)."
            )
        recommended_item: Optional[QListWidgetItem] = None
        for prof in load_profiles():
            present = prof.is_model_present()
            size_chunk = _estimate_preset_disk_label(prof)
            state_chunk = "downloaded" if present else "not downloaded"
            tail = f"({state_chunk}{', ' + size_chunk if size_chunk else ''})"
            label = f"{prof.name}    {tail}"
            if prof.name == recommended:
                label = "★ " + label + " , recommended for your hardware"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, prof.name)
            self.list.addItem(item)
            if prof.name == recommended:
                recommended_item = item
        if recommended_item is not None:
            self.list.setCurrentItem(recommended_item)
        reco_label = QLabel(self._reco_text)
        reco_label.setObjectName("Muted")
        reco_label.setWordWrap(True)
        hint = QLabel(
            "Click <b>Next</b> to download the chosen preset's model. "
            "You can also queue other models from the curated catalog "
            "on that screen, handy if you want to grab a stronger "
            "model for later without going through the Model Manager."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(reco_label)
        lay.addSpacing(6)
        lay.addWidget(self.list, 1)
        lay.addWidget(hint)
        self.registerField("preset_name", self.list, "currentText")

    def selected(self) -> Optional[str]:
        it = self.list.currentItem()
        if not it:
            return None
        return it.data(Qt.ItemDataRole.UserRole)


class _ModelDownloadWorker(QThread):
    """Download a single GGUF from Hugging Face Hub.

    Wraps :func:`anonymize.hf_models.download_model` (the same backend
    the Model Manager uses), surfacing progress as Qt signals so the
    wizard's progress bar stays responsive on a multi-GB download.
    """

    # ``object`` (not ``int``) for byte counts: PySide6 maps Signal's
    # ``int`` to C int32, so any download bigger than ~2 GB would
    # wrap or get truncated by the time the slot got it. ``object``
    # passes the Python int through unchanged.
    progress = Signal(object, object, float, object)  # done, total, bytes_per_s, eta_s
    phase = Signal(str)
    finished_with_result = Signal(bool, str)

    def __init__(self, repo: str, filename: str, dst: Optional[Path] = None) -> None:
        super().__init__()
        self._repo = repo
        self._filename = filename
        self._dst = dst
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # noqa: D401 - Qt override
        try:
            res = download_model(
                self._repo,
                self._filename,
                dst=self._dst,
                progress_cb=lambda d, t, s, e: self.progress.emit(d, t, s, e),
                phase_cb=lambda p: self.phase.emit(p),
                stop_event=self._stop,
            )
            if res.cancelled:
                self.finished_with_result.emit(False, "cancelled")
            elif res.ok:
                self.finished_with_result.emit(True, "")
            else:
                self.finished_with_result.emit(False, res.error or "download failed")
        except GatedRepoError as e:
            self.finished_with_result.emit(False, f"gated repo: {e}")
        except OfflineMode:
            self.finished_with_result.emit(False, "offline mode is enabled")
        except Exception as e:
            self.finished_with_result.emit(False, str(e))


class _DownloadModel(QWizardPage):
    """Wizard step that downloads the chosen preset's GGUF up-front so
    the GUI is fully usable the moment the wizard closes.

    Behaviour:
    * If the GGUF is already on disk, the page reports "Already on
      disk" and auto-completes (Next is enabled, no network call).
    * Otherwise the download starts on ``initializePage`` and the
      Finish/Next button stays disabled until either:
        - the download completes (preset becomes runnable), or
        - the user clicks "Skip" (the run can still be completed by
          opening the Model Manager from the main window later).
    * On error (no internet, gated repo, …) the page shows the
      reason and offers the "Skip" exit so the user is never trapped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Download model")
        self._worker: Optional[_ModelDownloadWorker] = None
        self._completed = False
        self._skipped = False
        # When the Windows installer has already staged the llama-server
        # binary (sentinel present in user_config_dir), skipping the
        # model download leaves the app in a non-functional state, the
        # binary is on disk but inference still needs a GGUF. Hide the
        # Skip button in that case so the user can't accidentally finish
        # the wizard with no model and then wonder why Start fails.
        sentinel = _read_installer_sentinel()
        self._installer_present = (
            isinstance(sentinel, dict)
            and isinstance(sentinel.get("variant"), str)
            and sentinel.get("variant") in ("cpu", "cuda", "vulkan")
        )
        # HF cas-bridge can take 5-30 s to start streaming on a fresh
        # connection. While we wait the bar would otherwise sit at
        # "connecting…" forever, keep a per-second counter going so
        # the user sees the wizard isn't frozen.
        self._connect_secs = 0
        self._got_first_progress = False
        self._connect_timer = QTimer(self)
        self._connect_timer.setInterval(1000)
        self._connect_timer.timeout.connect(self._tick_connect_label)

        self.summary = QLabel(
            "Preparing download…"
        )
        self.summary.setWordWrap(True)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate until total is known
        self.bar.setMinimumHeight(16)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")

        self.eta = QLabel("")
        self.eta.setObjectName("Muted")
        self.eta.setWordWrap(True)

        self.skip_btn = QPushButton("Skip download")
        self.skip_btn.setToolTip(
            "Continue without downloading. You can grab the model from "
            "the Model Manager later, just remember the GUI will refuse "
            "to Start until the file is on disk."
        )
        self.skip_btn.clicked.connect(self._on_skip_clicked)
        if self._installer_present:
            self.skip_btn.setVisible(False)
            self.skip_btn.setEnabled(False)
        skip_row = QHBoxLayout()
        skip_row.addStretch()
        skip_row.addWidget(self.skip_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addSpacing(8)
        lay.addWidget(self.bar)
        lay.addWidget(self.eta)
        lay.addStretch()
        lay.addLayout(skip_row)

    # ----- Qt wizard hooks -----------------------------------------------

    def initializePage(self) -> None:  # noqa: D401 - Qt override
        wiz = self.wizard()
        if wiz is None:
            return
        try:
            preset_name = wiz.preset_page.selected() or "default"  # type: ignore[attr-defined]
        except Exception:
            preset_name = "default"

        prof = get_profile(preset_name)
        if prof is None:
            self.summary.setText(
                f"<b>Preset:</b> {preset_name}<br>"
                "<i>Could not resolve preset metadata. Skip and "
                "configure later from Server view.</i>"
            )
            self.bar.setVisible(False)
            self.eta.setVisible(False)
            # Allow Finish anyway, the user picked something we can't
            # auto-resolve, so block-with-skip would be hostile.
            self._completed = True
            self.completeChanged.emit()
            return

        repo = prof.model_repo or ""
        fname = prof.model_filename or ""
        dst = Path(prof.model_path).expanduser() if prof.model else None
        if dst is None or not repo or not fname:
            self.summary.setText(
                f"<b>Preset:</b> {prof.name}<br>"
                "<i>This preset doesn't declare a Hugging Face source, "
                "you'll need to point its <code>model</code> path at a "
                "local GGUF before clicking Start.</i>"
            )
            self.bar.setVisible(False)
            self.eta.setVisible(False)
            self._completed = True
            self.completeChanged.emit()
            return

        if dst.exists() and dst.stat().st_size > 0:
            human_size = f"{dst.stat().st_size / 2**30:.1f} GB"
            self.summary.setText(
                f"<b>Preset:</b> {prof.name}<br>"
                f"<b>Model:</b> {fname}<br>"
                f"<b>Status:</b> ✓ already on disk ({human_size})"
            )
            self.bar.setRange(0, 100)
            self.bar.setValue(100)
            self.eta.setText("Nothing to do, click Next.")
            self.skip_btn.setVisible(False)
            self._completed = True
            self.completeChanged.emit()
            return

        intro = (
            "<i>Downloading from Hugging Face. One-time step. The "
            "wizard stays open while the file lands; click <b>Skip</b> "
            "below to defer to the Model Manager later.</i>"
            if not self._installer_present
            else (
                "<i>Downloading from Hugging Face. One-time step. The "
                "llama-server binary was installed by Setup, only the "
                "model file is missing, so the wizard waits for the "
                "download to complete before letting you finish.</i>"
            )
        )
        self.summary.setText(
            f"<b>Preset:</b> {prof.name}<br>"
            f"<b>Model:</b> {fname}<br>"
            f"<b>Source:</b> {repo}<br>"
            f"{intro}"
        )
        # Pre-pin the bar to a 0-1000 scale (per-mille). QProgressBar's
        # ``setRange`` uses C int internally, so passing the raw byte
        # count (often > 2 GB) overflows on multi-GB GGUFs and the bar
        # silently stays indeterminate. Use a scaled fraction instead.
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("connecting…")
        self.eta.setText("Connecting to Hugging Face…")
        # Skip button stays hidden when the installer pre-staged the
        # llama-server binary (see __init__): skipping there leaves
        # the app non-functional and the user can't recover without
        # round-tripping back through the Model Manager.
        self.skip_btn.setVisible(not self._installer_present)
        self.skip_btn.setEnabled(not self._installer_present)
        # Reset the connect-counter and start ticking. ``_on_progress``
        # stops the timer the moment the first chunk lands.
        self._connect_secs = 0
        self._got_first_progress = False
        self._connect_timer.start()
        self._start_worker(repo, fname, dst)

    def isComplete(self) -> bool:  # noqa: D401 - Qt override
        return self._completed or self._skipped

    def cleanupPage(self) -> None:  # noqa: D401 - Qt override
        # Cancel a download-in-progress when the user clicks Back.
        self._connect_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()

    # ----- helpers -------------------------------------------------------

    def _start_worker(self, repo: str, fname: str, dst: Path) -> None:
        self._worker = _ModelDownloadWorker(repo, fname, dst)
        self._worker.progress.connect(self._on_progress)
        self._worker.phase.connect(self._on_phase)
        self._worker.finished_with_result.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, done: int, total: int, bytes_per_s: float, eta_s: int) -> None:
        # First chunk just landed: stop the connect-counter so the eta
        # label switches to real bytes/MB-s instead of a stale "5s".
        if not self._got_first_progress:
            self._got_first_progress = True
            self._connect_timer.stop()
        # download_model emits speed in bytes/sec, convert here.
        mb_per_s = bytes_per_s / 2**20
        if total and total > 0:
            # Per-mille scale (avoids QProgressBar's C-int overflow on
            # multi-GB downloads).
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(done * 1000 / total)))
            done_gb = done / 2**30
            total_gb = total / 2**30
            pct = (done * 100.0) / total
            self.bar.setFormat(f"{pct:.1f} %")
            eta_txt = (
                f" · ETA {eta_s // 60}m {eta_s % 60}s"
                if eta_s and eta_s > 0
                else ""
            )
            self.eta.setText(
                f"{done_gb:.2f} / {total_gb:.2f} GB  ·  "
                f"{mb_per_s:.1f} MB/s{eta_txt}"
            )
        else:
            # HEAD didn't propagate content-length (rare on HF, common
            # on chunked transfer encoding mirrors). Show what we know:
            # bytes in flight + speed; bar stays flat but the label
            # proves the download is alive.
            done_mb = done / 2**20
            self.bar.setFormat("downloading…")
            self.eta.setText(f"{done_mb:.1f} MB downloaded · {mb_per_s:.1f} MB/s")

    def _on_phase(self, phase: str) -> None:
        # Don't clobber the live "Connecting… Ns" counter once the
        # downloader has reported "downloading" but hasn't streamed
        # any chunks yet, the timer's per-second updates are more
        # informative than a stale phase string.
        if phase and self._got_first_progress:
            self.eta.setText(phase)

    def _tick_connect_label(self) -> None:
        if self._got_first_progress:
            self._connect_timer.stop()
            return
        self._connect_secs += 1
        self.eta.setText(
            f"Connecting to Hugging Face… {self._connect_secs}s "
            "(big files take a few seconds to start streaming)"
        )

    def _on_finished(self, ok: bool, err: str) -> None:
        self._connect_timer.stop()
        if ok:
            self.bar.setRange(0, 1000)
            self.bar.setValue(1000)
            self.bar.setFormat("100 %")
            self.eta.setText("✓ done, click Next.")
            self.skip_btn.setVisible(False)
            self._completed = True
        else:
            if self._installer_present:
                # The binary is already on disk thanks to Setup; the
                # only thing the user can do here is retry. Don't
                # offer Skip because that would leave the GUI in a
                # half-installed state with no clear recovery hint.
                self.eta.setText(
                    f"⚠ download failed: {err}.  Check your internet "
                    "connection and click Back / Next to retry, or "
                    "finish the wizard and grab the model from the "
                    "Model Manager later."
                )
            else:
                self.eta.setText(
                    f"⚠ download failed: {err}.  Click Skip to continue; "
                    "you can retry from the Model Manager later."
                )
            # Keep Skip available (when applicable) so the user is
            # never trapped on a failed download page.
            self._completed = False
        self.completeChanged.emit()

    def _on_skip_clicked(self) -> None:
        self._connect_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
        self.eta.setText(
            "Skipped, open the Model Manager from the main window "
            "to download the model when you're ready."
        )
        self._skipped = True
        self.completeChanged.emit()
        # Advance to the next page automatically, clicking Skip is
        # the "I'm done here, move on" gesture, no reason to make
        # the user reach for Next afterwards.
        wiz = self.wizard()
        if wiz is not None:
            wiz.next()


class _Summary(QWizardPage):
    """Final confirmation page, quick recap of what's about to be applied."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Ready to go")
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        hint = QLabel(
            "Click <b>Finish</b> to save these defaults. "
            "Anything here can be tweaked later from the Server view."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(self.body)
        lay.addStretch()
        lay.addWidget(hint)

    def initializePage(self) -> None:  # noqa: D401 - Qt override
        # Pull the selected preset + deployment_mode from the wizard
        # at page-enter time (the user may have backed up to change
        # them) rather than at construction.
        wiz = self.wizard()
        preset = ""
        deploy = ""
        if wiz is not None:
            try:
                preset = wiz.preset_page.selected() or "default"  # type: ignore[attr-defined]
                deploy = getattr(wiz.hw_page, "deployment_mode", "") or ""  # type: ignore[attr-defined]
            except Exception:
                pass
        deploy_human = {
            "local_binary": "Local llama-server binary",
            "docker": "Docker (llama.cpp image)",
            "external": "External server (already running)",
        }.get(deploy, deploy or "Local llama-server binary")
        self.body.setText(
            "<p>Setup is about to apply the following defaults:</p>"
            f"<ul>"
            f"<li><b>Preset:</b> {preset}</li>"
            f"<li><b>Server deployment:</b> {deploy_human}</li>"
            "</ul>"
            "<p>The first time you press <b>Start</b> in the main window, "
            "the chosen server is launched and the model is loaded. "
            "If the model isn't on disk yet, the GUI will prompt you to "
            "open the Model Manager.</p>"
        )


class FirstRunWizard(QWizard):
    finished_with_preset = Signal(str)

    def __init__(self, *, hardware: Optional[HardwareReport] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to Report Anonymizer")
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(720, 520)
        self.hw = hardware or report()
        self.addPage(_Welcome())
        self.hw_page = _Hardware(self.hw)
        self.addPage(self.hw_page)
        self.preset_page = _Preset(self.hw)
        self.addPage(self.preset_page)
        # New: download the chosen preset's GGUF up-front so the GUI
        # is fully usable as soon as the wizard closes. Sits between
        # Preset and Summary; "Skip" keeps the previous behaviour
        # (open Model Manager later) for users without internet.
        self.download_page = _DownloadModel()
        self.addPage(self.download_page)
        self.summary_page = _Summary()
        self.addPage(self.summary_page)

    def accept(self) -> None:
        try:
            materialize_default_model()
        except Exception:
            pass
        try:
            mark_bootstrapped()
        except Exception:
            pass
        # Persist the auto-detected deployment_mode as a user-scope
        # override on EVERY preset so whichever one the user starts
        # next inherits the wizard's choice. Per-preset customisation
        # the user makes later (Configure deployment) still wins,
        # because save_user_profile only writes the file once and
        # subsequent edits keep replacing it.
        sel = self.preset_page.selected() or "default"
        try:
            self._apply_deployment_mode_to_all(getattr(self.hw_page, "deployment_mode", None))
        except Exception:
            pass
        self.finished_with_preset.emit(sel)
        super().accept()

    @staticmethod
    def _apply_deployment_mode_to_all(mode: Optional[str]) -> None:
        """Persist the auto-detected ``deployment_mode`` as a SPARSE
        user override on every preset so the next Start picks it up
        regardless of which preset the user ends up using.

        Uses :func:`save_user_deployment_override` (writes only
        deployment-mode-related fields) instead of
        :func:`save_user_profile` (writes the full profile). The
        sparse override lets builtins keep evolving (model_repo /
        model_filename / sampling / ctx_size all track upstream)
        while honouring the wizard's deployment choice.

        Presets that already match the chosen mode are skipped.
        """
        if not mode:
            return
        from anonymize.server_profile import (
            DEFAULT_DOCKER_IMAGE,
            load_profiles,
            save_user_deployment_override,
        )

        for prof in load_profiles():
            if getattr(prof, "deployment_mode", "local_binary") == mode:
                continue
            try:
                save_user_deployment_override(
                    prof.name,
                    deployment_mode=mode,
                    docker_image=getattr(prof, "docker_image", None) or DEFAULT_DOCKER_IMAGE,
                    docker_gpu=getattr(prof, "docker_gpu", True),
                )
            except Exception:
                continue


__all__ = ["FirstRunWizard"]
