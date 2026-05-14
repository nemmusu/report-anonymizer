"""Pipeline runner view: stage cards with Run/Stop + global Stop + reset.

After **Scan & detect** completes, the **Approve & promote** card enters an
orange ``PAUSED`` state: the user clicks ``Approve & continue`` to merge
Tier-0 / Tier-1 / pending YAML into ``substitution_map.yml``, then Apply runs.
Manual **Run** on Apply/Build/Verify is disabled until that gate clears so
the queue order is never accidentally inverted.
"""
from __future__ import annotations

from typing import Optional

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from anonymize.app_settings import get_str, set_str

from .icons import icon
from .state import AppState
from .theme import PALETTE
from .toast import Toaster


STAGES = (
    ("scan", "Scan & detect", "Tier-0 deterministic + Tier-1 LLM detection."),
    (
        "promote",
        "Approve & promote",
        "Merge auto-promoted + reviewed candidates into substitution_map.yml.",
    ),
    ("apply", "Apply", "Apply substitution_map.yml to the input."),
    ("build", "Build", "Rebuild PDF/HTML in the output (folder mode)."),
    ("verify", "Verify", "Sweep the output for residual leaks."),
    (
        "auto_resolve",
        "Auto-resolve residuals",
        "Feedback loop: derive placeholders for residual leaks from the existing map and re-apply.",
    ),
)


_PAUSE_ORANGE = "#F2994A"
_SKIPPED_GREY = "#7A8390"


def _looks_skipped(message: str) -> bool:
    """Heuristic: a stage that returned ok=True but did effectively
    nothing should show up as 'skipped' (grey, partial bar) instead of
    'done' (full green bar). We detect this from the engine's status
    string, the only place that knows whether the stage was a no-op."""
    if not message:
        return False
    m = message.lower()
    if "skipped" in m:
        return True
    if "0 files" in m and "0 events" in m:
        return True
    if "+0 merged" in m:
        return True
    if "map: +0 new entries" in m:
        return True
    if "0 residual leaks in 0 files" in m:
        return True
    return False

_APPLY_LOCKED_TIP = (
    "Pipeline is paused after scan. Click 'Approve & continue' on the "
    "'Approve & promote' card first, or use Stop to cancel the queued run."
)


class _ActivityFeed(QFrame):
    """In-layout chronological event log for pipeline activity.

    Replaces the burst of 8-10 floating toasts a Run-all used to emit
    on the right edge (covering the residuals/build banners). Sits
    under the progress card; each pipeline-flavoured
    ``Toaster.notify`` call appends a row "<icon> <title> · <msg> ·
    <hh:mm:ss>". The newest row goes on top so the user reads the
    most relevant info first without scrolling.

    Rows persist until the user clears them or a new Run-all starts;
    nothing here fades out automatically.
    """

    # Plain ASCII / common Unicode glyphs so the feed renders the same
    # across QSS-styled and native widgets without pulling in an icon
    # font. The colour comes from the surrounding QSS palette.
    _GLYPHS: dict[str, str] = {
        "info": "ⓘ",
        "ok": "✓",
        "warn": "⚠",
        "err": "✕",
    }
    _COLOURS: dict[str, str] = {
        "info": "#58a6ff",
        "ok": "#3fb950",
        "warn": "#f5a623",
        "err": "#f85149",
    }
    MAX_VISIBLE_ROWS = 12

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        self._rows: list[QWidget] = []

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Pipeline activity")
        title.setObjectName("Muted")
        clear_btn = QPushButton("Clear")
        clear_btn.setFlat(True)
        clear_btn.setObjectName("CaptionButton")
        clear_btn.clicked.connect(self.clear)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(clear_btn)

        self._rows_host = QWidget()
        self._rows_lay = QVBoxLayout(self._rows_host)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(2)
        self._rows_lay.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ActivityFeedScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setWidget(self._rows_host)
        # Cap the scroll area so the feed never pushes everything else
        # below it off-screen; cap is approx 12 rows tall (~22px each
        # plus padding).
        self._scroll.setMaximumHeight(22 * self.MAX_VISIBLE_ROWS + 24)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        lay.addLayout(header)
        lay.addWidget(self._scroll)

        # Empty state: hide the whole frame until the first event
        # arrives so the Pipeline tab does not show an empty feed on
        # initial open.
        self.setVisible(False)

    def add(self, title: str, message: str, kind: str = "info") -> None:
        """Append a row to the top of the feed (newest first)."""
        glyph = self._GLYPHS.get(kind, "ⓘ")
        colour = self._COLOURS.get(kind, "#58a6ff")
        ts = datetime.now().strftime("%H:%M:%S")
        row = QFrame()
        row.setObjectName("ActivityFeedRow")
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 1, 0, 1)
        rh.setSpacing(8)
        icon_lbl = QLabel(glyph)
        icon_lbl.setFixedWidth(16)
        icon_lbl.setStyleSheet(f"color: {colour}; font-weight: 700;")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Compose the body inline so a long message wraps but the
        # title + timestamp stay on the first line.
        body_html = (
            f"<b>{title}</b>"
            + (f" &middot; {message}" if message else "")
        )
        body = QLabel(body_html)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        ts_lbl = QLabel(ts)
        ts_lbl.setObjectName("Caption")
        ts_lbl.setStyleSheet("color: #6c7280; font-size: 11px;")
        rh.addWidget(icon_lbl)
        rh.addWidget(body, 1)
        rh.addWidget(ts_lbl)
        # Insert at index 0 so the newest event is visible without
        # scrolling. The trailing addStretch() stays at the bottom of
        # the layout (index = count - 1).
        self._rows_lay.insertWidget(0, row)
        self._rows.insert(0, row)
        # Cap the list so memory does not grow unbounded on long runs.
        while len(self._rows) > 200:
            old = self._rows.pop()
            try:
                self._rows_lay.removeWidget(old)
                old.deleteLater()
            except Exception:
                pass
        self.setVisible(True)

    def clear(self) -> None:
        for row in list(self._rows):
            try:
                self._rows_lay.removeWidget(row)
                row.deleteLater()
            except Exception:
                pass
        self._rows.clear()
        self.setVisible(False)


class _StageStrip(QWidget):
    """Horizontal stepper showing per-stage state in real time.

    Replaces the legacy "Show details" deck. Visual language is
    deliberately flat, no nested borders, no arrow connectors, just a
    coloured indicator + stage label per step. The active step gets a
    soft accent tint so the eye lands on it; everything else stays
    quiet so the strip never competes with the progress bar below.

    States:

    * ``idle``     , empty hollow ring, dim label
    * ``running``  , accent ring with inner dot, accent tint, bold label
    * ``paused``   , warn ring + tint, bold label
    * ``done``     , green check, dim label
    * ``skipped``  , grey check (stage completed, but was a no-op), very dim label
    * ``error``    , red ✕ on red tint, bold label
    * ``cancelled``, dim dot, very dim label
    """

    # (glyph, fg colour, bg tint, border colour, label colour, label weight)
    # Note: ``skipped`` uses a grey check rather than an empty ring so the
    # operator sees visual closure on every completed stage. Colour still
    # distinguishes real work (green) from a no-op finish (grey).
    _STATES: dict[str, tuple[str, str, str, str, str, str]] = {
        "idle":      ("○", "#6c7280", "transparent",                  "transparent",                "#9aa0a6", "500"),
        "running":   ("●", "#5da4ec", "rgba(79,140,201,0.12)",         "rgba(79,140,201,0.45)",      "#e6e8eb", "600"),
        "paused":    ("◐", "#f5a623", "rgba(245,166,35,0.10)",         "rgba(245,166,35,0.45)",      "#e6e8eb", "600"),
        "done":      ("✓", "#3fb950", "transparent",                   "transparent",                "#9aa0a6", "500"),
        "skipped":   ("✓", "#7A8390", "transparent",                   "transparent",                "#6c7280", "500"),
        "error":     ("✕", "#f85149", "rgba(248,81,73,0.12)",          "rgba(248,81,73,0.45)",       "#e6e8eb", "600"),
        "cancelled": ("·", "#454c57", "transparent",                   "transparent",                "#6c7280", "500"),
    }

    def __init__(self, stages: tuple) -> None:
        super().__init__()
        self.setObjectName("StageStrip")
        self._chips: dict[str, dict] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for key, label, _ in stages:
            chip = self._make_chip(label)
            self._chips[key] = chip
            lay.addWidget(chip["frame"])
        lay.addStretch()
        for key in self._chips:
            self.set_state(key, "idle")

    def _make_chip(self, label: str) -> dict:
        frame = QFrame()
        frame.setObjectName("StageChip")
        h = QHBoxLayout(frame)
        h.setContentsMargins(10, 5, 12, 5)
        h.setSpacing(8)
        dot = QLabel()
        dot.setFixedWidth(14)
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(label)
        text.setObjectName("StageChipText")
        h.addWidget(dot)
        h.addWidget(text)
        return {"frame": frame, "dot": dot, "text": text}

    def set_state(self, key: str, state: str) -> None:
        if key not in self._chips:
            return
        glyph, fg, bg, border, label_color, weight = self._STATES.get(
            state, self._STATES["idle"]
        )
        chip = self._chips[key]
        chip["dot"].setText(glyph)
        chip["dot"].setStyleSheet(
            f"color: {fg}; font-size: 13px; font-weight: 700;"
        )
        chip["text"].setStyleSheet(
            f"color: {label_color}; font-weight: {weight}; font-size: 12px;"
        )
        chip["frame"].setStyleSheet(
            "QFrame#StageChip { "
            f"background: {bg}; border: 1px solid {border}; "
            "border-radius: 12px; "
            "}"
        )

    def reset_all(self) -> None:
        for key in self._chips:
            self.set_state(key, "idle")


class StageCard(QFrame):
    run_requested = Signal(str)
    stop_requested = Signal(str)
    approve_continue_requested = Signal(str)
    paused_state_changed = Signal()
    # Broadcast the new visual state ("idle" / "running" / "done" /
    # "skipped" / "error" / "cancelled" / "paused") so the new strip
    # widget can mirror per-stage progress without each card having
    # to be visible. Replaces the old "Show details" deck.
    state_changed = Signal(str)

    def __init__(
        self,
        key: str,
        title: str,
        subtitle: str,
        *,
        enable_approve_continue: bool = False,
    ) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.key = key
        self._running = False
        self._paused = False
        self._enable_approve_continue = enable_approve_continue
        self.title_label = QLabel(title)
        self.title_label.setObjectName("H2")
        self.sub_label = QLabel(subtitle)
        self.sub_label.setObjectName("Muted")
        self.sub_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setMinimumHeight(14)
        self.bar.setTextVisible(True)
        self.status = QLabel("idle")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(22)

        self.run_btn = QPushButton(icon("play"), " Run")
        self.run_btn.setObjectName("PrimaryButton")
        self.run_btn.clicked.connect(self._on_run_clicked)

        self.stop_btn = QPushButton(icon("stop"), " Stop")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.clicked.connect(lambda: self.stop_requested.emit(self.key))
        self.stop_btn.setVisible(False)

        self.approve_btn: Optional[QPushButton] = None
        if enable_approve_continue:
            self.approve_btn = QPushButton(icon("play"), " Approve & continue")
            self.approve_btn.setObjectName("PrimaryButton")
            self.approve_btn.clicked.connect(
                lambda: self.approve_continue_requested.emit(self.key)
            )
            self.approve_btn.setVisible(False)

        head = QHBoxLayout()
        head.addWidget(self.title_label)
        head.addStretch()
        if self.approve_btn is not None:
            head.addWidget(self.approve_btn)
        head.addWidget(self.run_btn)
        head.addWidget(self.stop_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addLayout(head)
        lay.addWidget(self.sub_label)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        # Reserve enough vertical room so the title / subtitle / bar /
        # status never overlap each other on stage cards (the screenshot
        # the user sent showed those four lines compressed onto each
        # other when the parent layout was tight).
        self.setMinimumHeight(118)

    def _on_run_clicked(self) -> None:
        self.run_requested.emit(self.key)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.run_btn.setVisible(not running and not self._paused)
        self.stop_btn.setVisible(running)
        if running:
            self._paused = False
            if self.approve_btn is not None:
                self.approve_btn.setVisible(False)
            self._reset_bar_color()
            self.state_changed.emit("running")

    def set_progress(self, done: int, total: int, label: str) -> None:
        if total > 0:
            self.bar.setValue(int(100 * done / total))
        self.status.setText(label or "running…")

    def set_finished(self, ok: bool, message: str) -> None:
        self.set_running(False)
        skipped = ok and _looks_skipped(message)
        if not ok:
            # Failure: keep whatever progress was made so the bar shows
            # how far the stage got before erroring out.
            pass
        elif skipped:
            # No-op stage (apply on 0 files, build on a non-md single
            # file, etc.). Use a dim grey bar so the user can tell at a
            # glance that this stage did nothing, distinct from a real
            # successful completion.
            self.bar.setValue(100)
            self._set_bar_color(_SKIPPED_GREY)
        else:
            self.bar.setValue(100)
            self._reset_bar_color()
        self.status.setText(message)
        if not ok:
            self.status.setObjectName("BadgeErr")
        elif skipped:
            self.status.setObjectName("BadgeWarn")
        else:
            self.status.setObjectName("BadgeOk")
        self.status.style().polish(self.status)
        if not ok:
            self.state_changed.emit("error")
        elif skipped:
            self.state_changed.emit("skipped")
        else:
            self.state_changed.emit("done")

    def set_cancelled(self) -> None:
        self.set_running(False)
        self.status.setText("cancelled")
        self.status.setObjectName("BadgeWarn")
        self.status.style().polish(self.status)
        self._paused = False
        if self.approve_btn is not None:
            self.approve_btn.setVisible(False)
        self._reset_bar_color()
        self.state_changed.emit("cancelled")

    def reset(self) -> None:
        self.set_running(False)
        self.bar.setValue(0)
        self.status.setText("idle")
        self.status.setObjectName("Muted")
        self.status.style().polish(self.status)
        self._paused = False
        if self.approve_btn is not None:
            self.approve_btn.setVisible(False)
        self._reset_bar_color()
        self.state_changed.emit("idle")

    # ---- paused state -------------------------------------------------------

    def set_paused_for_approval(
        self,
        *,
        primary: bool = True,
        message: str = "PAUSED, awaiting approval",
    ) -> None:
        if self.approve_btn is None:
            return
        self._paused = True
        self.run_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self.approve_btn.setVisible(True)
        self.approve_btn.setObjectName(
            "PrimaryButton" if primary else "SecondaryButton"
        )
        self.approve_btn.style().polish(self.approve_btn)
        self.bar.setValue(100)
        self._set_bar_color(_PAUSE_ORANGE)
        self.status.setText(message)
        self.status.setObjectName("BadgeWarn")
        self.status.style().polish(self.status)
        self.paused_state_changed.emit()
        self.state_changed.emit("paused")

    def reset_paused_state(self) -> None:
        if not self._paused:
            return
        self._paused = False
        if self.approve_btn is not None:
            self.approve_btn.setVisible(False)
        self.run_btn.setVisible(not self._running)
        self._reset_bar_color()
        self.paused_state_changed.emit()
        self.state_changed.emit("idle")

    def _set_bar_color(self, color: str) -> None:
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; }}"
        )

    def _reset_bar_color(self) -> None:
        self.bar.setStyleSheet("")


class PipelineView(QWidget):
    run_requested = Signal(str)
    run_all_requested = Signal()
    stop_all_requested = Signal()
    stop_stage_requested = Signal(str)
    approve_continue_requested = Signal(str)
    reset_run_state_requested = Signal()
    # Inline "View verifier report" / "Send all to Review" CTAs
    # rendered next to the summary bar when residuals > 0. Wired
    # from MainWindow so the Pipeline view doesn't need a direct
    # reference to the (now hidden) Verifier view.
    open_verifier_requested = Signal()
    send_residuals_to_review_requested = Signal()
    # Emitted when the user clicks the inline "Send to Review" button
    # that appears on the summary when post-pipeline residuals exist.
    # Replaces the old standalone Verifier tab, same behaviour
    # (route every residual hit into the Review queue), one fewer
    # sidebar entry.
    send_residuals_to_review_requested = Signal()
    view_residuals_requested = Signal()
    # Build-output CTAs surfaced after BuildWorker finishes: the
    # operator gets a green "Build complete" banner with one-click
    # access to the output folder and the verifier report. Without
    # these, a successful build was invisible in the UI (Build card
    # silently went green but no file paths were shown).
    open_build_folder_requested = Signal(str)
    view_build_report_requested = Signal()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.cards: dict[str, StageCard] = {}
        # When True, disables every stage **Run** (post-scan gate) so the user
        # cannot start Apply before promote completes.
        self._manual_run_locked = False

        # Chronological activity log: replaces the burst of floating
        # toasts a Run-all used to drop on the top-right. Wired to
        # Toaster.set_pipeline_sink at the end of __init__ so
        # ``Toaster.notify(pipeline_event=True)`` lands here.
        self.activity_feed = _ActivityFeed()

        title = QLabel("Pipeline")
        title.setObjectName("H1")
        sub = QLabel(
            "Press Run. The pipeline scans, asks for review when it "
            "needs your decision, and finishes the rest on its own."
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)

        run_all = QPushButton(icon("play"), "  Run")
        run_all.setObjectName("PrimaryButton")
        run_all.setMinimumHeight(56)
        run_all_font = run_all.font()
        run_all_font.setPointSize(max(12, run_all_font.pointSize() + 2))
        run_all_font.setBold(True)
        run_all.setFont(run_all_font)
        run_all.setToolTip(
            "Run the full pipeline (scan → approve & promote → apply → "
            "build → verify → auto-resolve). Pauses for Review only when "
            "the LLM is uncertain about a candidate."
        )
        run_all.clicked.connect(self.run_all_requested.emit)
        run_all.setEnabled(False)
        self.run_all_btn = run_all

        stop_all = QPushButton(icon("stop"), "  Stop")
        stop_all.setObjectName("DangerButton")
        stop_all.setMinimumHeight(56)
        stop_all.clicked.connect(self.stop_all_requested.emit)
        stop_all.setEnabled(False)
        self.stop_all_btn = stop_all

        # Detection mode picker — sits next to Run so the operator
        # sees / confirms the trade-off the instant before they fire
        # the pipeline. "Fast" runs the legacy single-prompt detector
        # (one LLM call per chunk covering all 12 categories);
        # "High accuracy" runs 11 focused per-category prompts and
        # merges the candidate lists (~5x slower on the 4B preset
        # but measurably higher F1, see ``Project.detector_mode``).
        # The preference is persisted in app_settings.yml so the
        # next launch starts on the same mode.
        self.detector_mode_lbl = QLabel("Detection mode")
        self.detector_mode_lbl.setObjectName("Caption")
        self.detector_mode_cb = QComboBox()
        self.detector_mode_cb.addItem(
            "Fast  ·  single pass (recommended)", "single"
        )
        self.detector_mode_cb.addItem(
            "High accuracy  ·  multi-pass (~5x slower)", "multipass"
        )
        self.detector_mode_cb.setToolTip(
            "Fast (single pass): one prompt with all 12 categories per "
            "chunk. Recommended for most reports, ~30 s per typical PDF "
            "on the 4B preset.\n\n"
            "High accuracy (multi-pass): 11 focused per-category prompts "
            "run against every chunk and the candidate lists are merged. "
            "Roughly 5x more detector time, but the local bench on the "
            "4B preset lifted F1 from 0.84 to 0.92 (precision +0.12). "
            "Recommended for messy or multi-customer reports."
        )
        try:
            saved = get_str("detector_mode", default="single")
            idx = self.detector_mode_cb.findData(saved)
            if idx >= 0:
                self.detector_mode_cb.setCurrentIndex(idx)
        except Exception:
            self.detector_mode_cb.setCurrentIndex(0)
        self.detector_mode_cb.currentIndexChanged.connect(
            lambda _i: set_str(
                "detector_mode",
                self.detector_mode_cb.currentData() or "single",
            )
        )
        self.detector_mode_cb.setMinimumWidth(260)
        self.detector_mode_cb.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        mode_col = QVBoxLayout()
        mode_col.setContentsMargins(0, 0, 0, 0)
        mode_col.setSpacing(2)
        mode_col.addWidget(self.detector_mode_lbl)
        mode_col.addWidget(self.detector_mode_cb)
        self._detector_mode_col = mode_col

        reset_btn = QPushButton(icon("refresh"), "  Reset run state")
        reset_btn.setMinimumHeight(40)
        reset_btn.setToolTip(
            "Delete previous run files (auto_promoted_*, needs_review.yml, "
            "applied_substitutions.json, decisions, verifier report) and "
            "re-detect every leak as if it were the first run.\n"
            "The global substitution_map.yml is NOT touched."
        )
        reset_btn.clicked.connect(self.reset_run_state_requested.emit)
        self.reset_btn = reset_btn

        # Compact summary shown by default in place of the per-stage
        # cards. The full deck is hidden behind a "Show details" toggle.
        self.summary_status = QLabel("Ready. Click Run to start.")
        self.summary_status.setObjectName("PipelineStatus")
        self.summary_status.setWordWrap(True)
        self.summary_status.setMinimumHeight(20)
        self.summary_bar = QProgressBar()
        self.summary_bar.setObjectName("PipelineProgress")
        self.summary_bar.setRange(0, 100)
        self.summary_bar.setValue(0)
        self.summary_bar.setTextVisible(True)
        self.summary_bar.setFormat("%p%")
        self.summary_bar.setMinimumHeight(22)

        # Visual strip showing per-stage state. Replaces the old
        # "Show details" deck, one glance tells the operator what
        # ran, what's running, what was skipped, what errored.
        self.stage_strip = _StageStrip(STAGES)

        # Inline residual block: lights up only when the verifier
        # reports leftover hits. Replaces the old standalone Verifier
        # tab in the sidebar.
        self.residuals_box = QFrame()
        self.residuals_box.setObjectName("Card")
        self.residuals_label = QLabel("")
        self.residuals_label.setWordWrap(True)
        self.residuals_label.setObjectName("BadgeWarn")
        self.residuals_send_btn = QPushButton("Send all to Review")
        self.residuals_send_btn.setObjectName("PrimaryButton")
        self.residuals_send_btn.clicked.connect(
            self.send_residuals_to_review_requested.emit
        )
        self.residuals_view_btn = QPushButton("View report")
        self.residuals_view_btn.setToolTip(
            "Open the full verifier report (file list + matched "
            "patterns). Useful for triaging which residuals to send "
            "to Review."
        )
        self.residuals_view_btn.clicked.connect(
            self.open_verifier_requested.emit
        )
        rb_lay = QHBoxLayout(self.residuals_box)
        rb_lay.setContentsMargins(12, 8, 12, 8)
        rb_lay.addWidget(self.residuals_label, 1)
        rb_lay.addWidget(self.residuals_send_btn)
        rb_lay.addWidget(self.residuals_view_btn)
        self.residuals_box.setVisible(False)

        # Build-complete banner. Shown after a successful Build stage
        # so the operator immediately sees where the redacted output
        # landed (path + one-click "Open folder"). The Run-all flow
        # used to advance to Verify / Auto-resolve right after Build,
        # leaving the user staring at an Auto-resolve progress bar
        # with no signal that the actual artefact had been written.
        self.build_box = QFrame()
        self.build_box.setObjectName("Card")
        self.build_label = QLabel("")
        self.build_label.setWordWrap(True)
        self.build_label.setObjectName("BadgeOK")
        # Render the rich-text fragments (``<b>`` / ``<code>`` / the
        # em-dash glyph) instead of showing the HTML source verbatim.
        # Without this the operator sees ``—`` in the banner
        # text — which is what a user reported.
        self.build_label.setTextFormat(Qt.TextFormat.RichText)
        self.build_open_btn = QPushButton("Open output folder")
        self.build_open_btn.setObjectName("PrimaryButton")
        self.build_open_btn.setToolTip(
            "Reveal the redacted output in the file manager."
        )
        self.build_view_btn = QPushButton("View report")
        self.build_view_btn.setToolTip(
            "Open the verifier report (Markdown) for this run."
        )
        self._build_open_path: str = ""
        self.build_open_btn.clicked.connect(
            lambda: self._build_open_path
            and self.open_build_folder_requested.emit(self._build_open_path)
        )
        self.build_view_btn.clicked.connect(
            self.view_build_report_requested.emit
        )
        bb_lay = QHBoxLayout(self.build_box)
        bb_lay.setContentsMargins(12, 8, 12, 8)
        bb_lay.addWidget(self.build_label, 1)
        bb_lay.addWidget(self.build_open_btn)
        bb_lay.addWidget(self.build_view_btn)
        self.build_box.setVisible(False)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addLayout(mode_col)
        head.addWidget(run_all, 1)
        head.addWidget(stop_all)

        # Group the live-progress widgets (stage strip + status text +
        # progress bar) in a single Card so they sit on the theme's
        # ``surface`` colour instead of looking abandoned on the page
        # background.  Without the wrapper the QProgressBar's trough
        # rendered nearly black on the bare page bg.
        progress_card = QFrame()
        progress_card.setObjectName("PipelineProgressCard")
        pc_lay = QVBoxLayout(progress_card)
        pc_lay.setContentsMargins(18, 14, 18, 16)
        pc_lay.setSpacing(12)
        pc_lay.addWidget(self.stage_strip)
        # A 1-px hairline separates the stepper from the status block
        #, visual cue that the strip is "navigation" and what's below
        # is "current step detail".
        sep = QFrame()
        sep.setObjectName("PipelineDivider")
        sep.setFrameShape(QFrame.Shape.NoFrame)
        sep.setFixedHeight(1)
        pc_lay.addWidget(sep)
        pc_lay.addWidget(self.summary_status)
        pc_lay.addWidget(self.summary_bar)
        self.progress_card = progress_card

        # Secondary toolbar (minor actions).
        tools = QHBoxLayout()
        tools.addStretch()
        tools.addWidget(reset_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addLayout(head)
        lay.addWidget(progress_card)
        lay.addWidget(self.build_box)
        lay.addWidget(self.residuals_box)
        lay.addWidget(self.activity_feed)
        lay.addLayout(tools)
        lay.addStretch()

        # The per-stage cards are kept alive as logical state holders
        # (other parts of the app drive them via ``card(key)``) but
        # they are not added to the layout, the new strip mirrors
        # their state visually so the user never has to expand a
        # second-level panel.
        for key, t, s in STAGES:
            card = StageCard(
                key, t, s, enable_approve_continue=(key == "promote")
            )
            card.setParent(self)
            card.run_requested.connect(self.run_requested.emit)
            card.stop_requested.connect(self.stop_stage_requested.emit)
            card.approve_continue_requested.connect(
                self.approve_continue_requested.emit
            )
            card.paused_state_changed.connect(self._sync_run_button_state)
            card.state_changed.connect(
                lambda s, k=key: self.stage_strip.set_state(k, s)
            )
            card.setVisible(False)
            self.cards[key] = card

        state.busy_changed.connect(self._on_busy)
        state.project_changed.connect(lambda _p: self._sync_run_button_state())
        state.server_status_changed.connect(
            lambda _ok, _msg: self._sync_run_button_state()
        )
        # Route pipeline-flavoured toast calls into the in-layout feed
        # instead of top-right floaters. The Toaster singleton is
        # already attached by MainWindow before this view is built.
        try:
            Toaster.set_pipeline_sink(self.activity_feed.add)
        except Exception:
            pass
        self._sync_run_button_state()

    def set_locked(self, locked: bool) -> None:
        """Block manual **Run** on all stages (used during post-scan pause)."""
        self._manual_run_locked = locked
        self._sync_run_button_state()

    def _on_busy(self, busy: bool, label: str) -> None:
        self._sync_run_button_state()
        # Mirror the engine's busy label into the simple-mode summary.
        if busy:
            human = label or "running…"
            self.summary_status.setText(f"Working: {human}")
        # When busy clears, leave the last status message visible, the
        # _on_stage_finished hook in MainWindow updates it via
        # ``set_summary``.

    def set_build_artifacts(self, report) -> None:
        """Show the green "Build complete" banner with artefact count
        + output folder, or hide it when ``report`` is ``None`` (e.g.
        before the first build, or after the project changes).

        ``report`` is the ``BuildReport`` produced by
        :func:`anonymize.pipeline.stage_build` and carries an
        ``artefacts: list[Path]`` field.
        """
        if report is None:
            self.build_box.setVisible(False)
            self._build_open_path = ""
            return
        artefacts = list(getattr(report, "artefacts", []) or [])
        warnings = list(getattr(report, "warnings", []) or [])
        warn_suffix = (
            f" · <span style='color:#c98a00'>{len(warnings)} warning(s)</span>"
            if warnings
            else ""
        )
        if not artefacts:
            # Build completed but emitted zero artefacts (e.g. all
            # substitutions applied in-place, no PDF rebuild needed).
            # Still surface the banner so the operator gets explicit
            # closure — hiding it leaves them staring at the Verify /
            # Auto-resolve activity wondering whether Build ran at all.
            self._build_open_path = ""
            self.build_label.setText(
                "✓ Build complete — substitutions applied "
                "(no new PDFs to write)" + warn_suffix
            )
            self.build_open_btn.setEnabled(False)
            self.build_open_btn.setVisible(False)
            self.build_box.setVisible(True)
            return
        # Resolve a common parent so the Open-folder button targets
        # one location even when the build dropped multiple PDFs in
        # subdirectories (folder mode).
        from os.path import commonpath
        try:
            first = artefacts[0]
            paths = [str(p) for p in artefacts]
            parent = commonpath(paths) if len(paths) > 1 else str(first.parent)
        except Exception:
            parent = str(artefacts[0].parent) if hasattr(artefacts[0], "parent") else ""
        self._build_open_path = parent
        count = len(artefacts)
        noun = "PDF" if count == 1 else "PDFs"
        self.build_label.setText(
            f"✓ Build complete — <b>{count}</b> {noun} written to "
            f"<code>{parent}</code>{warn_suffix}"
        )
        self.build_open_btn.setEnabled(bool(parent))
        self.build_open_btn.setVisible(True)
        self.build_box.setVisible(True)

    def set_residuals(self, count: int) -> None:
        """Show / hide the inline residuals row + buttons. ``count`` is
        the number of residual hits the verifier reported on the most
        recent run; 0 hides the row entirely."""
        if count <= 0:
            self.residuals_box.setVisible(False)
            return
        self.residuals_label.setText(
            f"⚠ Verifier found {count} residual leak(s) after the "
            f"pipeline. They are real values that slipped past the "
            f"detector, send them to Review or open the report to "
            f"triage."
        )
        self.residuals_box.setVisible(True)

    def set_summary(self, message: str, *, percent: Optional[int] = None) -> None:
        """Update the simple-mode summary line and progress bar.

        ``MainWindow._on_stage_finished`` calls this after every stage
        so the simple view reflects the pipeline's progress without
        requiring the user to expand the details panel.
        """
        if message:
            self.summary_status.setText(message)
        if percent is not None:
            self.summary_bar.setValue(max(0, min(100, int(percent))))

    def _sync_run_button_state(self) -> None:
        """Enable **Run** only when:
        * a project is open,
        * the pipeline is not already busy / globally locked,
        * llama-server is online (no point launching otherwise),
        * the active profile's slot budget can fit a single chunk.
        """
        from anonymize.budget import check_slot_budget

        busy = self.state.busy
        has_project = self.state.project is not None
        any_paused = any(c._paused for c in self.cards.values())
        server_online = bool(getattr(self.state, "server_online", False))
        prof = getattr(self.state, "profile", None)
        budget_ok = True
        budget_reason = ""
        if prof is not None:
            est = check_slot_budget(
                ctx_size=int(prof.ctx_size),
                parallel=int(prof.parallel),
            )
            budget_ok = est.fits
            budget_reason = est.reason or est.explain()
        self._budget_reason = budget_reason
        self._budget_ok = budget_ok

        runnable = (
            has_project
            and not busy
            and not any_paused
            and server_online
            and budget_ok
        )

        # Big "Run" / "Stop" buttons in the header.
        self.run_all_btn.setEnabled(runnable)
        self.stop_all_btn.setEnabled(busy)
        if not has_project:
            self.run_all_btn.setToolTip("Open a project first.")
        elif any_paused:
            self.run_all_btn.setToolTip(_APPLY_LOCKED_TIP)
        elif busy:
            self.run_all_btn.setToolTip("Pipeline is already running.")
        elif not server_online:
            self.run_all_btn.setToolTip(
                "llama-server is offline. Start it from the Server view "
                "(or wait for the auto-restart) before clicking Run."
            )
        elif not budget_ok:
            self.run_all_btn.setToolTip(
                "Token budget too tight for the active preset:\n\n"
                + budget_reason
            )
        else:
            self.run_all_btn.setToolTip(
                "Run the full pipeline (scan → approve & promote → apply "
                "→ build → verify → auto-resolve). Pauses for Review only "
                "when the LLM is uncertain about a candidate."
            )

        for key, c in self.cards.items():
            blocked = (
                busy
                or c._paused
                or self._manual_run_locked
                or not has_project
                or not server_online
                or not budget_ok
            )
            c.run_btn.setEnabled(not blocked)
            if key == "apply":
                c.run_btn.setToolTip(
                    _APPLY_LOCKED_TIP
                    if self._manual_run_locked and blocked
                    else ""
                )
        self.reset_btn.setEnabled(has_project and not busy)

    def card(self, key: str) -> Optional[StageCard]:
        return self.cards.get(key)

    def reset_all_paused(self) -> None:
        for c in self.cards.values():
            c.reset_paused_state()

    def refresh_lock_sync(self) -> None:
        """Call after ``AppState.set_busy`` if the busy flag is toggled without
        emitting ``busy_changed`` in the same tick (tests / edge cases)."""
        self._sync_run_button_state()


__all__ = ["PipelineView", "StageCard"]
