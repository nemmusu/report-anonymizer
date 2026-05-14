"""Preset gallery: cards for each ServerProfile.

Each card shows the preset name, description, summarized parameters, the
download status of its model, and a hardware-compatibility badge.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anonymize.hardware import HardwareReport, compatibility
from anonymize.hf_models import MODELS_DIR
from anonymize.server_profile import (
    ServerProfile,
    get_default_profile_name,
    load_profiles,
    set_default_profile_name,
)


class PresetCard(QFrame):
    use_clicked = Signal(str)
    customize_clicked = Signal(str)
    download_clicked = Signal(str)
    delete_clicked = Signal(str)
    set_default_clicked = Signal(str)

    def __init__(
        self,
        profile: ServerProfile,
        hw: Optional[HardwareReport],
        *,
        is_default: bool = False,
    ) -> None:
        super().__init__()
        self.setObjectName("PresetCard")
        self.setProperty("selected", False)
        self.setProperty("isDefault", "true" if is_default else "false")
        self.profile = profile
        self.is_default = is_default

        title_text = ("★ " if is_default else "") + profile.name
        title = QLabel(title_text)
        title.setObjectName("H2")
        if is_default:
            title.setToolTip("This preset is the default, used on app start.")
        src = QLabel(profile.source)
        src.setObjectName("Caption")
        # Wrap title + scope chip in their own subtly-tinted strip so
        # the card has visible hierarchy instead of one flat block of
        # dark text on a dark surface.
        head_frame = QFrame()
        head_frame.setObjectName("PresetCardHead")
        head_lay = QHBoxLayout(head_frame)
        head_lay.setContentsMargins(12, 8, 12, 8)
        head_lay.setSpacing(8)
        head_lay.addWidget(title)
        head_lay.addStretch()
        head_lay.addWidget(src)

        desc = QLabel(profile.description or "-")
        desc.setObjectName("Muted")
        desc.setWordWrap(True)

        meta_lines = []
        meta_lines.append(
            f"ctx {profile.ctx_size:,} · parallel {profile.parallel} · GPU layers {profile.n_gpu_layers}"
        )
        if profile.rope_scaling:
            meta_lines.append(
                f"rope: {profile.rope_scaling} · orig_ctx {profile.yarn_orig_ctx or '-'}"
            )
        if profile.cache_type_k != "f16" or profile.cache_type_v != "f16":
            meta_lines.append(f"kv cache: K={profile.cache_type_k} V={profile.cache_type_v}")
        if profile.model_repo and profile.model_filename:
            meta_lines.append(f"HF: {profile.model_repo}")
        meta = QLabel("\n".join(meta_lines))
        meta.setObjectName("Muted")
        meta.setWordWrap(True)

        # status badges
        present = profile.is_model_present()
        downloaded_lbl = QLabel("downloaded" if present else "not downloaded")
        downloaded_lbl.setObjectName("BadgeOk" if present else "BadgeMuted")

        # compatibility (best-effort)
        compat_lbl = QLabel("")
        if hw and present:
            try:
                size_b = profile.model_path.stat().st_size if profile.model_path.exists() else 0
            except Exception:
                size_b = 0
            avail_vram = sum(g.vram_free_mb for g in hw.gpus) if hw.gpus else 0
            # ``available_mb`` from the Memory report, psutil's
            # "available" includes inactive page cache that can be
            # reclaimed, so it's the right number to compare a
            # CPU-only model against.
            avail_ram = hw.memory.available_mb if hw.memory else 0
            cmp = compatibility(
                model_size_bytes=size_b,
                n_gpu_layers=profile.n_gpu_layers,
                ctx_size=profile.ctx_size,
                cache_type_k=profile.cache_type_k,
                cache_type_v=profile.cache_type_v,
                available_vram_mb=avail_vram,
                available_ram_mb=avail_ram,
            )
            compat_lbl.setText(cmp.message)
            compat_lbl.setObjectName(
                {
                    "ok": "BadgeOk",
                    "tight": "BadgeWarn",
                    "likely_oom": "BadgeErr",
                    "cpu_fallback": "BadgeMuted",
                }.get(cmp.level, "BadgeMuted")
            )

        badges = QHBoxLayout()
        badges.addWidget(downloaded_lbl)
        if compat_lbl.text():
            badges.addWidget(compat_lbl)
        badges.addStretch()

        use_btn = QPushButton("Use")
        use_btn.setObjectName("PrimaryButton")
        cust_btn = QPushButton("Customize")
        dl_btn = QPushButton("Download" if not present else "Re-download")
        # "Set as default" toggles the preferred startup preset.
        # Disabled (greyed-out) when this card is already the default
        # so the user has clear visual feedback. Tooltip explains why.
        default_btn = QPushButton("Default ✓" if is_default else "Set as default")
        default_btn.setToolTip(
            "Already the default, used on every app start."
            if is_default
            else "Make this preset the one auto-loaded on app start."
        )
        default_btn.setEnabled(not is_default)

        use_btn.clicked.connect(lambda: self.use_clicked.emit(profile.name))
        cust_btn.clicked.connect(lambda: self.customize_clicked.emit(profile.name))
        dl_btn.clicked.connect(lambda: self.download_clicked.emit(profile.name))
        default_btn.clicked.connect(
            lambda: self.set_default_clicked.emit(profile.name)
        )

        # Delete is only allowed for user-owned / project-local
        # presets, builtin presets ship with the app and removing
        # them via the GUI would be confusing (they reappear on
        # restart). The button is therefore hidden on builtin cards.
        del_btn: Optional[QPushButton] = None
        if not profile.is_builtin:
            del_btn = QPushButton("Delete")
            del_btn.setObjectName("DangerButton")
            del_btn.setToolTip(
                "Delete this preset from the user / project YAML "
                "where it lives. Builtin presets cannot be deleted."
            )
            del_btn.clicked.connect(
                lambda: self.delete_clicked.emit(profile.name)
            )

        # Wrap the action buttons into a 2-column grid so they wrap
        # cleanly to a second row when the card is narrow (the
        # gallery falls back to a single column at 280 px wide and
        # five buttons in one row don't fit). Every button shares
        # the same column-stretch so labels stay readable.
        from PySide6.QtWidgets import QSizePolicy

        for b in (use_btn, cust_btn, dl_btn, default_btn) + (
            (del_btn,) if del_btn is not None else ()
        ):
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setMinimumWidth(0)
        actions = QGridLayout()
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        actions.setColumnStretch(0, 1)
        actions.setColumnStretch(1, 1)
        actions.addWidget(use_btn, 0, 0)
        actions.addWidget(cust_btn, 0, 1)
        actions.addWidget(dl_btn, 1, 0)
        actions.addWidget(default_btn, 1, 1)
        if del_btn is not None:
            actions.addWidget(del_btn, 2, 0, 1, 2)

        # Body holds everything below the title strip; it gets its
        # own padding so the title strip can stretch to the card
        # edges and the body still breathes.
        body_lay = QVBoxLayout()
        body_lay.setContentsMargins(14, 10, 14, 12)
        body_lay.setSpacing(6)
        body_lay.addWidget(desc)
        body_lay.addWidget(meta)
        body_lay.addLayout(badges)
        body_lay.addLayout(actions)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(head_frame)
        lay.addLayout(body_lay)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().polish(self)


class PresetGallery(QScrollArea):
    """Responsive grid of preset cards.

    The number of columns adapts to the width of the parent dock so the
    panel stays usable on small screens (single column when narrow).
    """

    use_clicked = Signal(str)
    customize_clicked = Signal(str)
    download_clicked = Signal(str)
    delete_clicked = Signal(str)
    default_changed = Signal(str)  # emitted with the new default preset name

    _CARD_MIN_WIDTH = 280  # px; below this we fall back to 1 column

    def __init__(self, *, hw: Optional[HardwareReport] = None) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._hw = hw
        self._cards: dict[str, PresetCard] = {}
        self._columns = 1

        self._inner = QWidget()
        self._grid = QGridLayout(self._inner)
        self._grid.setSpacing(10)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self.setWidget(self._inner)
        self.refresh()

    def set_hardware(self, hw: Optional[HardwareReport]) -> None:
        self._hw = hw
        self.refresh()

    def refresh(self) -> None:
        # Properly tear down old cards. ``setParent(None)`` on a
        # visible QFrame re-parents it to the desktop and Qt
        # promptly turns each detached card into a top-level window;
        # repeated refreshes (e.g. one per finished download) then
        # spawn dozens of ghost ``Document Anonymizer`` windows.
        # Hide first, remove from the grid layout, then schedule a
        # Qt-side deletion so the C++ object is reaped on the next
        # event-loop tick.
        for c in self._cards.values():
            try:
                c.hide()
                self._grid.removeWidget(c)
            except Exception:
                pass
            c.deleteLater()
        self._cards.clear()
        profiles = load_profiles()
        cols = self._compute_columns()
        self._columns = cols
        # Resolve the active default name once per refresh so every card
        # can decide whether it is "the default".
        default_name = get_default_profile_name()
        if not default_name:
            # If the user never set one, treat the literal "default" as
            # the implicit default (matches ``get_default_profile``).
            if any(p.name == "default" for p in profiles):
                default_name = "default"
        for i, p in enumerate(profiles):
            card = PresetCard(p, self._hw, is_default=(p.name == default_name))
            card.setMinimumWidth(self._CARD_MIN_WIDTH)
            card.use_clicked.connect(self.use_clicked.emit)
            card.customize_clicked.connect(self.customize_clicked.emit)
            card.download_clicked.connect(self.download_clicked.emit)
            card.delete_clicked.connect(self.delete_clicked.emit)
            card.set_default_clicked.connect(self._on_set_default)
            self._grid.addWidget(card, i // cols, i % cols)
            self._cards[p.name] = card

    def _on_set_default(self, name: str) -> None:
        """Persist ``name`` as the default preset and refresh the gallery
        so the badge moves to the new card."""
        try:
            set_default_profile_name(name)
        except Exception:
            return
        self.default_changed.emit(name)
        self.refresh()

    def _compute_columns(self) -> int:
        avail = max(self.viewport().width() - 24, self._CARD_MIN_WIDTH)
        return max(1, avail // (self._CARD_MIN_WIDTH + 12))

    def resizeEvent(self, event):  # noqa: D401, N802 - Qt override
        super().resizeEvent(event)
        new_cols = self._compute_columns()
        if new_cols != self._columns and self._cards:
            self._relayout(new_cols)

    def _relayout(self, cols: int) -> None:
        self._columns = cols
        items = list(self._cards.values())
        for c in items:
            self._grid.removeWidget(c)
        for i, c in enumerate(items):
            self._grid.addWidget(c, i // cols, i % cols)

    def select(self, name: str) -> None:
        for k, c in self._cards.items():
            c.set_selected(k == name)


__all__ = ["PresetCard", "PresetGallery"]
