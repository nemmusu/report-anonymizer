"""Preset editor: full-form editor with command preview + save user/project."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from anonymize.server_profile import (
    ServerProfile,
    SamplingConfig,
    render_command,
    save_user_profile,
    save_project_profile,
)


CTX_SUGGESTIONS = (8192, 16384, 32768, 65536, 131072, 262144, 500_000, 1_000_000)


class PresetEditor(QDialog):
    saved = Signal(object)  # ServerProfile

    def __init__(
        self,
        profile: ServerProfile,
        *,
        project_dir: Optional[Path] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit preset · {profile.name}")
        self.resize(820, 720)
        self.profile = profile.clone(name=profile.name) if profile.is_builtin else profile
        self._project_dir = project_dir

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        form = QVBoxLayout(body)

        # ---- Identity ------------------------------------------------------
        ident = QGroupBox("Identity")
        idform = QFormLayout(ident)
        self.name = QLineEdit(self.profile.name)
        self.desc = QLineEdit(self.profile.description)
        idform.addRow("Name", self.name)
        idform.addRow("Description", self.desc)
        form.addWidget(ident)

        # ---- Runtime -------------------------------------------------------
        rt = QGroupBox("Runtime")
        rtform = QFormLayout(rt)

        # Deployment mode: picks how Start materialises a real
        # llama-server.  The follow-up rows (binary path / Docker
        # image) hide and show themselves so the form stays focused
        # on what the picked mode actually needs.
        self.deployment = QComboBox()
        # Windows ships with a native llama.cpp binary installed by
        # the Setup wizard; Docker Desktop is unnecessary friction
        # there, so the docker mode is hidden from the combo box on
        # win32. If a profile imported from another OS happens to
        # carry "docker", findData() returns -1 below and we fall
        # back to the first entry (local_binary).
        import sys as _sys
        _modes = [
            ("local_binary", "Local binary (llama-server on this machine)"),
        ]
        if _sys.platform != "win32":
            _modes.append(("docker", "Docker (managed by the GUI)"))
        _modes.append(("external", "External (already running, just connect)"))
        for mode_id, mode_label in _modes:
            self.deployment.addItem(mode_label, mode_id)
        current_mode = getattr(self.profile, "deployment_mode", "local_binary")
        self.deployment.setCurrentIndex(
            max(0, self.deployment.findData(current_mode))
        )
        self.deployment.currentIndexChanged.connect(self._refresh_deployment_rows)
        rtform.addRow("Deployment", self.deployment)

        self.binary = QLineEdit(self.profile.binary)
        binrow = QHBoxLayout()
        binrow.addWidget(self.binary, 1)
        bin_btn = QPushButton("Browse…")
        bin_btn.clicked.connect(self._pick_binary)
        binrow.addWidget(bin_btn)
        self._binary_row = self._wrap(binrow)
        self._binary_label = QLabel("llama-server binary")
        rtform.addRow(self._binary_label, self._binary_row)

        self.docker_image = QLineEdit(
            getattr(self.profile, "docker_image", "")
            or "ghcr.io/ggml-org/llama.cpp:server-cuda"
        )
        self.docker_gpu = QCheckBox("Use GPU (--gpus all)")
        self.docker_gpu.setChecked(
            bool(getattr(self.profile, "docker_gpu", True))
        )
        self._docker_image_label = QLabel("Docker image")
        self._docker_gpu_label = QLabel("")  # checkbox carries its own text
        rtform.addRow(self._docker_image_label, self.docker_image)
        rtform.addRow(self._docker_gpu_label, self.docker_gpu)

        self.host = QLineEdit(self.profile.host)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(self.profile.port)
        rtform.addRow("Host", self.host)
        rtform.addRow("Port", self.port)
        form.addWidget(rt)
        self._refresh_deployment_rows()

        # ---- Model ---------------------------------------------------------
        md = QGroupBox("Model")
        mdform = QFormLayout(md)
        self.model = QLineEdit(self.profile.model)
        modelrow = QHBoxLayout()
        modelrow.addWidget(self.model, 1)
        m_btn = QPushButton("Browse…")
        m_btn.clicked.connect(self._pick_model)
        modelrow.addWidget(m_btn)
        mdform.addRow("Model file (.gguf)", self._wrap(modelrow))
        self.repo = QLineEdit(self.profile.model_repo or "")
        self.repo_file = QLineEdit(self.profile.model_filename or "")
        mdform.addRow("HF repo_id", self.repo)
        mdform.addRow("HF filename", self.repo_file)
        form.addWidget(md)

        # ---- Performance ---------------------------------------------------
        perf = QGroupBox("Performance")
        pf = QFormLayout(perf)
        self.parallel = QSpinBox(); self.parallel.setRange(1, 64); self.parallel.setValue(self.profile.parallel)
        self.ctx = QComboBox(); self.ctx.setEditable(True)
        for v in CTX_SUGGESTIONS:
            self.ctx.addItem(f"{v:,}", v)
        if self.profile.ctx_size in CTX_SUGGESTIONS:
            self.ctx.setCurrentIndex(CTX_SUGGESTIONS.index(self.profile.ctx_size))
        else:
            self.ctx.setEditText(str(self.profile.ctx_size))
        self.gpu_layers = QSpinBox(); self.gpu_layers.setRange(0, 999); self.gpu_layers.setValue(self.profile.n_gpu_layers)
        self.threads = QSpinBox(); self.threads.setRange(0, 256); self.threads.setValue(self.profile.threads or 0)
        self.batch = QSpinBox(); self.batch.setRange(1, 65536); self.batch.setValue(self.profile.batch_size)
        self.ubatch = QSpinBox(); self.ubatch.setRange(1, 65536); self.ubatch.setValue(self.profile.ubatch_size)
        self.fa = QCheckBox("Flash attention"); self.fa.setChecked(self.profile.flash_attn)
        self.mmap = QCheckBox("mmap"); self.mmap.setChecked(self.profile.mmap)
        self.no_warm = QCheckBox("No warmup"); self.no_warm.setChecked(self.profile.no_warmup)
        pf.addRow("Parallel slots", self.parallel)
        pf.addRow("Context size (tokens)", self.ctx)
        pf.addRow("GPU layers (n_gpu_layers)", self.gpu_layers)
        pf.addRow("Threads (0 = auto)", self.threads)
        pf.addRow("Batch size (-b)", self.batch)
        pf.addRow("Micro-batch (-ub)", self.ubatch)
        pf.addRow("", self.fa)
        pf.addRow("", self.mmap)
        pf.addRow("", self.no_warm)
        form.addWidget(perf)

        # ---- KV cache ------------------------------------------------------
        kv = QGroupBox("KV cache")
        kvf = QFormLayout(kv)
        self.cache_k = QComboBox(); self.cache_k.addItems(["f16", "bf16", "q8_0", "q4_0"])
        self.cache_v = QComboBox(); self.cache_v.addItems(["f16", "bf16", "q8_0", "q4_0"])
        self.cache_k.setCurrentText(self.profile.cache_type_k)
        self.cache_v.setCurrentText(self.profile.cache_type_v)
        kvf.addRow("cache-type-k (-ctk)", self.cache_k)
        kvf.addRow("cache-type-v (-ctv)", self.cache_v)
        form.addWidget(kv)

        # ---- Long context --------------------------------------------------
        lc = QGroupBox("Long context (RoPE / YARN)")
        lcf = QFormLayout(lc)
        self.rope_scaling = QComboBox(); self.rope_scaling.addItems(["", "linear", "yarn"])
        self.rope_scaling.setCurrentText(self.profile.rope_scaling or "")
        self.rope_scale = QDoubleSpinBox(); self.rope_scale.setRange(0.0, 64.0); self.rope_scale.setSingleStep(0.5)
        self.rope_scale.setValue(self.profile.rope_scale or 0.0)
        self.rope_freq = QDoubleSpinBox(); self.rope_freq.setRange(0.0, 1e9); self.rope_freq.setDecimals(1)
        self.rope_freq.setValue(self.profile.rope_freq_base or 0.0)
        self.yarn_orig = QSpinBox(); self.yarn_orig.setRange(0, 2_000_000); self.yarn_orig.setValue(self.profile.yarn_orig_ctx or 0)
        self.override_kv = QPlainTextEdit("\n".join(self.profile.override_kv or []))
        self.override_kv.setPlaceholderText("one --override-kv entry per line, e.g. qwen3.context_length=int:1000000")
        lcf.addRow("rope-scaling", self.rope_scaling)
        lcf.addRow("rope-scale (0 = unset)", self.rope_scale)
        lcf.addRow("rope-freq-base (0 = unset)", self.rope_freq)
        lcf.addRow("yarn-orig-ctx (0 = unset)", self.yarn_orig)
        lcf.addRow("override-kv (one per line)", self.override_kv)
        form.addWidget(lc)

        # ---- Sampling ------------------------------------------------------
        smp = QGroupBox("Sampling (HTTP requests)")
        smpf = QFormLayout(smp)
        s = self.profile.sampling
        self.temp = QDoubleSpinBox(); self.temp.setRange(0.0, 2.0); self.temp.setSingleStep(0.05); self.temp.setValue(s.temperature)
        self.top_k = QSpinBox(); self.top_k.setRange(0, 1000); self.top_k.setValue(s.top_k)
        self.top_p = QDoubleSpinBox(); self.top_p.setRange(0.0, 1.0); self.top_p.setSingleStep(0.05); self.top_p.setValue(s.top_p)
        self.min_p = QDoubleSpinBox(); self.min_p.setRange(0.0, 1.0); self.min_p.setSingleStep(0.05); self.min_p.setValue(s.min_p)
        self.repeat_pen = QDoubleSpinBox(); self.repeat_pen.setRange(0.0, 4.0); self.repeat_pen.setSingleStep(0.05); self.repeat_pen.setValue(s.repeat_penalty)
        smpf.addRow("temperature", self.temp)
        smpf.addRow("top_k", self.top_k)
        smpf.addRow("top_p", self.top_p)
        smpf.addRow("min_p", self.min_p)
        smpf.addRow("repeat_penalty", self.repeat_pen)
        form.addWidget(smp)

        # ---- Misc ----------------------------------------------------------
        misc = QGroupBox("Misc")
        mf = QFormLayout(misc)
        self.chat_template = QLineEdit(self.profile.chat_template or "")
        self.template_file = QLineEdit(self.profile.template_file or "")
        self.jinja = QCheckBox("--jinja"); self.jinja.setChecked(self.profile.jinja)
        self.no_webui = QCheckBox("--no-webui"); self.no_webui.setChecked(self.profile.no_webui)
        self.verbose = QCheckBox("--verbose"); self.verbose.setChecked(self.profile.verbose)
        self.extra = QPlainTextEdit("\n".join(self.profile.extra_args or []))
        self.extra.setPlaceholderText("extra raw flags (one per line)")
        mf.addRow("chat-template", self.chat_template)
        mf.addRow("template file", self.template_file)
        mf.addRow("", self.jinja)
        mf.addRow("", self.no_webui)
        mf.addRow("", self.verbose)
        mf.addRow("extra args", self.extra)
        form.addWidget(misc)

        # ---- Command preview ----------------------------------------------
        pv = QGroupBox("Command preview")
        pvl = QVBoxLayout(pv)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(110)
        pvl.addWidget(self.preview)
        refresh_btn = QPushButton("Refresh preview")
        refresh_btn.clicked.connect(self._refresh_preview)
        pvl.addWidget(refresh_btn)
        form.addWidget(pv)

        # ---- Actions -------------------------------------------------------
        actions = QHBoxLayout()
        save_user = QPushButton("Save as user preset")
        save_user.setObjectName("PrimaryButton")
        save_user.clicked.connect(lambda: self._save(scope="user"))
        save_proj = QPushButton("Save in project")
        save_proj.clicked.connect(lambda: self._save(scope="project"))
        save_proj.setEnabled(project_dir is not None)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save_proj)
        actions.addWidget(save_user)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addLayout(actions)

        self._refresh_preview()

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _pick_binary(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Select llama-server binary")
        if p:
            self.binary.setText(p)

    def _pick_model(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Select GGUF model", filter="GGUF (*.gguf)")
        if p:
            self.model.setText(p)

    def _refresh_deployment_rows(self) -> None:
        """Show / hide the deployment-specific rows so the form
        only carries the fields the picked mode actually uses.
        Local binary -> binary path row; Docker -> image + GPU
        toggle; External -> hide both, the user only edits host:port."""
        mode = self.deployment.currentData() or "local_binary"
        is_local = mode == "local_binary"
        is_docker = mode == "docker"
        for w in (self._binary_label, self._binary_row, self.binary):
            w.setVisible(is_local)
        for w in (self._docker_image_label, self.docker_image,
                  self._docker_gpu_label, self.docker_gpu):
            w.setVisible(is_docker)

    def _build_profile(self) -> ServerProfile:
        p = ServerProfile(
            name=self.name.text().strip() or self.profile.name,
            description=self.desc.text().strip(),
            is_builtin=False,
            source="user",
            deployment_mode=(self.deployment.currentData() or "local_binary"),
            binary=self.binary.text().strip() or self.profile.binary,
            docker_image=(
                self.docker_image.text().strip()
                or getattr(self.profile, "docker_image", "")
            ),
            docker_gpu=self.docker_gpu.isChecked(),
            host=self.host.text().strip() or "127.0.0.1",
            port=int(self.port.value()),
            model=self.model.text().strip(),
            model_repo=self.repo.text().strip() or None,
            model_filename=self.repo_file.text().strip() or None,
            parallel=int(self.parallel.value()),
            ctx_size=int(self._ctx_value()),
            n_gpu_layers=int(self.gpu_layers.value()),
            threads=int(self.threads.value()) or None,
            batch_size=int(self.batch.value()),
            ubatch_size=int(self.ubatch.value()),
            flash_attn=self.fa.isChecked(),
            mmap=self.mmap.isChecked(),
            no_warmup=self.no_warm.isChecked(),
            cache_type_k=self.cache_k.currentText(),
            cache_type_v=self.cache_v.currentText(),
            rope_scaling=self.rope_scaling.currentText() or None,
            rope_scale=float(self.rope_scale.value()) or None,
            rope_freq_base=float(self.rope_freq.value()) or None,
            yarn_orig_ctx=int(self.yarn_orig.value()) or None,
            override_kv=[s.strip() for s in self.override_kv.toPlainText().splitlines() if s.strip()],
            chat_template=self.chat_template.text().strip() or None,
            template_file=self.template_file.text().strip() or None,
            jinja=self.jinja.isChecked(),
            no_webui=self.no_webui.isChecked(),
            verbose=self.verbose.isChecked(),
            extra_args=[s.strip() for s in self.extra.toPlainText().splitlines() if s.strip()],
            sampling=SamplingConfig(
                temperature=float(self.temp.value()),
                top_k=int(self.top_k.value()),
                top_p=float(self.top_p.value()),
                min_p=float(self.min_p.value()),
                repeat_penalty=float(self.repeat_pen.value()),
            ),
        )
        return p

    def _ctx_value(self) -> int:
        try:
            text = self.ctx.currentText().replace(",", "").strip()
            return int(text)
        except Exception:
            return self.profile.ctx_size

    def _refresh_preview(self) -> None:
        try:
            cmd = render_command(self._build_profile())
        except Exception as e:
            self.preview.setPlainText(f"<error: {e}>")
            return
        self.preview.setPlainText(" \\\n  ".join(cmd))

    def _save(self, *, scope: str) -> None:
        prof = self._build_profile()
        try:
            if scope == "user":
                save_user_profile(prof)
            else:
                if self._project_dir is None:
                    return
                save_project_profile(prof, self._project_dir)
        except Exception:
            return
        self.saved.emit(prof)
        self.accept()


__all__ = ["PresetEditor"]
