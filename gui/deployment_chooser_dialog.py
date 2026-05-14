"""Recovery / first-time picker for the server deployment mode.

Shown when ``Start`` fails because llama-server is not installed
(``deployment_mode = local_binary`` but the binary is not on PATH),
or when Docker mode hits a missing daemon, or when External mode
times out on the health probe. Also reachable from the Server panel
as "Configure deployment…" for proactive switching.

The dialog presents the same three modes the preset editor exposes,
highlights the one ``suggest_deployment_mode`` recommends for this
host, lets the user pick, and persists the choice as a *user-scope
override* on the active preset (so the builtin keeps its canonical
shape and the next launch picks up the choice automatically).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from anonymize.hardware import (
    active_llama_variant,
    discover_llama_variants,
    suggest_deployment_mode,
)
from anonymize.server_profile import (
    DEFAULT_DOCKER_IMAGE,
    ServerProfile,
    save_user_profile,
)


class DeploymentChooserDialog(QDialog):
    def __init__(
        self,
        profile: ServerProfile,
        *,
        reason: str = "",
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose how to run llama-server")
        self.setModal(True)
        self.profile = profile
        self.resulting_mode: Optional[str] = None

        suggested, hint = suggest_deployment_mode()

        head = QLabel(
            "<b>llama-server is not running.</b><br/>"
            f"{(reason or 'Pick how the GUI should bring it up').strip()}.<br/>"
            f"<span style='color:#9aa0a6'>{hint}</span>"
        )
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)

        # Three radio buttons + per-mode help text. The current
        # active mode is pre-selected so an external user landing
        # here from a "no health response" doesn't lose their
        # config; otherwise the auto-detect suggestion wins.
        active = getattr(self.profile, "deployment_mode", "local_binary")
        if active == "local_binary" and suggested != "local_binary":
            active = suggested

        self.group = QButtonGroup(self)

        self.rb_local = QRadioButton("Local binary (llama-server on this machine)")
        self.rb_local.setToolTip(
            "Use a llama.cpp build that's already installed on PATH "
            "or at a custom path. Fastest if you've already compiled "
            "llama.cpp."
        )
        self.rb_docker = QRadioButton("Docker (managed by the GUI)")
        self.rb_docker.setToolTip(
            "The GUI will pull the llama.cpp Docker image once and "
            "spawn / stop the container for you on every Start / "
            "Stop. Ideal if you don't have llama-server installed."
        )
        self.rb_external = QRadioButton("External (already running, just connect)")
        self.rb_external.setToolTip(
            "Bring your own server (manual launch, systemd, k8s, "
            "remote box). The GUI never spawns a process and just "
            "talks to host:port over HTTP."
        )

        self.group.addButton(self.rb_local, 0)
        self.group.addButton(self.rb_docker, 1)
        self.group.addButton(self.rb_external, 2)

        for rb, mode in (
            (self.rb_local, "local_binary"),
            (self.rb_docker, "docker"),
            (self.rb_external, "external"),
        ):
            if mode == active:
                rb.setChecked(True)

        # Per-mode extra fields. Only visible when the matching
        # radio is selected, so the dialog stays focused on what
        # the picked mode actually needs.
        self.binary = QLineEdit(self.profile.binary or "llama-server")
        self.binary.setPlaceholderText("llama-server (PATH) or /full/path/to/llama-server")

        # Installer-aware variant picker: on Windows installs that
        # bundled cpu / cuda / vulkan llama-server variants, surface a
        # dropdown so the operator can switch backend in one click
        # instead of editing the binary path by hand. The picker
        # replaces the bare QLineEdit on those hosts; on every other
        # setup (no sentinel, no bundled variants) the legacy textbox
        # is the only widget visible.
        self._llama_variants: dict[str, Path] = discover_llama_variants()
        self.variant_combo = QComboBox()
        self.variant_combo.setToolTip(
            "Switch the llama-server backend without editing the "
            "binary path. CPU is the universal fallback, CUDA targets "
            "NVIDIA GPUs, Vulkan covers most other GPUs (AMD / Intel "
            "/ NVIDIA via Vulkan)."
        )
        _human = {
            "cpu": "CPU (AVX2, universal fallback)",
            "cuda": "CUDA (NVIDIA GPUs)",
            "vulkan": "Vulkan (AMD / Intel / NVIDIA)",
        }
        for v in ("cpu", "cuda", "vulkan"):
            if v in self._llama_variants:
                self.variant_combo.addItem(_human[v], v)
        # Preselect the variant matching the active profile's binary
        # (or, failing that, the variant the installer recorded).
        self._preselect_variant()

        self.docker_image = QLineEdit(
            getattr(self.profile, "docker_image", "") or DEFAULT_DOCKER_IMAGE
        )
        self.docker_gpu = QCheckBox("Use GPU (--gpus all)")
        self.docker_gpu.setChecked(bool(getattr(self.profile, "docker_gpu", True)))

        self.host = QLineEdit(self.profile.host or "127.0.0.1")
        self.port = QLineEdit(str(self.profile.port or 8080))

        local_form = QFormLayout()
        if self._llama_variants:
            local_form.addRow("Backend", self.variant_combo)
            # Keep the binary path field around for power users (e.g.
            # they pointed at a hand-built llama.cpp), but hide it
            # when the variant picker is present. The user can still
            # override by editing the underlying preset YAML.
            self.binary.setVisible(False)
        else:
            local_form.addRow("Binary path", self.binary)

        docker_form = QFormLayout()
        docker_form.addRow("Image", self.docker_image)
        docker_form.addRow("", self.docker_gpu)

        ext_form = QFormLayout()
        ext_row = QHBoxLayout()
        ext_row.addWidget(self.host, 2)
        ext_row.addWidget(QLabel(":"))
        ext_row.addWidget(self.port, 1)
        ext_form.addRow("Server endpoint", self._wrap(ext_row))

        # Wrap each block in a container so we can hide/show them
        # in one call when the active radio changes.
        from PySide6.QtWidgets import QWidget

        self._local_box = QWidget()
        self._local_box.setLayout(local_form)
        self._docker_box = QWidget()
        self._docker_box.setLayout(docker_form)
        self._ext_box = QWidget()
        self._ext_box.setLayout(ext_form)

        for rb in (self.rb_local, self.rb_docker, self.rb_external):
            rb.toggled.connect(self._refresh_visibility)

        # Suggestion footer near the buttons so the user sees what
        # the GUI thinks is best right above the action.
        footer = QLabel(
            f"<i>Recommended for this machine:</i> <b>{self._mode_label(suggested)}</b>"
        )
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.setObjectName("Muted")

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("Use this mode")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(head)
        lay.addSpacing(4)
        lay.addWidget(self.rb_local)
        lay.addWidget(self._local_box)
        lay.addWidget(self.rb_docker)
        lay.addWidget(self._docker_box)
        lay.addWidget(self.rb_external)
        lay.addWidget(self._ext_box)
        lay.addStretch()
        lay.addWidget(footer)
        lay.addWidget(bb)

        self._refresh_visibility()

        if sys.platform == "win32":
            # Docker Desktop is heavyweight friction on Windows: the
            # Setup wizard already installs a native llama.cpp binary
            # (CPU/CUDA/Vulkan variant chosen at install time). Hide
            # the Docker row entirely so the user isn't led astray.
            self.rb_docker.setVisible(False)
            self._docker_box.setVisible(False)
            self.rb_docker.setEnabled(False)
            if self.rb_docker.isChecked():
                # Defensive: profile imported from Linux/macOS could
                # have deployment_mode == "docker". Fall back to the
                # native local binary so the dialog doesn't open in
                # an unreachable state.
                self.rb_local.setChecked(True)

    def _preselect_variant(self) -> None:
        """Align the variant dropdown with the active profile.

        Tries an exact path match against ``profile.binary`` first,
        then falls back to the variant recorded by the installer
        sentinel. Does nothing when the dropdown has no items.
        """
        if not self._llama_variants:
            return
        try:
            current = Path(self.profile.binary or "").resolve()
        except Exception:
            current = None
        matched: Optional[str] = None
        for v, path in self._llama_variants.items():
            try:
                if current is not None and path.resolve() == current:
                    matched = v
                    break
            except Exception:
                continue
        if matched is None:
            sentinel_variant = active_llama_variant()
            if sentinel_variant in self._llama_variants:
                matched = sentinel_variant
        if matched is None:
            return
        idx = self.variant_combo.findData(matched)
        if idx >= 0:
            self.variant_combo.setCurrentIndex(idx)

    @staticmethod
    def _mode_label(mode: str) -> str:
        return {
            "local_binary": "Local binary",
            "docker": "Docker (managed)",
            "external": "External server",
        }.get(mode, mode)

    @staticmethod
    def _wrap(layout) -> "object":
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.setLayout(layout)
        return w

    def _refresh_visibility(self) -> None:
        self._local_box.setVisible(self.rb_local.isChecked())
        if sys.platform != "win32":
            self._docker_box.setVisible(self.rb_docker.isChecked())
        self._ext_box.setVisible(self.rb_external.isChecked())

    def _selected_mode(self) -> str:
        if self.rb_docker.isChecked():
            return "docker"
        if self.rb_external.isChecked():
            return "external"
        return "local_binary"

    def _accept(self) -> None:
        mode = self._selected_mode()
        # Apply the mode-specific fields the user filled in.
        if mode == "local_binary":
            if self._llama_variants:
                variant = self.variant_combo.currentData()
                path = self._llama_variants.get(variant) if variant else None
                if path is not None:
                    self.profile.binary = str(path)
                else:
                    # Defensive fallback when the dropdown is somehow
                    # empty/unsettled: keep whatever was already on
                    # the profile.
                    self.profile.binary = (
                        self.profile.binary or "llama-server"
                    )
            else:
                self.profile.binary = (
                    self.binary.text().strip() or self.profile.binary or "llama-server"
                )
        elif mode == "docker":
            self.profile.docker_image = (
                self.docker_image.text().strip()
                or getattr(self.profile, "docker_image", "")
                or DEFAULT_DOCKER_IMAGE
            )
            self.profile.docker_gpu = self.docker_gpu.isChecked()
        else:  # external
            self.profile.host = self.host.text().strip() or "127.0.0.1"
            try:
                port_value = int(self.port.text().strip() or "8080")
                if 1 <= port_value <= 65535:
                    self.profile.port = port_value
            except ValueError:
                pass
        self.profile.deployment_mode = mode
        # Persist as a user-scope override so the choice survives
        # a fresh launch and the builtin definition stays canonical.
        try:
            self.profile.source = "user"
            self.profile.is_builtin = False
            save_user_profile(self.profile)
        except Exception:
            pass
        self.resulting_mode = mode
        self.accept()


__all__ = ["DeploymentChooserDialog"]
