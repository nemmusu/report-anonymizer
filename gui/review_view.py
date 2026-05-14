"""Review view: tree-table of needs_review candidates with bulk actions.

Rows are organised by category (parent) -> cluster (canonical value, child).
Operator workflow:

* select rows / categories,
* hit Y / N / M (or click the bottom action bar) to approve / reject /
  edit-placeholder,
* press "Promote approved" to merge the approved rows into the canonical
  substitution_map.yml.

The view exposes ``promote_requested(approved_candidates)`` which the
MainWindow connects to a :class:`PromoteWorker`.
"""
from __future__ import annotations

from typing import Iterable

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QAction, QShortcut, QKeySequence, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import hashlib
import json
import shutil
import tempfile
from anonymize.candidates import Candidate
from anonymize.format_adapters import get_adapter
from anonymize.format_adapters.base import SubstitutionRule

from ._render_panes import (
    HtmlRenderPane,
    MarkdownRenderPane,
    OfficeRenderPane,
    PdfRenderPane,
    PlainTextRenderPane,
    SelectableOfficeRenderPane,
    SelectablePdfRenderPane,
    SpreadsheetRenderPane,
    pick_pane_for,
    pick_selectable_pane_for,
)
from ._dismissible_dialog import dismissible_message, make_dismissible
from .state import AppState
from .icons import icon
from .build_preview_panel import BuildPreviewPanel
from .image_review_panel import ImageReviewPanel
from .theme import PALETTE


_CAT_ORDER = (
    "brand",
    "network",
    "app_packages",
    "phones",
    "emails",
    "keys",
    "credentials",
    "headers",
    "user_agents",
    "ids",
    "infra_ids",
    "other",
)


_DECISION_PENDING = "pending"
_DECISION_APPROVE = "approve"
_DECISION_SKIP = "skip"


class _CandItem(QTreeWidgetItem):
    """A child row representing a single Candidate cluster."""

    def __init__(self, cand: Candidate) -> None:
        super().__init__()
        self.cand = cand
        # Decision is mirrored on the underlying ``Candidate`` so it
        # round-trips through ``needs_review.yml`` and survives a
        # GUI restart for the same project.
        self.decision = getattr(cand, "decision", None) or _DECISION_PENDING
        self._refresh()
        self.setFlags(
            self.flags()
            | Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsUserCheckable
        )

    def _refresh(self) -> None:
        c = self.cand
        self.setText(0, "")  # decision column
        self.setText(1, c.value)
        self.setText(2, c.suggested_placeholder)
        self.setText(3, str(c.count))
        self.setText(4, f"{c.confidence:.2f}")
        self.setText(5, f"{c.critic_confidence:.2f}")
        self.setText(6, c.critic_is_real_leak)
        self.setText(7, c.rationale)
        self.setText(8, ", ".join(c.examples[:2]))
        self._refresh_color()

    def _refresh_color(self) -> None:
        # Approved → green ("included in the final anonymisation map").
        # Skip / pending → original colour (not in the map but still
        # visible in the queue). The bug fix here: the previous build
        # rendered ``skip`` rows in the error/red palette, which blurred
        # the "deselected → not included → original colour" semantics
        # the operator expects after pressing Un-approve.
        if self.decision == _DECISION_APPROVE:
            color = QColor(PALETTE["ok"])
        else:
            color = QColor(PALETTE["text"])
        for col in range(self.columnCount()):
            self.setForeground(col, color)
        marker = "✓" if self.decision == _DECISION_APPROVE else (
            "✗" if self.decision == _DECISION_SKIP else "·"
        )
        self.setText(0, marker)


class _AutoItem(QTreeWidgetItem):
    """A child row for a candidate the system has already auto-approved
    (Tier-0 deterministic rule or Tier-1 high-confidence LLM).

    Displayed with the same ✓ "approved" styling as :class:`_MapItem`
    so the operator sees what's queued for the next Promote at a
    glance, with the option to edit the placeholder inline or demote
    it back to pending review.
    """

    def __init__(self, cand: Candidate, tier: str) -> None:
        super().__init__()
        self.cand = cand
        self.tier = tier  # "T0" or "T1"
        self._refresh()
        self.setFlags(self.flags() | Qt.ItemFlag.ItemIsEditable)

    def _refresh(self) -> None:
        c = self.cand
        self.setText(0, "✓")
        self.setText(1, c.value)
        self.setText(2, c.suggested_placeholder)
        self.setText(3, str(c.count))
        self.setText(4, f"{c.confidence:.2f}")
        self.setText(5, f"{c.critic_confidence:.2f}")
        self.setText(6, f"auto {self.tier}")
        self.setText(7, c.rationale or "")
        self.setText(8, ", ".join(c.examples[:2]))
        self.setToolTip(
            0,
            f"Auto-approved by {self.tier}, will be merged into "
            "substitution_map.yml at the next Promote. Edit the "
            "placeholder column inline, or click 'Demote to pending' "
            "to send it back for manual review.",
        )
        ok = QColor(PALETTE.get("ok", "#27AE60"))
        for col in range(self.columnCount()):
            self.setForeground(col, ok)


class _MapItem(QTreeWidgetItem):
    """A child row representing an existing entry already promoted into
    the substitution_map. Visually marked as "approved" so the operator
    sees what's locked-in alongside what still needs a decision.
    """

    def __init__(self, category: str, entry: dict) -> None:
        super().__init__()
        self.category = category
        self.entry = entry  # {"from": ..., "to": ..., "id": ...}
        self._refresh()
        # Editable so the placeholder column can be edited inline.
        self.setFlags(self.flags() | Qt.ItemFlag.ItemIsEditable)

    def _refresh(self) -> None:
        self.setText(0, "✓")
        self.setText(1, str(self.entry.get("from", "")))
        self.setText(2, str(self.entry.get("to", "")))
        self.setText(3, "")
        self.setText(4, "")
        self.setText(5, "")
        self.setText(6, "in map")
        self.setText(7, f"id: {self.entry.get('id', '')}")
        self.setText(8, "")
        self.setToolTip(
            0,
            "Already in substitution_map.yml, edit the placeholder "
            "column inline to update it, or click 'Remove from map'.",
        )
        ok = QColor(PALETTE.get("ok", "#27AE60"))
        for col in range(self.columnCount()):
            self.setForeground(col, ok)


class _AddToMapDialog(QDialog):
    """Modal: add a brand-new ``(value, category, placeholder)`` row to
    the substitution map without going through the candidate pipeline.
    """

    def __init__(
        self,
        *,
        categories: tuple[str, ...],
        default_category: str = "other",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to substitution map")
        self.setModal(True)

        self.value = QLineEdit()
        self.value.setPlaceholderText("e.g. Acme Corporation")
        self.cat = QComboBox()
        for c in categories:
            self.cat.addItem(c, c)
        idx = self.cat.findData(default_category)
        if idx >= 0:
            self.cat.setCurrentIndex(idx)
        self.placeholder = QLineEdit()
        self.placeholder.setPlaceholderText("e.g. ACME-001")

        form = QFormLayout()
        form.addRow("Value (from):", self.value)
        form.addRow("Category:", self.cat)
        form.addRow("Placeholder (to):", self.placeholder)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

    def values(self) -> tuple[str, str, str]:
        return (
            self.value.text().strip(),
            self.cat.currentData() or "other",
            self.placeholder.text().strip(),
        )


class ReviewView(QWidget):
    promote_requested = Signal(list)  # list[Candidate]
    # Emitted when the operator clicks "Save and continue to Apply"
    # in the Images tab. The MainWindow surfaces the Build-preview
    # tab so the operator can confirm the final look before the
    # actual apply / build / verify runs (the user-facing gate).
    image_save_continue_requested = Signal()
    # Emitted from the Build-preview tab when the operator clicks
    # Build. The MainWindow turns this into a real apply / build /
    # verify queue (the same one that used to run automatically
    # after Promote).
    build_requested = Signal()

    HEADERS = (
        "",
        "Value",
        "Placeholder",
        "Count",
        "Conf det",
        "Conf crit",
        "Verdict",
        "Rationale",
        "Examples",
    )

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state

        # ---- summary banner ------------------------------------------------
        # Counters update every time AppState.candidates_changed fires so the
        # operator can see at a glance how many items were auto-promoted by
        # the deterministic rules (T0), how many by the LLM with high
        # confidence (T1 auto), and how many still need a human decision.
        self.summary = QLabel("")
        self.summary.setObjectName("ReviewSummary")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)

        # ---- toolbar (search/filter) ---------------------------------------
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search value, placeholder, rationale…")
        self.search.textChanged.connect(self._apply_filter)

        self.cat_combo = QComboBox()
        self.cat_combo.addItem("All categories", "")
        for c in _CAT_ORDER:
            self.cat_combo.addItem(c, c)
        self.cat_combo.currentIndexChanged.connect(self._apply_filter)

        self.only_disagree = QPushButton("Only critic-uncertain")
        self.only_disagree.setCheckable(True)
        self.only_disagree.toggled.connect(self._apply_filter)

        top = QHBoxLayout()
        top.addWidget(QLabel("Search:"))
        top.addWidget(self.search, 2)
        top.addWidget(QLabel("Category:"))
        top.addWidget(self.cat_combo, 1)
        top.addWidget(self.only_disagree)

        # ---- tree ----------------------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.HEADERS))
        self.tree.setHeaderLabels(self.HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        # The inline edit field inherits the row's foreground colour
        # by default, so editing a green ``in map`` row produced
        # green-on-dark-grey text that was nearly invisible. Pin the
        # editor's palette to the canonical input colours instead.
        self.tree.setStyleSheet(
            "QTreeWidget QLineEdit {"
            f" color: {PALETTE['text']};"
            f" background: {PALETTE['bg_input']};"
            f" selection-background-color: {PALETTE['accent_dim']};"
            f" border: 1px solid {PALETTE['accent']};"
            " padding: 2px 4px;"
            "}"
        )
        # Right-click context menu so the operator can act on the row
        # under the cursor without going to the bottom action bar.
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        h = self.tree.header()
        for i, w in enumerate((28, 280, 220, 60, 80, 80, 90, 320, 240)):
            self.tree.setColumnWidth(i, w)
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # ---- bottom actions ------------------------------------------------
        self.lbl_count = QLabel("0 candidates")
        self.lbl_count.setObjectName("Muted")

        btn_y = QPushButton("Approve (Y)")
        btn_y.setObjectName("PrimaryButton")
        btn_n = QPushButton("Skip (N)")
        btn_m = QPushButton("Edit placeholder (M)")
        btn_cat_y = QPushButton("Approve category")
        btn_promote = QPushButton("Promote & build")
        btn_promote.setObjectName("PrimaryButton")
        btn_promote.setToolTip(
            "Merge every ✓ approved candidate into substitution_map.yml "
            "and immediately re-run Apply → Build → Verify so the "
            "redacted output reflects your decisions. The Pipeline "
            "view auto-switches to track progress."
        )

        btn_add_map = QPushButton("Add to map…")
        btn_add_map.setToolTip(
            "Add a brand-new (value, placeholder) pair to "
            "substitution_map.yml without going through the candidate "
            "pipeline. Useful when you already know what to anonymize."
        )
        btn_delete = QPushButton("Delete")
        btn_delete.setObjectName("DangerButton")
        btn_delete.setToolTip(
            "Hard-remove the selected rows from every storage layer "
            "(pending queue, auto-promoted YAMLs, substitution_map). "
            "Use Un-approve instead if you want to demote auto rows "
            "back to pending review."
        )
        btn_delete.clicked.connect(self._delete_selected)

        btn_unapprove = QPushButton("Un-approve")
        btn_unapprove.setObjectName("DangerButton")
        btn_unapprove.setToolTip(
            "Drop the approval from the selected ✓ rows.\n"
            "  • In-map rows are removed from substitution_map.yml.\n"
            "  • Auto-approved (T0/T1) rows are demoted back to the "
            "pending review queue.\n"
            "Documents already anonymised stay as they are."
        )

        btn_y.clicked.connect(lambda: self._set_decision(_DECISION_APPROVE))
        btn_n.clicked.connect(lambda: self._set_decision(_DECISION_SKIP))
        btn_m.clicked.connect(self._edit_placeholder)
        btn_cat_y.clicked.connect(self._approve_selected_category)
        btn_promote.clicked.connect(self._promote_clicked)
        btn_add_map.clicked.connect(self._add_to_map_clicked)
        btn_unapprove.clicked.connect(self._unapprove_selected)

        bottom = QHBoxLayout()
        bottom.addWidget(self.lbl_count)
        bottom.addStretch()
        for b in (btn_y, btn_n, btn_m, btn_cat_y, btn_promote):
            bottom.addWidget(b)
        # Small visual gap between candidate-decision actions and
        # map-maintenance actions so the two groups aren't conflated.
        sep = QLabel("│")
        sep.setObjectName("Muted")
        bottom.addSpacing(8)
        bottom.addWidget(sep)
        bottom.addSpacing(4)
        bottom.addWidget(btn_add_map)
        bottom.addWidget(btn_unapprove)
        bottom.addWidget(btn_delete)

        # Shortcuts
        QShortcut(QKeySequence("Y"), self, lambda: self._set_decision(_DECISION_APPROVE))
        QShortcut(QKeySequence("N"), self, lambda: self._set_decision(_DECISION_SKIP))
        QShortcut(QKeySequence("M"), self, self._edit_placeholder)
        # Del / Backspace = hard delete on the candidate tree. Scoped
        # to the tree widget so the shortcut doesn't fire from other
        # focusable widgets in the view.
        del_shortcut = QShortcut(QKeySequence("Delete"), self.tree)
        del_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        del_shortcut.activated.connect(self._delete_selected)

        # ---- live preview pane (right side) -----------------------------
        # Renders the current input document with the user's pending
        # decisions visualised as highlights (green = approved,
        # orange = pending). A toggle keeps the preview off by default
        # for performance; turning it on populates a per-format render
        # pane next to the candidate tree.
        self.preview_toggle = QPushButton("Show preview")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setChecked(False)
        self.preview_toggle.setToolTip(
            "Render the current input document side-by-side with the "
            "candidate list. Approved candidates appear as green "
            "highlights; pending ones as orange. The original file is "
            "NOT modified, this is a live preview of what Promote → "
            "Apply would produce."
        )
        self.preview_toggle.toggled.connect(self._on_preview_toggled)
        self.preview_file_combo = QComboBox()
        self.preview_file_combo.setVisible(False)
        self.preview_file_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_preview()
        )
        zoom_icon_size = QSize(16, 16)
        self.preview_zoom_out = QPushButton(icon("zoom-out"), "")
        self.preview_zoom_out.setIconSize(zoom_icon_size)
        self.preview_zoom_out.setFixedSize(32, 28)
        self.preview_zoom_out.setToolTip("Zoom out (Ctrl+wheel also works)")
        self.preview_zoom_out.setVisible(False)
        self.preview_zoom_in = QPushButton(icon("zoom-in"), "")
        self.preview_zoom_in.setIconSize(zoom_icon_size)
        self.preview_zoom_in.setFixedSize(32, 28)
        self.preview_zoom_in.setToolTip("Zoom in (Ctrl+wheel also works)")
        self.preview_zoom_in.setVisible(False)
        self.preview_zoom_fit = QPushButton(icon("maximize"), " Fit")
        self.preview_zoom_fit.setIconSize(zoom_icon_size)
        self.preview_zoom_fit.setToolTip("Fit page to window width")
        self.preview_zoom_fit.setVisible(False)
        self.preview_zoom_100 = QPushButton("100%")
        self.preview_zoom_100.setToolTip("Reset zoom to 100%")
        self.preview_zoom_100.setVisible(False)
        self.preview_zoom_out.clicked.connect(lambda: self._zoom_preview("out"))
        self.preview_zoom_in.clicked.connect(lambda: self._zoom_preview("in"))
        self.preview_zoom_fit.clicked.connect(lambda: self._zoom_preview("fit"))
        self.preview_zoom_100.clicked.connect(lambda: self._zoom_preview("reset"))
        top.addStretch()
        top.addWidget(self.preview_toggle)
        top.addWidget(self.preview_file_combo)
        top.addWidget(self.preview_zoom_out)
        top.addWidget(self.preview_zoom_in)
        top.addWidget(self.preview_zoom_fit)
        top.addWidget(self.preview_zoom_100)

        self._preview_panes: dict[type, QWidget] = {}
        self.preview_stack = QStackedWidget()
        self.preview_stack.setVisible(False)
        # User actions (approve / skip / edit / category-approve) burst
        # in tight succession; coalesce them into a single re-apply so
        # the preview pane doesn't fight the operator's keyboard.
        self._preview_refresh_timer = QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.setInterval(180)
        self._preview_refresh_timer.timeout.connect(self._refresh_preview)

        body = QSplitter(Qt.Orientation.Horizontal)
        left_box = QWidget()
        left_lay = QVBoxLayout(left_box)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)
        left_lay.addWidget(self.tree, 1)
        body.addWidget(left_box)
        body.addWidget(self.preview_stack)
        body.setSizes([800, 0])
        body.setHandleWidth(6)
        self._body_splitter = body

        # Wrap the existing review surface in a child widget so the
        # Review tab can host TWO panels side by side: the existing
        # text-candidate UI here, plus the new image-redaction
        # gallery (added below). The text panel keeps its layout
        # exactly as it was before the image work landed; nothing
        # about the keyboard shortcuts, selection model, or signal
        # wiring changes.
        self._text_panel = QWidget()
        text_root = QVBoxLayout(self._text_panel)
        text_root.setContentsMargins(12, 8, 12, 8)
        text_root.setSpacing(8)
        text_root.addWidget(self.summary)
        text_root.addLayout(top)
        text_root.addWidget(body, 1)
        text_root.addLayout(bottom)

        # The Image tab is stood up disabled. The MainWindow flips
        # ``setEnabled(True)`` and calls ``image_panel.set_paths()``
        # the first time a Promote completes; from then on the tab
        # is interactive whenever the project has any embedded
        # images to review.
        self.image_panel = ImageReviewPanel()
        self.image_panel.setEnabled(False)
        self.image_panel.save_and_continue_requested.connect(
            self._on_image_save_requested
        )

        # Build-preview tab. Stays disabled until the operator
        # finishes the image review (or skips it when there are no
        # images). The Back buttons inside it route via the existing
        # tab widget so navigation feels native.
        self.build_panel = BuildPreviewPanel(self.state)
        self.build_panel.setEnabled(False)
        self.build_panel.build_requested.connect(self._on_build_requested)
        self.build_panel.back_to_text_requested.connect(self._focus_text_tab)
        self.build_panel.back_to_images_requested.connect(self.focus_image_tab)

        # Live-refresh wiring: any change that influences the rendered
        # output (text rules, in-memory image redactions) re-renders
        # the Build-preview panel through its debounce timer so the
        # operator can leave it open and watch the preview update as
        # they edit.
        self.build_panel.set_live_decisions_provider(
            self.image_panel.current_decisions
        )
        self.image_panel.decisions_changed.connect(
            self.build_panel.schedule_refresh
        )
        self.state.candidates_changed.connect(
            self.build_panel.schedule_refresh
        )
        self.state.map_changed.connect(
            lambda _m: self.build_panel.schedule_refresh()
        )

        # The wrapper QTabWidget IS the Review pane root; both panels
        # share the same outer geometry / margins / padding the
        # parent layout (in MainWindow) gives us.
        self.tabs = QTabWidget()
        self.tabs.addTab(self._text_panel, "Text candidates")
        self.tabs.addTab(self.image_panel, "Images")
        self.tabs.addTab(self.build_panel, "Preview of build")
        # Disabled state on the tab itself shows it as greyed out,
        # the tooltip nudges the operator toward the right action.
        self.tabs.setTabToolTip(
            1,
            "Promote the text decisions first. The image review "
            "becomes available once Promote has run at least once.",
        )
        self.tabs.setTabToolTip(
            2,
            "Final look before Build. Available once you finish the "
            "image review (or click Save and continue with no images "
            "to redact).",
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.tabs)

        self.state.candidates_changed.connect(self.refresh_from_state)
        self.state.candidates_changed.connect(self._schedule_preview_refresh)
        # Map mutations (promote, inline edit, add, remove) re-render
        # the tree so the ✓ rows always reflect the on-disk YAML.
        self.state.map_changed.connect(lambda _m: self.refresh_from_state())
        self.state.map_changed.connect(lambda _m: self._schedule_preview_refresh())
        self.state.project_changed.connect(self._on_project_changed)
        self.refresh_from_state()
        self._on_project_changed(self.state.project)

    # ---- image review tab plumbing -----------------------------------------

    def enable_image_tab(self) -> None:
        """Light up the Images tab. Idempotent: safe to call after
        every promote, the panel only re-reads the YAMLs.

        The Build-preview tab is enabled at the same time so the
        operator can flip between Images and Preview while editing
        and watch the redactions land in real time, even before
        clicking Save. The preview reads the in-memory image
        decisions through the live provider hook, so there is no
        on-disk dependency.
        """
        proj = self.state.project
        if proj is None:
            return
        self.image_panel.setEnabled(True)
        try:
            self.tabs.setTabToolTip(1, "Review embedded images.")
        except Exception:
            pass
        self.image_panel.set_paths(
            Path(proj.image_inventory_path),
            Path(proj.image_redactions_path),
        )
        self.enable_build_tab()

    def focus_image_tab(self) -> None:
        """Switch to the Images tab. Used by MainWindow after a
        promote completes so the operator's eye lands on the next
        thing to do.
        """
        try:
            self.tabs.setCurrentWidget(self.image_panel)
        except Exception:
            pass

    def enable_build_tab(self) -> None:
        """Light up the Build-preview tab. Idempotent."""
        self.build_panel.setEnabled(True)
        try:
            self.tabs.setTabToolTip(
                2,
                "Final look before Build. Click Build to commit the "
                "redacted output to disk, or use the Back buttons to "
                "tweak text rules / image rects.",
            )
        except Exception:
            pass
        # Force a fresh render so the operator does not have to click
        # Refresh manually. The panel coalesces redundant rebuilds via
        # the on-disk cache, so calling this multiple times is cheap.
        try:
            self.build_panel.refresh()
        except Exception:
            pass

    def focus_build_tab(self) -> None:
        """Switch to the Build-preview tab."""
        try:
            self.tabs.setCurrentWidget(self.build_panel)
        except Exception:
            pass

    def focus_text_tab(self) -> None:
        """Switch the inner tab strip to the "Text candidates" panel.

        Public counterpart to the historical private ``_focus_text_tab``;
        callers outside the class (e.g. ``MainWindow._on_hits_to_pending``
        after a "Send all to Review" click) should land on this tab so
        the operator sees the rows that were just enqueued instead of
        whichever sub-tab was active before (often "Preview of build").
        """
        try:
            self.tabs.setCurrentWidget(self._text_panel)
        except Exception:
            pass

    # Backwards-compatible alias: existing internal callers used the
    # private name. Keep both pointing at the same method so nothing in
    # the rest of the module breaks while exposing the public surface.
    _focus_text_tab = focus_text_tab

    def focus_first_leak(self) -> bool:
        """Switch to the Text candidates sub-tab and select the first
        un-handled leak (pending row whose decision is not ``approve``).

        Returns ``True`` when a leak was found and selected, ``False``
        otherwise. Used by the build-time leak warning so the operator
        lands directly on the row that needs attention after clicking
        "Back to review".
        """
        try:
            self.tabs.setCurrentWidget(self._text_panel)
        except Exception:
            pass
        for top_idx in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(top_idx)
            for child_idx in range(top.childCount()):
                ch = top.child(child_idx)
                if (
                    isinstance(ch, _CandItem)
                    and ch.decision != _DECISION_APPROVE
                    and not ch.isHidden()
                ):
                    try:
                        top.setExpanded(True)
                        self.tree.setCurrentItem(ch)
                        self.tree.scrollToItem(ch)
                    except Exception:
                        pass
                    return True
        return False

    def _on_image_save_requested(self) -> None:
        # The panel has already persisted to disk via save_to_disk();
        # all we need to do here is bubble the "review the build
        # preview now" intent up to MainWindow, which routes the
        # operator into the Build tab. The actual apply / build /
        # verify only fires when the operator clicks Build there.
        self.image_save_continue_requested.emit()

    def _on_build_requested(self) -> None:
        # Operator confirmed the build preview: bubble up so the
        # MainWindow can run the apply / build / verify queue.
        self.build_requested.emit()

    # ---- summary banner -----------------------------------------------------

    def _refresh_summary(self) -> None:
        n_t0 = len(self.state.auto_t0)
        n_t1 = len(self.state.auto_t1)
        n_pending = len(self.state.pending)
        n_map = 0
        if self.state.smap is not None:
            n_map = sum(len(v) for v in self.state.smap.entries.values())
        if n_t0 == 0 and n_t1 == 0 and n_pending == 0 and n_map == 0:
            self.summary.setText(
                '<span style="color:%s;">Run a scan first to populate the review queue.</span>'
                % PALETTE.get("text_dim", "#888")
            )
            return
        ok = PALETTE.get("ok", "#27AE60")
        warn = PALETTE.get("warn", "#F2994A")
        dim = PALETTE.get("text_dim", "#888")
        if n_pending > 0:
            cta = (
                f'<span style="color:{warn};font-weight:600;">'
                f'{n_pending} need your decision</span> '
                f'<span style="color:{dim};">'
                f'(approve / skip / edit placeholder, then Promote approved)</span>'
            )
        else:
            cta = (
                f'<span style="color:{ok};font-weight:600;">'
                'No items need manual review</span> '
                f'<span style="color:{dim};">'
                "(go back to Pipeline and click 'Approve & continue')</span>"
            )
        self.summary.setText(
            f'<span style="color:{ok};">In map: <b>{n_map}</b></span>'
            f' &nbsp;·&nbsp; '
            f'<span style="color:{ok};">T0 auto: <b>{n_t0}</b></span>'
            f' &nbsp;·&nbsp; '
            f'<span style="color:{ok};">T1 auto: <b>{n_t1}</b></span>'
            f' &nbsp;·&nbsp; {cta}'
        )

    # ---- model <-> view -----------------------------------------------------

    def refresh_from_state(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        # Already-mapped entries per category.  Computed first so the
        # auto/pending buckets below can dedupe against it, without
        # this, every entry the user has already promoted would render
        # twice (once as ✓ in-map, once as ✓ auto or · pending).
        map_by_cat: dict[str, list[dict]] = {}
        map_keys: set[str] = set()
        if self.state.smap is not None:
            for cat, items in self.state.smap.entries.items():
                for it in items:
                    f = it.get("from")
                    if not f:
                        continue
                    map_by_cat.setdefault(cat, []).append(it)
                    map_keys.add(str(f))
        # Pending candidates per category, minus anything already in the map.
        by_cat: dict[str, list[Candidate]] = {}
        for c in self.state.pending:
            if c.value in map_keys:
                continue
            by_cat.setdefault(c.category or "other", []).append(c)
        # Auto-promoted candidates per category (with tier tag), same dedupe.
        auto_by_cat: dict[str, list[tuple[Candidate, str]]] = {}
        for c in self.state.auto_t0:
            if c.value in map_keys:
                continue
            auto_by_cat.setdefault(c.category or "other", []).append((c, "T0"))
        for c in self.state.auto_t1:
            if c.value in map_keys:
                continue
            auto_by_cat.setdefault(c.category or "other", []).append((c, "T1"))
        all_cats = (
            set(by_cat.keys()) | set(map_by_cat.keys()) | set(auto_by_cat.keys())
        )
        ordered = list(_CAT_ORDER) + sorted(
            c for c in all_cats if c not in _CAT_ORDER
        )
        for cat in ordered:
            pending_items = by_cat.get(cat) or []
            auto_items = auto_by_cat.get(cat) or []
            map_items = map_by_cat.get(cat) or []
            if not pending_items and not auto_items and not map_items:
                continue
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, "")
            parts = []
            if map_items:
                parts.append(f"{len(map_items)} in map")
            if auto_items:
                parts.append(f"{len(auto_items)} auto")
            if pending_items:
                parts.append(f"{len(pending_items)} pending")
            label = f"[{cat}]" + (f"  ·  {', '.join(parts)}" if parts else "")
            parent.setText(1, label)
            count_total = sum(c.count for c in pending_items) + sum(
                c.count for c, _ in auto_items
            )
            parent.setText(3, str(count_total))
            parent.setData(0, Qt.ItemDataRole.UserRole, cat)
            parent.setForeground(1, QColor(PALETTE["text_dim"]))
            # Map (locked) → Auto (will be merged at Promote) → Pending
            # (needs decision). Reading top-to-bottom matches the
            # lifecycle of an entry.
            for entry in map_items:
                parent.addChild(_MapItem(cat, entry))
            for cand, tier in auto_items:
                parent.addChild(_AutoItem(cand, tier))
            for c in pending_items:
                parent.addChild(_CandItem(c))
            parent.setExpanded(True)
        self.tree.blockSignals(False)
        self._apply_filter()
        n_map = sum(len(v) for v in map_by_cat.values())
        n_auto = sum(len(v) for v in auto_by_cat.values())
        self.lbl_count.setText(
            f"{len(self.state.pending)} pending  ·  {n_auto} auto  ·  "
            f"{n_map} already in map"
        )
        self._refresh_summary()

    # ---- filtering ----------------------------------------------------------

    def _apply_filter(self) -> None:
        q = self.search.text().lower().strip()
        cat = self.cat_combo.currentData() or ""
        only_uncertain = self.only_disagree.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            top_cat = top.data(0, Qt.ItemDataRole.UserRole) or ""
            top_visible = False
            if cat and top_cat and top_cat != cat:
                top.setHidden(True)
                continue
            for j in range(top.childCount()):
                ch = top.child(j)
                if isinstance(ch, _CandItem):
                    show = True
                    c = ch.cand
                    if q and q not in c.value.lower() and q not in c.suggested_placeholder.lower() and q not in c.rationale.lower():
                        show = False
                    if only_uncertain and c.critic_is_real_leak == "yes":
                        show = False
                    ch.setHidden(not show)
                    top_visible = top_visible or show
                elif isinstance(ch, _MapItem):
                    v = str(ch.entry.get("from", "")).lower()
                    t = str(ch.entry.get("to", "")).lower()
                    show = True
                    if q and q not in v and q not in t:
                        show = False
                    # ``only_uncertain`` is a candidate-specific filter;
                    # map rows are by definition certain, hide them when
                    # the user is triaging uncertain items only.
                    if only_uncertain:
                        show = False
                    ch.setHidden(not show)
                    top_visible = top_visible or show
                elif isinstance(ch, _AutoItem):
                    show = True
                    c = ch.cand
                    if (
                        q
                        and q not in c.value.lower()
                        and q not in c.suggested_placeholder.lower()
                        and q not in (c.rationale or "").lower()
                    ):
                        show = False
                    if only_uncertain:
                        show = False
                    ch.setHidden(not show)
                    top_visible = top_visible or show
                else:
                    ch.setHidden(False)
                    top_visible = True
            top.setHidden(not top_visible)

    # ---- actions ------------------------------------------------------------

    def _selected_cand_items(self) -> list[_CandItem]:
        out: list[_CandItem] = []
        for it in self.tree.selectedItems():
            if isinstance(it, _CandItem):
                out.append(it)
            else:
                # parent: include all visible children
                for j in range(it.childCount()):
                    ch = it.child(j)
                    if isinstance(ch, _CandItem) and not ch.isHidden():
                        out.append(ch)
        return out

    def _set_decision(self, decision: str) -> None:
        items = self._selected_cand_items()
        if not items:
            return
        # When the operator approves a candidate whose
        # ``suggested_placeholder`` is missing or echoes the value
        # (the LLM detector occasionally proposes identity placeholders
        # for ``ids``/``brand`` rows), auto-derive a placeholder via
        # the category strategy so the entry actually makes it into
        # the substitution_map. Without this the engine would drop
        # the row silently at promote time.
        if decision == _DECISION_APPROVE:
            self._auto_fill_placeholders(items)
        for it in items:
            it.decision = decision
            # Mirror onto the dataclass so write_candidates_yaml
            # round-trips it.
            it.cand.decision = decision
            it._refresh_color()
        self._persist_pending_list()
        self._schedule_preview_refresh()
        # Move to next visible row to speed up triage
        cur = self.tree.currentItem()
        if cur:
            below = self.tree.itemBelow(cur)
            while below and below.isHidden():
                below = self.tree.itemBelow(below)
            if below is not None:
                self.tree.setCurrentItem(below)

    def _auto_fill_placeholders(self, items: list["_CandItem"]) -> None:
        from anonymize.decisions_log import DecisionsLog
        from anonymize.placeholders import auto_derive_placeholder

        proj = self.state.project
        if proj is None:
            return
        try:
            log = DecisionsLog.load(proj.decisions_path)
        except Exception:
            return
        self.tree.blockSignals(True)
        try:
            for it in items:
                c = it.cand
                v = (c.value or "").strip()
                p = (c.suggested_placeholder or "").strip()
                if not v or (p and p != v):
                    continue
                new_p = auto_derive_placeholder(
                    v, c.category or "other", log=log
                )
                if new_p and new_p != v:
                    c.suggested_placeholder = new_p
                    it._refresh()
        finally:
            self.tree.blockSignals(False)

    def _edit_placeholder(self) -> None:
        # Route to the right handler depending on which row type is
        # selected. Each path persists in a different way: candidates
        # → in-memory only (until Promote), auto → ``auto_*.yml``,
        # map → ``substitution_map.yml``.
        sel = self.tree.selectedItems()
        map_rows = [it for it in sel if isinstance(it, _MapItem)]
        auto_rows = [it for it in sel if isinstance(it, _AutoItem)]
        cand_rows = [it for it in sel if isinstance(it, _CandItem)]
        if map_rows and not auto_rows and not cand_rows:
            self._edit_map_placeholder(map_rows)
            return
        if auto_rows and not cand_rows:
            self._edit_auto_placeholder(auto_rows)
            return
        items = self._selected_cand_items()
        if not items:
            return
        first = items[0].cand
        text, ok = QInputDialog.getText(
            self, "Edit placeholder", f"Placeholder for '{first.value}':",
            text=first.suggested_placeholder,
        )
        if not ok or not text:
            return
        self.tree.blockSignals(True)
        for it in items:
            it.cand.suggested_placeholder = text
            it._refresh()
        self.tree.blockSignals(False)
        self._schedule_preview_refresh()

    def _edit_auto_placeholder(self, items: list[_AutoItem]) -> None:
        if not items:
            return
        first = items[0].cand
        text, ok = QInputDialog.getText(
            self,
            "Edit placeholder",
            f"Placeholder for '{first.value}' (auto-approved):",
            text=first.suggested_placeholder,
        )
        if not ok or not text.strip():
            return
        text = text.strip()
        self.tree.blockSignals(True)
        try:
            for it in items:
                it.cand.suggested_placeholder = text
                it._refresh()
        finally:
            self.tree.blockSignals(False)
        self._persist_auto_lists()
        self._schedule_preview_refresh()

    def _persist_auto_lists(self) -> None:
        """Rewrite ``auto_promoted_t{0,1}.yml`` so inline edits survive
        a restart and feed the next Promote correctly."""
        proj = self.state.project
        if proj is None:
            return
        try:
            from anonymize.triage import write_candidates_yaml

            write_candidates_yaml(proj.auto_t0_path, self.state.auto_t0)
            write_candidates_yaml(proj.auto_t1_path, self.state.auto_t1)
        except Exception:
            pass

    def _persist_pending_list(self) -> None:
        """Rewrite ``needs_review.yml`` so decisions and inline edits
        on pending rows survive a restart. Without this the operator
        would have to re-decide every row after every GUI restart."""
        proj = self.state.project
        if proj is None:
            return
        try:
            from anonymize.triage import write_candidates_yaml

            write_candidates_yaml(proj.pending_path, self.state.pending)
        except Exception:
            pass

    def _edit_map_placeholder(self, items: list[_MapItem]) -> None:
        if not items or self.state.smap is None:
            return
        first = items[0].entry
        text, ok = QInputDialog.getText(
            self,
            "Edit placeholder",
            f"Placeholder for '{first.get('from', '')}':",
            text=str(first.get("to", "")),
        )
        if not ok or not text.strip():
            return
        text = text.strip()
        smap = self.state.smap
        for it in items:
            mid = str(it.entry.get("id", ""))
            if not mid:
                continue
            smap.update(mid, to=text)
            it.entry["to"] = text
        try:
            smap.save()
        except Exception:
            return
        self.state.map_changed.emit(smap)

    def _add_to_map_clicked(self) -> None:
        smap = self.state.smap
        if smap is None:
            dismissible_message(
                self,
                "information",
                "No project",
                "Open or create a project first, the substitution "
                "map lives inside the project's output folder.",
            )
            return
        # Default to the currently filtered category if any, otherwise
        # the parent of the focused row, otherwise "other".
        default_cat = self.cat_combo.currentData() or ""
        if not default_cat:
            cur = self.tree.currentItem()
            if cur is not None:
                top = cur if cur.parent() is None else cur.parent()
                default_cat = top.data(0, Qt.ItemDataRole.UserRole) or ""
        if not default_cat:
            default_cat = "other"
        dlg = _AddToMapDialog(
            categories=_CAT_ORDER,
            default_category=default_cat,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value, cat, placeholder = dlg.values()
        if not value or not placeholder:
            dismissible_message(
                self,
                "warning",
                "Missing fields",
                "Both <b>Value</b> and <b>Placeholder</b> are required.",
            )
            return
        if value == placeholder:
            dismissible_message(
                self,
                "warning",
                "Identity placeholder",
                "Placeholder must differ from the value, otherwise the "
                "entry would be a no-op.",
            )
            return
        # Reject duplicates (same ``from`` already mapped under any
        # category), adding it twice would produce a duplicate_from
        # invariant violation.
        existing = smap.find(value)
        if existing is not None:
            dismissible_message(
                self,
                "warning",
                "Already mapped",
                f"<b>{value}</b> is already mapped under category "
                f"<i>{existing[0]}</i>. Edit the existing row instead.",
            )
            return
        smap.add(cat, value, placeholder)
        try:
            smap.save()
        except Exception as e:
            dismissible_message(self, "critical", "Save failed", str(e))
            return
        self.state.map_changed.emit(smap)

    def _unapprove_selected(self) -> None:
        """Un-approve the rows currently selected in the tree.

        Thin wrapper around :meth:`_unapprove_items` so the menu /
        button / keyboard handler all share the same business logic
        and confirmation dialog.
        """
        self._unapprove_items(self.tree.selectedItems())

    def _unapprove_selected_category(self) -> None:
        """Un-approve every approved row under the selected parent(s).

        Mirror of :meth:`_approve_selected_category` for the unapprove
        side of the menu. Collects approved candidates, auto-promoted
        rows, and in-map rows that sit under the currently-selected
        category headers, then routes them through the shared bulk
        unapprove flow.
        """
        targets: list = []
        for it in self.tree.selectedItems():
            if it.parent() is None:
                for j in range(it.childCount()):
                    ch = it.child(j)
                    if isinstance(ch, _CandItem):
                        if ch.decision == _DECISION_APPROVE and not ch.isHidden():
                            targets.append(ch)
                    elif isinstance(ch, (_AutoItem, _MapItem)) and not ch.isHidden():
                        targets.append(ch)
        if not targets:
            return
        self._unapprove_items(targets)

    def _unapprove_all_approved(self) -> None:
        """Un-approve every approved row in the tree, regardless of
        which category headers happen to be selected. Mirror of
        :meth:`_approve_all_pending`. Touches: pending rows marked
        approved, auto T0/T1 rows, and substitution-map entries.
        """
        targets: list = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for j in range(parent.childCount()):
                ch = parent.child(j)
                if isinstance(ch, _CandItem):
                    if ch.decision == _DECISION_APPROVE:
                        targets.append(ch)
                elif isinstance(ch, (_AutoItem, _MapItem)):
                    targets.append(ch)
        if not targets:
            return
        self._unapprove_items(targets)

    def _unapprove_items(self, items) -> None:
        """Un-approve the supplied rows.

        Per the new in_map semantics:
        * Approved pending rows (``_CandItem`` with ``decision=approve``)
          are reset to ``decision=pending`` and stay visible in their
          original colour, ready to be re-approved or deleted.
        * Auto-approved (T0/T1) rows are demoted back to the pending
          review queue so the operator can re-decide.
        * In-map rows are removed from ``substitution_map.yml`` AND
          re-added to the pending queue so the row stays visible (the
          previous behaviour silently dropped the row from the tree,
          which the operator perceived as a "delete").

        All three sub-actions share a single confirmation dialog with
        a per-type breakdown so the user understands exactly what
        will change.
        """
        sel = list(items or [])
        map_rows = [it for it in sel if isinstance(it, _MapItem)]
        auto_rows = [it for it in sel if isinstance(it, _AutoItem)]
        cand_approved_rows = [
            it for it in sel
            if isinstance(it, _CandItem) and it.decision == _DECISION_APPROVE
        ]
        if not map_rows and not auto_rows and not cand_approved_rows:
            return
        smap = self.state.smap
        # Build a human-readable preview of what the action will hit.
        sample: list[str] = []
        for it in map_rows[:3]:
            sample.append(f"'{it.entry.get('from', '')}' (in map)")
        for it in auto_rows[: max(0, 3 - len(sample))]:
            sample.append(f"'{it.cand.value}' (auto {it.tier})")
        for it in cand_approved_rows[: max(0, 3 - len(sample))]:
            sample.append(f"'{it.cand.value}' (approved pending)")
        more = (
            (len(map_rows) + len(auto_rows) + len(cand_approved_rows))
            - len(sample)
        )
        preview = ", ".join(sample) + (f" + {more} more" if more > 0 else "")
        parts: list[str] = []
        if map_rows:
            parts.append(
                f"{len(map_rows)} entry/ies will be removed from "
                f"<code>substitution_map.yml</code> (and stay visible "
                f"as pending so you can re-approve or delete them)"
            )
        if auto_rows:
            parts.append(
                f"{len(auto_rows)} auto-approved row(s) will be demoted "
                f"back to the pending review queue"
            )
        if cand_approved_rows:
            parts.append(
                f"{len(cand_approved_rows)} approved pending row(s) will "
                f"go back to the undecided state"
            )
        total = len(map_rows) + len(auto_rows) + len(cand_approved_rows)
        ans = dismissible_message(
            self,
            "question",
            "Un-approve",
            (
                f"<b>{total}</b> row(s) selected ({preview}).<br><br>"
                + "<br>".join(f"• {p}" for p in parts)
                + "<br><br><i>Documents already anonymised stay as they "
                "are, this only changes what future Apply runs will "
                "substitute.</i>"
            ),
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            default_button=QMessageBox.StandardButton.Cancel,
            dismissible=False,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        # 1. Map rows: drop from smap and re-stage as pending so they
        #    remain visible in the tree (the old behaviour purged them
        #    from view, which the operator read as a "delete").
        if map_rows and smap is not None:
            recycled: list[Candidate] = []
            for it in map_rows:
                mid = str(it.entry.get("id", ""))
                if mid:
                    smap.remove(mid)
                value = str(it.entry.get("from", "") or "")
                if not value:
                    continue
                recycled.append(
                    Candidate(
                        value=value,
                        category=it.category or "other",
                        suggested_placeholder=str(it.entry.get("to", "") or ""),
                        tier="T2_human",
                        decision=_DECISION_PENDING,
                        rationale="un-approved from substitution_map",
                    )
                )
            try:
                smap.save()
            except Exception as e:
                dismissible_message(self, "critical", "Save failed", str(e))
                return
            if recycled:
                pending_vals = {c.value for c in self.state.pending}
                merged = list(self.state.pending) + [
                    c for c in recycled if c.value not in pending_vals
                ]
                self.state.set_candidates(pending=merged)
                self._persist_pending_list()
            self.state.map_changed.emit(smap)
        # 2. Auto-approved rows: demote back to the pending queue.
        if auto_rows:
            self._demote_auto_rows(auto_rows)
        # 3. Approved pending rows: reset to ``decision=pending`` so
        #    they stay visible in their original colour. Items keep
        #    their ✓→· marker; the next approve/delete works on them.
        if cand_approved_rows:
            self.tree.blockSignals(True)
            try:
                for it in cand_approved_rows:
                    it.decision = _DECISION_PENDING
                    it.cand.decision = _DECISION_PENDING
                    it._refresh_color()
            finally:
                self.tree.blockSignals(False)
            self._persist_pending_list()
            self._schedule_preview_refresh()

    def _delete_selected(self) -> None:
        """Hard-remove the selected rows everywhere they're stored.

        Distinct from :meth:`_unapprove_selected` (which demotes auto
        rows back to *pending* so the operator can re-decide). Delete
        is the "make it disappear, don't ask me again" action, the
        candidate is purged from the pending YAML, the auto-promoted
        YAMLs and the substitution map alike. Already-anonymised
        documents are NOT touched (this only changes future Apply
        runs); a single confirmation dialog states that.
        """
        sel = self.tree.selectedItems()
        cand_rows = [it for it in sel if isinstance(it, _CandItem)]
        auto_rows = [it for it in sel if isinstance(it, _AutoItem)]
        map_rows = [it for it in sel if isinstance(it, _MapItem)]
        if not (cand_rows or auto_rows or map_rows):
            return
        smap = self.state.smap
        sample: list[str] = []
        for it in cand_rows[:3]:
            sample.append(f"'{it.cand.value}' (pending)")
        for it in auto_rows[: max(0, 3 - len(sample))]:
            sample.append(f"'{it.cand.value}' (auto {it.tier})")
        for it in map_rows[: max(0, 3 - len(sample))]:
            sample.append(f"'{it.entry.get('from', '')}' (in map)")
        more = (len(cand_rows) + len(auto_rows) + len(map_rows)) - len(sample)
        preview = ", ".join(sample) + (f" + {more} more" if more > 0 else "")
        ans = dismissible_message(
            self,
            "question",
            "Delete",
            (
                f"<b>{len(cand_rows) + len(auto_rows) + len(map_rows)}</b> "
                f"row(s) selected ({preview}).<br><br>"
                "They will be removed from every storage layer "
                "(pending review queue, auto-promoted YAMLs, "
                "<code>substitution_map.yml</code>) and will <b>not</b> "
                "be re-detected on the next scan unless their value "
                "appears again in a future document.<br><br>"
                "<i>Documents already anonymised stay as they are, "
                "this only changes what future Apply runs will "
                "substitute.</i>"
            ),
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            default_button=QMessageBox.StandardButton.Cancel,
            dismissible=False,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return

        # 1. Map rows: drop the entry by id.
        if map_rows and smap is not None:
            for it in map_rows:
                mid = str(it.entry.get("id", ""))
                if mid:
                    smap.remove(mid)
            try:
                smap.save()
            except Exception as e:
                dismissible_message(self, "critical", "Save failed", str(e))
                return
            self.state.map_changed.emit(smap)

        # 2. Pending + auto rows: drop from in-memory state and persist.
        if cand_rows or auto_rows:
            drop_vals = (
                {it.cand.value for it in cand_rows}
                | {it.cand.value for it in auto_rows}
            )
            new_t0 = [c for c in self.state.auto_t0 if c.value not in drop_vals]
            new_t1 = [c for c in self.state.auto_t1 if c.value not in drop_vals]
            new_pending = [
                c for c in self.state.pending if c.value not in drop_vals
            ]
            self.state.set_candidates(
                auto_t0=new_t0, auto_t1=new_t1, pending=new_pending
            )
            proj = self.state.project
            if proj is not None:
                try:
                    from anonymize.triage import write_candidates_yaml

                    write_candidates_yaml(proj.auto_t0_path, new_t0)
                    write_candidates_yaml(proj.auto_t1_path, new_t1)
                    write_candidates_yaml(proj.pending_path, new_pending)
                except Exception:
                    pass
        self._schedule_preview_refresh()

    def _show_tree_menu(self, pos) -> None:
        """Right-click context menu on the candidate tree.

        The menu is the same shape regardless of the row type under
        the cursor, every action is wired through one handler that
        does the right thing per row class. This way the operator
        learns one menu, not three.

        * Approve / Skip / Edit placeholder act on pending rows
          (no-op + greyed when the selection is all auto / map).
        * Approve category / Approve all pending act on the whole
          tree, not just the selection, handy when you trust the
          model and want to bulk-promote.
        * Unapprove undoes whatever approval state the row carries:
          pending Y -> back to undecided; auto -> demoted to pending;
          in-map -> removed from substitution_map.yml.
        * Delete is the universal "make this disappear from every
          storage layer" action.
        """
        sel = self.tree.selectedItems()
        if not sel:
            return
        has_cand = any(isinstance(it, _CandItem) for it in sel)
        any_pending = bool(self.state.pending)

        menu = QMenu(self.tree)
        act_approve = menu.addAction(
            "Approve  (Y)",
            lambda: self._set_decision(_DECISION_APPROVE),
        )
        act_approve.setEnabled(has_cand)
        act_skip = menu.addAction(
            "Skip  (N)",
            lambda: self._set_decision(_DECISION_SKIP),
        )
        act_skip.setEnabled(has_cand)
        act_edit = menu.addAction(
            "Edit placeholder…  (M)", self._edit_placeholder
        )
        act_edit.setEnabled(has_cand or any(isinstance(it, _MapItem) for it in sel))
        menu.addSeparator()
        act_cat = menu.addAction(
            "Approve all in this category", self._approve_selected_category
        )
        act_cat.setEnabled(has_cand)
        act_all = menu.addAction(
            "Approve all pending", self._approve_all_pending
        )
        act_all.setEnabled(any_pending)
        menu.addSeparator()
        menu.addAction("Unapprove", self._unapprove_selected)
        act_uncat = menu.addAction(
            "Unapprove all in this category", self._unapprove_selected_category
        )
        # Enabled whenever the selection contains a category header
        # (parent row). The handler itself does the filtering, so we
        # can be generous with the enable predicate.
        act_uncat.setEnabled(any(it.parent() is None for it in sel))
        any_approved = (
            any(
                isinstance(it, _CandItem) and it.decision == _DECISION_APPROVE
                for it in (sel or [])
            )
            or bool(self.state.auto_t0)
            or bool(self.state.auto_t1)
            or bool(
                self.state.smap.entries
                if self.state.smap is not None
                else []
            )
        )
        act_unall = menu.addAction(
            "Unapprove all approved", self._unapprove_all_approved
        )
        act_unall.setEnabled(any_approved)
        menu.addAction("Delete", self._delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _approve_all_pending(self) -> None:
        """One-click bulk approve: mark every pending row as approved.

        Doesn't promote or apply on its own, just sets the decision
        flag so the next ``Promote & build`` merges them. The operator
        can still un-approve individual rows afterwards.
        """
        all_items: list[_CandItem] = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for j in range(parent.childCount()):
                ch = parent.child(j)
                if isinstance(ch, _CandItem):
                    all_items.append(ch)
        if not all_items:
            return
        ans = dismissible_message(
            self,
            "question",
            "Approve all pending",
            (
                f"<b>Approve every pending row</b> "
                f"({len(all_items)} item(s)) across all categories? "
                "You can still unapprove individual rows before "
                "clicking Promote & build."
            ),
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            default_button=QMessageBox.StandardButton.Ok,
            dismissible=False,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        # Mirror exactly what _set_decision does, but on every
        # candidate row in the tree (not just the selection): auto-fill
        # missing placeholders, set decision, refresh row colour,
        # persist, schedule preview refresh.
        self._auto_fill_placeholders(all_items)
        for it in all_items:
            it.decision = _DECISION_APPROVE
            it.cand.decision = _DECISION_APPROVE
            it._refresh_color()
        self._persist_pending_list()
        self._schedule_preview_refresh()

    def _demote_auto_rows(self, items: list[_AutoItem]) -> None:
        """Move ``items`` from the auto-approved buckets back to
        ``state.pending`` and persist the three YAMLs."""
        if not items:
            return
        moved = [it.cand for it in items]
        moved_vals = {c.value for c in moved}
        new_t0 = [c for c in self.state.auto_t0 if c.value not in moved_vals]
        new_t1 = [c for c in self.state.auto_t1 if c.value not in moved_vals]
        pending_vals = {c.value for c in self.state.pending}
        new_pending = list(self.state.pending) + [
            c for c in moved if c.value not in pending_vals
        ]
        self.state.set_candidates(
            auto_t0=new_t0, auto_t1=new_t1, pending=new_pending
        )
        proj = self.state.project
        if proj is not None:
            try:
                from anonymize.triage import write_candidates_yaml

                write_candidates_yaml(proj.auto_t0_path, new_t0)
                write_candidates_yaml(proj.auto_t1_path, new_t1)
                write_candidates_yaml(proj.pending_path, new_pending)
            except Exception:
                pass
        self._schedule_preview_refresh()

    def _approve_selected_category(self) -> None:
        # Approve everything visible under the currently selected parent(s).
        approved_items: list[_CandItem] = []
        for it in self.tree.selectedItems():
            if it.parent() is None:
                for j in range(it.childCount()):
                    ch = it.child(j)
                    if isinstance(ch, _CandItem) and not ch.isHidden():
                        approved_items.append(ch)
        if approved_items:
            self._auto_fill_placeholders(approved_items)
            for ch in approved_items:
                ch.decision = _DECISION_APPROVE
                ch.cand.decision = _DECISION_APPROVE
                ch._refresh_color()
            self._persist_pending_list()
            self._schedule_preview_refresh()

    def _on_item_changed(self, it, col) -> None:
        # Columns 1 (value / from) and 2 (placeholder / to) are
        # editable on every row type. Other columns are display-only -
        # snap them back if the user accidentally enters edit mode.
        if isinstance(it, _CandItem):
            if col == 1:
                self._apply_value_edit(
                    it, col, it.cand.value, "value", persist=self._persist_pending_list
                )
            elif col == 2:
                self._apply_value_edit(
                    it,
                    col,
                    it.cand.suggested_placeholder or "",
                    "suggested_placeholder",
                    persist=self._persist_pending_list,
                )
            else:
                self._snap_back(it)
                return
            self._schedule_preview_refresh()
            return
        if isinstance(it, _AutoItem):
            if col == 1:
                self._apply_value_edit(
                    it, col, it.cand.value, "value", persist=self._persist_auto_lists
                )
            elif col == 2:
                self._apply_value_edit(
                    it,
                    col,
                    it.cand.suggested_placeholder or "",
                    "suggested_placeholder",
                    persist=self._persist_auto_lists,
                )
            else:
                self._snap_back(it)
                return
            self._schedule_preview_refresh()
            return
        if isinstance(it, _MapItem):
            if self.state.smap is None:
                self._snap_back(it)
                return
            if col == 1:
                self._handle_map_from_edit(it)
            elif col == 2:
                self._handle_map_to_edit(it)
            else:
                self._snap_back(it)
            return

    def _snap_back(self, it: QTreeWidgetItem) -> None:
        """Revert any inline edit on ``it`` to the underlying model
        state, used for non-editable columns (count / confidence /
        verdict / rationale / examples) so a stray double-click can't
        produce a row whose visible cells lie about the data."""
        self.tree.blockSignals(True)
        try:
            it._refresh()  # type: ignore[attr-defined]
        finally:
            self.tree.blockSignals(False)

    def _set_cell(self, it: QTreeWidgetItem, col: int, value: str) -> None:
        self.tree.blockSignals(True)
        try:
            it.setText(col, value)
        finally:
            self.tree.blockSignals(False)

    def _apply_value_edit(
        self,
        it,
        col: int,
        old_value: str,
        attr: str,
        *,
        persist=None,
    ) -> None:
        """Common path for editable cells on candidate-backed rows
        (``_CandItem`` / ``_AutoItem``).

        ``attr`` is the attribute on ``it.cand`` to update; ``persist``
        is an optional callable invoked after a successful change so
        the on-disk YAML matches the in-memory state immediately.
        Sets ``cand.user_edited = True`` so the pipeline's merge
        logic refuses to clobber the change on the next scan re-run.
        """
        new_value = it.text(col).strip()
        if not new_value:
            self._set_cell(it, col, old_value)
            return
        if new_value == old_value:
            return
        # Snapshot the detector-supplied value the first time the
        # operator renames it, so the pipeline's merge logic can still
        # match this row across scan re-runs by its original key.
        if attr == "value" and not getattr(it.cand, "original_value", ""):
            try:
                it.cand.original_value = old_value
            except Exception:
                pass
        setattr(it.cand, attr, new_value)
        # Mark the candidate as user-edited so re-runs of scan/detect
        # (which would otherwise rewrite the YAML with the detector's
        # original output) merge-preserve this row.
        try:
            it.cand.user_edited = True
        except Exception:
            pass
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

    def _handle_map_from_edit(self, it: "_MapItem") -> None:
        smap = self.state.smap
        new_from = it.text(1).strip()
        old_from = str(it.entry.get("from", ""))
        if not new_from:
            self._set_cell(it, 1, old_from)
            return
        if new_from == old_from:
            return
        # Another row in the map can't already claim this ``from``;
        # SubstitutionMap.validate_invariants would flag it as a
        # ``duplicate_from`` violation otherwise.
        my_id = str(it.entry.get("id", ""))
        for cat, items in smap.entries.items():
            for entry in items:
                if str(entry.get("id", "")) == my_id:
                    continue
                if str(entry.get("from", "")) == new_from:
                    self._set_cell(it, 1, old_from)
                    dismissible_message(
                        self,
                        "warning",
                        "Duplicate value",
                        f"<b>{new_from}</b> is already mapped under "
                        f"category <i>{cat}</i>. Edit that row instead.",
                    )
                    return
        it.entry["from"] = new_from
        try:
            smap.save()
        except Exception as e:
            self._set_cell(it, 1, old_from)
            dismissible_message(self, "critical", "Save failed", str(e))
            return
        self.state.map_changed.emit(smap)

    def _handle_map_to_edit(self, it: "_MapItem") -> None:
        smap = self.state.smap
        new_to = it.text(2).strip()
        old_to = str(it.entry.get("to", ""))
        if not new_to:
            self._set_cell(it, 2, old_to)
            return
        if new_to == old_to:
            return
        mid = str(it.entry.get("id", ""))
        if not mid:
            return
        smap.update(mid, to=new_to)
        it.entry["to"] = new_to
        try:
            smap.save()
        except Exception:
            self._set_cell(it, 2, old_to)
            return
        self.state.map_changed.emit(smap)

    def _promote_clicked(self) -> None:
        approved = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                if isinstance(ch, _CandItem) and ch.decision == _DECISION_APPROVE:
                    approved.append(ch.cand)
        self.promote_requested.emit(approved)


    # ---- live preview --------------------------------------------------------

    def _on_project_changed(self, _proj) -> None:
        # Repopulate the preview-file combo when the project changes,
        # listing the input files (single / multi / folder) so the
        # user can pick which one to preview. Hide the combo entirely
        # in single-file mode where there's only one option.
        self.preview_file_combo.blockSignals(True)
        self.preview_file_combo.clear()
        proj = self.state.project
        if proj is not None:
            try:
                files = self._discover_input_files(proj)
            except Exception:
                files = []
            for p in files:
                self.preview_file_combo.addItem(p.name, str(p))
            multi = len(files) > 1
            self.preview_file_combo.setVisible(
                multi and self.preview_toggle.isChecked()
            )
        self.preview_file_combo.blockSignals(False)
        if self.preview_toggle.isChecked():
            self._refresh_preview()

    @staticmethod
    def _discover_input_files(proj) -> list[Path]:
        out: list[Path] = []
        if not proj or not proj.input_paths:
            return out
        if proj.mode == "folder":
            root = proj.input_paths[0]
            if root.is_dir():
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix.lower() in {
                        ".pdf", ".md", ".txt", ".html", ".docx",
                        ".pptx", ".odt", ".rtf",
                    }:
                        out.append(p)
        elif proj.mode == "multi":
            out.extend(p for p in proj.input_paths if p.is_file())
        else:  # single
            out.append(proj.input_paths[0])
        return out

    def _on_preview_add_to_map(self, value: str) -> None:
        """Right-click on a PDF/Office preview selection → add the
        word to ``substitution_map.yml`` under category ``other``
        with placeholder ``XXXX``. Streamlined (no dialog) so the
        operator can build the map by clicking through the preview.

        Map entries are the source of truth for the preview, so the
        new mapping appears highlighted in both the live preview
        (via :meth:`_build_preview_rules`) and the Build-preview tab
        immediately. A toast confirms the action since the context
        menu disappears with no other visible side-effect.
        """
        from .toast import Toaster

        proj = self.state.project
        if proj is None:
            return
        clean = (value or "").strip()
        if not clean:
            return
        smap = self.state.smap
        if smap is None:
            return
        existing = smap.find(clean)
        if existing is not None:
            # Silent toast on duplicates so repeated right-clicks on
            # the same word don't pop modal dialogs.
            Toaster.notify(
                "Already mapped",
                f"'{clean[:60]}' is already under '{existing[0]}'",
                kind="info",
            )
            return
        smap.add("other", clean, "XXXX")
        try:
            smap.save()
        except Exception as e:
            Toaster.notify("Save failed", str(e)[:120], kind="err")
            return
        self.state.map_changed.emit(smap)
        Toaster.notify(
            "Added to substitution map",
            f"'{clean[:60]}' → XXXX (category: other)",
            kind="ok",
        )

    def _on_preview_toggled(self, on: bool) -> None:
        self.preview_stack.setVisible(on)
        # Show / hide the file picker only in multi-file mode.
        multi = self.preview_file_combo.count() > 1
        self.preview_file_combo.setVisible(on and multi)
        self._refresh_preview_zoom_visibility(on)
        self.preview_toggle.setText("Hide preview" if on else "Show preview")
        if on:
            # Reveal the right pane of the splitter.
            self._body_splitter.setSizes([600, 600])
            self._refresh_preview()
        else:
            self._body_splitter.setSizes([1000, 0])

    def _refresh_preview_zoom_visibility(self, preview_on: bool) -> None:
        """Hide the external zoom buttons when the active preview pane
        already has its own toolbar (PDF.js for PDF/Office), keep them
        for text-based panes that don't.

        The user feedback was: external zoom is redundant on PDF (the
        embedded PDF.js viewer has its own toolbar) and shouldn't
        clutter the row when it's not needed; on Markdown / Plain /
        HTML panes the external buttons remain the only zoom
        controls and stay visible."""
        if not preview_on:
            for btn in (
                self.preview_zoom_out,
                self.preview_zoom_in,
                self.preview_zoom_fit,
                self.preview_zoom_100,
            ):
                btn.setVisible(False)
            return
        pane = self.preview_stack.currentWidget()
        is_internal_viewer = isinstance(pane, SelectablePdfRenderPane)
        for btn in (
            self.preview_zoom_out,
            self.preview_zoom_in,
            self.preview_zoom_fit,
            self.preview_zoom_100,
        ):
            btn.setVisible(not is_internal_viewer)

    def _zoom_preview(self, action: str) -> None:
        pane = self.preview_stack.currentWidget()
        if pane is None:
            return
        fn = {
            "in": getattr(pane, "zoom_in", None),
            "out": getattr(pane, "zoom_out", None),
            "fit": getattr(pane, "fit_to_window", None),
            "reset": getattr(pane, "zoom_reset", None),
        }.get(action)
        if callable(fn):
            fn()

    def _current_preview_file(self) -> Path | None:
        proj = self.state.project
        if proj is None:
            return None
        idx = self.preview_file_combo.currentIndex()
        if idx >= 0:
            data = self.preview_file_combo.itemData(idx)
            if data:
                return Path(data)
        files = self._discover_input_files(proj)
        return files[0] if files else None

    _PREVIEW_CACHE_DIR = Path(tempfile.gettempdir()) / "anondiff" / "preview"

    def _schedule_preview_refresh(self) -> None:
        """Coalesce rapid review actions into a single preview re-apply."""
        if self.preview_toggle.isChecked():
            self._preview_refresh_timer.start()

    def _refresh_preview(self) -> None:
        """Render the document AS IT WOULD LOOK after Promote+Apply.

        Builds a unified rule-set (already-mapped + auto-T0/T1 + approved
        pending), runs the actual format adapter to produce a temporary
        anonymised file, then renders THAT in the preview pane with
        highlights on the placeholder text.  Cached on disk so repeated
        views of the same state are fast.
        """
        if not self.preview_toggle.isChecked():
            return
        path = self._current_preview_file()
        if path is None or not path.exists():
            return
        rules = self._build_preview_rules()
        out_path = self._cached_apply(path, rules)
        if out_path is None:
            # Apply failed (broken adapter, missing dep, etc.), fall back
            # to the original file so the pane still shows something.
            out_path = path

        # Use the selectable PDF / Office variants (PDF.js via
        # WebEngine) so the operator can drag-select text in the
        # preview. Highlight events get baked as native PDF
        # annotations on a temp copy of the file, so the colour
        # mapping the rasterised pane uses is preserved while
        # selection works.
        pane_cls = pick_selectable_pane_for(out_path)
        pane = self._preview_panes.get(pane_cls)
        if pane is None:
            pane = pane_cls()
            self._preview_panes[pane_cls] = pane
            self.preview_stack.addWidget(pane)
            # Right-click "Add to substitution map" inside the
            # rendered preview wires straight into AppState (same
            # path the Build-preview pane uses). Every selectable
            # pane (PDF / Office / HTML / Markdown / Spreadsheet /
            # plaintext) exposes the same ``add_to_map_requested``
            # signal, so we connect by capability rather than class.
            if hasattr(pane, "add_to_map_requested"):
                pane.add_to_map_requested.connect(
                    self._on_preview_add_to_map
                )
        self.preview_stack.setCurrentWidget(pane)
        # Refresh the toolbar zoom buttons: if we just switched from a
        # PDF (internal viewer toolbar) to a Markdown / Plain pane, the
        # external buttons need to reappear, and vice versa.
        self._refresh_preview_zoom_visibility(self.preview_toggle.isChecked())

        events = self._build_output_highlight_events(out_path, rules)

        if isinstance(pane, SelectableOfficeRenderPane):
            pane.load_office(out_path, events=events, side="left")
        elif isinstance(pane, SelectablePdfRenderPane):
            pane.load_pdf(out_path, events=events, side="left")
        elif isinstance(pane, OfficeRenderPane):
            pane.load_office(out_path)
            pane.set_events(events, side="left")
        elif isinstance(pane, PdfRenderPane):
            pane.load_pdf(out_path)
            pane.set_events(events, side="left")
        elif isinstance(pane, HtmlRenderPane):
            try:
                txt = out_path.read_text(encoding="utf-8")
            except Exception:
                txt = ""
            values = [r.to for r in rules if r.to]
            keys = [r.from_ or r.to for r in rules if r.to]
            pane.render_html(
                txt,
                highlight_values=values,
                scheme="per_mapping",
                color_keys=keys,
            )
        elif isinstance(pane, MarkdownRenderPane):
            try:
                txt = out_path.read_text(encoding="utf-8")
            except Exception:
                txt = ""
            values = [r.to for r in rules if r.to]
            keys = [r.from_ or r.to for r in rules if r.to]
            pane.render_markdown(
                txt,
                highlight_values=values,
                scheme="per_mapping",
                color_keys=keys,
            )
        elif isinstance(pane, SpreadsheetRenderPane):
            values = [r.to for r in rules if r.to]
            keys = [r.from_ or r.to for r in rules if r.to]
            pane.render_spreadsheet(
                out_path,
                highlight_values=values,
                scheme="per_mapping",
                color_keys=keys,
            )
        elif isinstance(pane, PlainTextRenderPane):
            try:
                txt = out_path.read_text(encoding="utf-8")
            except Exception:
                txt = f"<cannot extract: {out_path}>"
            pane.set_text(txt)
            spans = []
            for r in rules:
                t = r.to or ""
                if not t:
                    continue
                start = 0
                while True:
                    i = txt.find(t, start)
                    if i < 0:
                        break
                    spans.append({
                        "off": i,
                        "len": len(t),
                        "value": t,
                        "to": t,
                        "category": r.category or "",
                    })
                    start = i + max(1, len(t))
            pane.set_spans(spans)

    def _build_preview_rules(self) -> list[SubstitutionRule]:
        """Combined substitution rules used to drive the live preview.

        Order: (1) already-mapped entries are authoritative, (2) auto-T0
        and auto-T1 candidates that will be merged at Promote, (3)
        approved pending candidates the operator has just ticked. A
        ``from`` value already covered by an earlier source is skipped
        so the rule list stays free of duplicates.
        """
        rules: list[SubstitutionRule] = []
        seen: set[str] = set()
        if self.state.smap is not None:
            for r in self.state.smap.to_rules(tier="preview"):
                if r.from_ and r.from_ not in seen and r.to and r.to != r.from_:
                    rules.append(r)
                    seen.add(r.from_)
        for c in list(self.state.auto_t0) + list(self.state.auto_t1):
            v = (c.value or "").strip()
            p = (c.suggested_placeholder or "").strip()
            if not v or not p or v == p or v in seen:
                continue
            rules.append(
                SubstitutionRule(
                    from_=v, to=p, category=c.category or "other", tier="preview"
                )
            )
            seen.add(v)
        for it in self._iter_cand_items():
            if it.decision != _DECISION_APPROVE:
                continue
            v = (it.cand.value or "").strip()
            p = (it.cand.suggested_placeholder or "").strip()
            if not v or not p or v == p or v in seen:
                continue
            rules.append(
                SubstitutionRule(
                    from_=v, to=p, category=it.cand.category or "other", tier="preview"
                )
            )
            seen.add(v)
        return rules

    def _cached_apply(
        self, src: Path, rules: list[SubstitutionRule]
    ) -> Path | None:
        """Run the format adapter to produce an anonymised copy of
        ``src``, caching by ``(src, mtime, rules)``.  Returns ``None`` on
        failure (caller should fall back to the original)."""
        if not rules:
            return src  # nothing to substitute → original is the after-state
        try:
            self._PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        try:
            mtime = src.stat().st_mtime_ns
        except Exception:
            mtime = 0
        rule_blob = json.dumps(
            [
                (r.from_, r.to, r.category, bool(r.case_insensitive))
                for r in sorted(rules, key=lambda x: (x.from_, x.to))
            ],
            sort_keys=True,
            ensure_ascii=False,
        )
        sig = hashlib.md5(
            f"{src.resolve()}|{mtime}|{rule_blob}".encode("utf-8")
        ).hexdigest()[:16]
        out = self._PREVIEW_CACHE_DIR / f"{sig}_{src.stem}{src.suffix}"
        if out.exists():
            return out
        try:
            ad = get_adapter(src)
            ad.write(src, out, rules)
        except Exception:
            # Best-effort cleanup of any partial file the adapter may
            # have produced before failing.
            try:
                if out.exists():
                    out.unlink()
            except Exception:
                pass
            return None
        return out if out.exists() else None

    def _build_output_highlight_events(
        self, out_path: Path, rules: list[SubstitutionRule]
    ) -> list[dict]:
        """Return rect/event dicts pointing at the placeholder text in
        the *anonymised* file. PDF rects come from
        ``page.search_for(to)`` so highlights land on the substitution,
        not on the original word."""
        events: list[dict] = []
        if not rules or out_path.suffix.lower() != ".pdf":
            return events
        try:
            import fitz  # type: ignore
        except Exception:
            return events
        try:
            doc = fitz.open(str(out_path))
        except Exception:
            return events
        try:
            for pi in range(doc.page_count):
                page = doc.load_page(pi)
                for r in rules:
                    if not r.to:
                        continue
                    try:
                        rects = list(page.search_for(r.to) or [])
                    except Exception:
                        rects = []
                    for rect in rects:
                        events.append({
                            "from": r.from_,
                            "to": r.to,
                            "value": r.to,
                            "category": r.category or "",
                            "mapping_id": r.mapping_id or "",
                            "page": pi,
                            "rects": [
                                (
                                    float(rect[0]),
                                    float(rect[1]),
                                    float(rect[2]),
                                    float(rect[3]),
                                )
                            ],
                        })
        finally:
            doc.close()
        return events

    def _iter_cand_items(self):
        for top_idx in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(top_idx)
            for child_idx in range(top.childCount()):
                child = top.child(child_idx)
                if isinstance(child, _CandItem):
                    yield child


__all__ = ["ReviewView"]
