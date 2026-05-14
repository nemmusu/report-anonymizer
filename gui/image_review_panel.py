"""Image-redaction Review tab (inline editor flavour).

Layout:

    +-- thumbnail strip (horizontal, scrollable) ------------------+
    |  [t1] [t2] [✓ t3] [t4] [t5] [t6] ...                         |
    +--------------------------------------------------------------+
    |  ImageEditorWidget (toolbar + canvas + side rect list)       |
    +--------------------------------------------------------------+
    |  [< Previous]  [Skip]  [Next >]            [Save and continue]|
    +--------------------------------------------------------------+

The editor is embedded directly: no modal dialog, no "open editor"
hop. The thumbnail strip lets the operator jump to any image at a
glance; the prev/next buttons walk through them sequentially.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anonymize.image_inventory import (
    ImageDecision,
    ImageInventory,
    ImageRedactions,
    InventoryImage,
    compute_image_id,
    load_decisions,
    load_inventory,
    save_decisions,
)

from .icons import icon
from .image_editor import ImageEditorWidget
from .theme import PALETTE


# Thumbnail strip dimensions. Keep modest so a project with 50+
# images stays scrollable horizontally without towering rows.
_STRIP_THUMB_SIZE = QSize(96, 96)
_STRIP_HEIGHT = 130


def _decision_label(decision: Optional[ImageDecision]) -> str:
    if decision is None:
        return "Not reviewed"
    if decision.decision == "skip":
        return "Skipped (passes through unchanged)"
    if decision.decision == "redact":
        n = len(decision.rects)
        return f"Will redact: {n} rect{'s' if n != 1 else ''}"
    return "Not reviewed"


class _ThumbnailStrip(QListWidget):
    """Horizontal scroll strip of clickable thumbnails."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(_STRIP_THUMB_SIZE)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(_STRIP_HEIGHT)
        self.setSpacing(6)


class ImageReviewPanel(QWidget):
    """Inline image-review experience.

    Sits inside the Review tab as a sibling of the text candidate
    panel. Disabled until the first Promote completes; the parent
    flips ``setEnabled(True)`` and calls :meth:`reload` once the
    inventory is fresh.
    """

    save_and_continue_requested = Signal()
    # Fires whenever the operator's in-memory image redactions
    # change in any way (rect added, moved, deleted, property
    # edited, or the active image swapped). The Build-preview panel
    # listens to this so the post-redaction render stays live with
    # what the operator is editing, even before they hit Save.
    decisions_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._inventory_path: Optional[Path] = None
        self._decisions_path: Optional[Path] = None
        self._inventory: ImageInventory = ImageInventory()
        self._decisions: ImageRedactions = ImageRedactions()
        # Ordered list of visible image_ids (governs prev / next).
        self._visible_ids: list[str] = []
        # Currently displayed image_id (None when nothing loaded).
        self._current_id: Optional[str] = None
        self._dirty: bool = False
        self._raw_cache: dict[str, bytes] = {}
        # Debounce live decisions_changed so the Build-preview pane
        # does not re-render after every mouse move while drawing /
        # dragging a rect.
        self._decisions_emit_timer = QTimer(self)
        self._decisions_emit_timer.setSingleShot(True)
        self._decisions_emit_timer.setInterval(250)
        self._decisions_emit_timer.timeout.connect(
            self.decisions_changed.emit
        )

        self._build_ui()

    # ---- public API ----

    def set_paths(
        self,
        inventory_path: Path,
        decisions_path: Path,
    ) -> None:
        self._inventory_path = inventory_path
        self._decisions_path = decisions_path
        self.reload()

    def current_decisions(self) -> ImageRedactions:
        """Return the in-memory image redactions, including any
        unsaved edits in the active editor canvas. Used by the
        Build-preview pane so it stays live with the operator."""
        # Pull the editor's rects into the in-memory map so the
        # snapshot mirrors what the operator currently sees.
        self._capture_current_into_decisions()
        return self._decisions

    def reload(self) -> None:
        if self._inventory_path is None or self._decisions_path is None:
            self._inventory = ImageInventory()
            self._decisions = ImageRedactions()
        else:
            self._inventory = load_inventory(self._inventory_path)
            self._decisions = load_decisions(self._decisions_path)
        self._raw_cache.clear()
        self._dirty = False
        self._rebuild_file_combo()
        self._rebuild_strip()
        if self._visible_ids:
            self._jump_to(self._visible_ids[0])
        else:
            self._show_empty_state()
        self._update_summary()

    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def save_to_disk(self) -> bool:
        """Capture in-memory editor state then write the YAML."""
        self._capture_current_into_decisions()
        if self._decisions_path is None:
            return False
        try:
            save_decisions(self._decisions_path, self._decisions)
        except Exception as e:
            from ._dismissible_dialog import dismissible_message
            dismissible_message(
                self,
                "warning",
                "Could not save image decisions",
                f"Writing {self._decisions_path} failed:\n{e}",
            )
            return False
        self._dirty = False
        return True

    # ---- UI construction ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # File filter row (multi-file projects).
        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(6)
        self._file_label = QLabel("File:")
        self._file_combo = QComboBox(self)
        self._file_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._file_combo.currentIndexChanged.connect(self._on_file_filter_changed)
        filter_row.addWidget(self._file_label)
        filter_row.addWidget(self._file_combo, 1)
        root.addLayout(filter_row)

        self._summary = QLabel("")
        self._summary.setStyleSheet(
            f"QLabel {{ color: {PALETTE['text_dim']}; padding: 2px 4px; }}"
        )
        root.addWidget(self._summary)

        self._strip = _ThumbnailStrip(self)
        self._strip.itemSelectionChanged.connect(self._on_strip_selection_changed)
        root.addWidget(self._strip)

        # Editor area inside a stacked widget so we can swap to an
        # empty-state placeholder when the project has no images
        # under the active filter.
        self._stack = QStackedWidget(self)
        self._editor = ImageEditorWidget(self)
        # Editor changes flag the panel as dirty so the parent knows
        # there is something to save before navigating away.
        self._editor.rects_changed.connect(self._on_editor_rects_changed)
        self._stack.addWidget(self._editor)

        self._empty_state = QLabel(
            "No embedded images detected for the current filter.\n"
            "Image redaction has nothing to do for this slice; "
            "you can move straight to the build preview."
        )
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)
        self._empty_state.setStyleSheet(
            f"QLabel {{ color: {PALETTE['text_dim']}; padding: 24px; }}"
        )
        self._stack.addWidget(self._empty_state)
        root.addWidget(self._stack, 1)

        # Bottom action bar: prev / skip / next on the left, save on
        # the right.
        bottom = QHBoxLayout()
        self._prev_btn = QPushButton(icon("history"), "  Previous")
        self._prev_btn.setToolTip("Previous image (Alt+Left)")
        self._prev_btn.clicked.connect(self._on_prev_clicked)
        self._skip_btn = QPushButton(icon("x"), "Skip current")
        self._skip_btn.setToolTip(
            "Mark this image as 'skip': it passes through to the "
            "output unchanged."
        )
        self._skip_btn.clicked.connect(self._on_skip_clicked)
        self._next_btn = QPushButton("Next  ")
        self._next_btn.setIcon(icon("history"))
        self._next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._next_btn.setToolTip("Next image (Alt+Right)")
        self._next_btn.clicked.connect(self._on_next_clicked)

        self._save_btn = QPushButton(icon("save"), "Save and continue")
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setToolTip(
            "Persist your image decisions and continue to the build "
            "preview, where you can confirm the final output before "
            "running apply / build / verify."
        )
        self._save_btn.clicked.connect(self._on_save_clicked)

        bottom.addWidget(self._prev_btn)
        bottom.addWidget(self._skip_btn)
        bottom.addWidget(self._next_btn)
        bottom.addStretch()
        bottom.addWidget(self._save_btn)
        root.addLayout(bottom)

        # Status indicator under the bottom row showing the current
        # image's position + decision pill.
        self._position_label = QLabel("")
        self._position_label.setStyleSheet(
            f"QLabel {{ color: {PALETTE['text_dim']}; padding: 2px 4px; }}"
        )
        root.addWidget(self._position_label)

        # Keyboard shortcuts: Alt+Left / Alt+Right.
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Alt+Left"), self, self._on_prev_clicked)
        QShortcut(QKeySequence("Alt+Right"), self, self._on_next_clicked)

    # ---- file filter ----

    def _rebuild_file_combo(self) -> None:
        prev = self._file_combo.currentData() if self._file_combo.count() else None
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        self._file_combo.addItem("All files", "")
        for f in self._inventory.files:
            label = Path(f.file).name or f.file
            n_imgs = len(f.images)
            self._file_combo.addItem(
                f"{label}  ({n_imgs} image{'s' if n_imgs != 1 else ''})",
                f.file,
            )
        if prev:
            idx = self._file_combo.findData(prev)
            if idx >= 0:
                self._file_combo.setCurrentIndex(idx)
        single_file_mode = len(self._inventory.files) <= 1
        self._file_combo.setVisible(not single_file_mode)
        self._file_label.setVisible(not single_file_mode)
        self._file_combo.blockSignals(False)

    def _on_file_filter_changed(self) -> None:
        # Persist current edits before swapping the visible set.
        self._capture_current_into_decisions()
        self._rebuild_strip()
        if self._visible_ids:
            self._jump_to(self._visible_ids[0])
        else:
            self._show_empty_state()
        self._update_summary()

    # ---- thumbnail strip ----

    def _rebuild_strip(self) -> None:
        self._strip.blockSignals(True)
        self._strip.clear()
        active_file = self._file_combo.currentData() if self._file_combo.count() else ""
        seen: set[str] = set()
        # Order images by file then by their position within the file
        # (page index for PDF, slide index for PPTX, etc.). The
        # original inventory list already follows that order, so we
        # just dedup by image_id within the visible slice.
        self._visible_ids = []
        for f in self._inventory.files:
            if active_file and f.file != active_file:
                continue
            for im in f.images:
                if im.image_id in seen:
                    continue
                seen.add(im.image_id)
                self._visible_ids.append(im.image_id)
                pix = self._load_strip_pixmap(im)
                li = QListWidgetItem(QIcon(pix) if pix is not None else icon("file"), "")
                li.setData(Qt.ItemDataRole.UserRole, im.image_id)
                # Tooltip carries the file + position so the operator
                # can identify the image without selecting it.
                li.setToolTip(self._strip_tooltip(f.file, im))
                # Status mark: a small ✓ in the label when a decision
                # exists, an asterisk for "in progress" (rects without
                # save).
                marker = self._decision_marker(im.image_id)
                li.setText(marker)
                li.setTextAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
                self._strip.addItem(li)
        self._strip.blockSignals(False)

    def _decision_marker(self, image_id: str) -> str:
        d = self._decisions.get(image_id)
        if d is None:
            return ""
        if d.decision == "redact" and d.rects:
            return "✓"
        if d.decision == "skip":
            return "—"
        return ""

    def _strip_tooltip(self, file_path: str, im: InventoryImage) -> str:
        bits = [Path(file_path).name or file_path]
        kind = im.location.kind
        if kind == "pdf" and im.location.page_index is not None:
            bits.append(f"page {im.location.page_index + 1}")
        elif kind == "pptx" and im.location.slide_index is not None:
            bits.append(f"slide {im.location.slide_index + 1}")
        if im.width and im.height:
            bits.append(f"{im.width} x {im.height}")
        decision = self._decisions.get(im.image_id)
        bits.append(_decision_label(decision))
        return "\n".join(bits)

    def _load_strip_pixmap(self, im: InventoryImage) -> Optional[QPixmap]:
        if not im.thumbnail or self._inventory_path is None:
            return None
        candidate = Path(im.thumbnail)
        if not candidate.is_absolute():
            candidate = self._inventory_path.parent / candidate
        if not candidate.exists():
            return None
        pix = QPixmap(str(candidate))
        if pix.isNull():
            return None
        # Scale to fit the strip's icon size while preserving aspect.
        return pix.scaled(
            _STRIP_THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _on_strip_selection_changed(self) -> None:
        items = self._strip.selectedItems()
        if not items:
            return
        image_id = items[0].data(Qt.ItemDataRole.UserRole)
        if image_id == self._current_id:
            return
        self._jump_to(image_id)

    def _jump_to(self, image_id: str) -> None:
        # Persist the previous image's edits before swapping.
        self._capture_current_into_decisions()
        raw = self._raw_bytes_for(image_id)
        if raw is None:
            self._show_load_failure(image_id)
            return
        occurrences = self._occurrences_of(image_id)
        decision = self._decisions.get(image_id)
        initial_rects = list(decision.rects) if (decision and decision.decision == "redact") else []
        meta = self._meta_for(image_id)
        fmt = (meta.format if meta else "png") or "png"
        self._editor.load_image(
            raw,
            initial_rects=initial_rects,
            image_id_short=image_id[:24],
            occurrence_count=len(occurrences),
            fmt_hint=fmt,
        )
        self._stack.setCurrentWidget(self._editor)
        self._current_id = image_id
        # Reflect the new selection back into the strip (in case the
        # caller invoked _jump_to programmatically, not via click).
        self._sync_strip_selection(image_id)
        self._refresh_position_label()
        self._update_nav_buttons()

    def _sync_strip_selection(self, image_id: str) -> None:
        self._strip.blockSignals(True)
        for i in range(self._strip.count()):
            li = self._strip.item(i)
            li.setSelected(li.data(Qt.ItemDataRole.UserRole) == image_id)
        # Make sure it's visible after a programmatic jump.
        for i in range(self._strip.count()):
            if self._strip.item(i).data(Qt.ItemDataRole.UserRole) == image_id:
                self._strip.scrollToItem(
                    self._strip.item(i),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                break
        self._strip.blockSignals(False)

    def _show_empty_state(self) -> None:
        self._stack.setCurrentWidget(self._empty_state)
        self._current_id = None
        self._refresh_position_label()
        self._update_nav_buttons()

    def _show_load_failure(self, image_id: str) -> None:
        self._stack.setCurrentWidget(self._empty_state)
        self._empty_state.setText(
            f"Could not load image bytes for\n{image_id[:32]}...\n"
            "Source file may have been moved or deleted; "
            "rerun the scan stage to refresh the inventory."
        )
        self._current_id = None
        self._refresh_position_label()
        self._update_nav_buttons()

    # ---- in-memory persistence between jumps ----

    def _capture_current_into_decisions(self) -> None:
        """Pull the editor's current rects into ``self._decisions``.

        Called BEFORE every navigation that swaps the visible image,
        so the operator's in-flight edits never get lost mid-session.
        Saving to disk is a separate explicit step (the Save button).
        """
        if self._current_id is None:
            return
        rects = self._editor.current_rects()
        if rects:
            d = self._decisions.decisions.get(self._current_id)
            if d is None or d.decision != "redact" or list(d.rects) != list(rects):
                meta = self._meta_for(self._current_id)
                self._decisions.decisions[self._current_id] = ImageDecision(
                    image_id=self._current_id,
                    decision="redact",
                    image_w=meta.width if meta else None,
                    image_h=meta.height if meta else None,
                    rects=rects,
                    edited_at=_now_iso(),
                )
                self._dirty = True
        else:
            existing = self._decisions.decisions.get(self._current_id)
            # If the operator removed every rect we treat that as
            # "no longer want a redaction here": drop the entry so
            # the strip marker resets to "not reviewed".
            if existing is not None and existing.decision == "redact":
                self._decisions.decisions.pop(self._current_id, None)
                self._dirty = True

    def _on_editor_rects_changed(self) -> None:
        # Mark dirty without committing yet; commit happens on
        # navigate or on save.
        self._dirty = True
        # Mirror the editor's current rects into the in-memory
        # decisions map RIGHT AWAY (not just on navigate / save) so
        # any consumer that reads ``current_decisions()`` sees the
        # operator's live state, including the Build-preview panel.
        self._capture_current_into_decisions()
        # Refresh the strip's marker for the current image so the ✓
        # appears as soon as the operator paints a rect.
        self._refresh_strip_marker(self._current_id)
        # Notify external listeners (debounced).
        self._decisions_emit_timer.start()

    def _refresh_strip_marker(self, image_id: Optional[str]) -> None:
        if image_id is None:
            return
        for i in range(self._strip.count()):
            li = self._strip.item(i)
            if li.data(Qt.ItemDataRole.UserRole) == image_id:
                # Use editor live state for the current image so the
                # check appears immediately on rect draw.
                if image_id == self._current_id and self._editor.has_rects():
                    li.setText("✓")
                else:
                    li.setText(self._decision_marker(image_id))
                break

    # ---- navigation buttons ----

    def _on_prev_clicked(self) -> None:
        if self._current_id is None or not self._visible_ids:
            return
        try:
            i = self._visible_ids.index(self._current_id)
        except ValueError:
            return
        if i <= 0:
            return
        self._jump_to(self._visible_ids[i - 1])

    def _on_next_clicked(self) -> None:
        if self._current_id is None or not self._visible_ids:
            if self._visible_ids:
                self._jump_to(self._visible_ids[0])
            return
        try:
            i = self._visible_ids.index(self._current_id)
        except ValueError:
            return
        if i >= len(self._visible_ids) - 1:
            return
        self._jump_to(self._visible_ids[i + 1])

    def _on_skip_clicked(self) -> None:
        if self._current_id is None:
            return
        self._editor.clear_rects()
        self._decisions.decisions[self._current_id] = ImageDecision(
            image_id=self._current_id,
            decision="skip",
            edited_at=_now_iso(),
        )
        self._dirty = True
        self._refresh_strip_marker(self._current_id)
        self._refresh_position_label()
        # Auto-advance to the next image so the operator can keep
        # triaging without reaching for the mouse.
        self._on_next_clicked()

    def _on_save_clicked(self) -> None:
        if not self.save_to_disk():
            return
        self.save_and_continue_requested.emit()

    def _update_nav_buttons(self) -> None:
        if not self._visible_ids or self._current_id is None:
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            self._skip_btn.setEnabled(False)
            return
        try:
            i = self._visible_ids.index(self._current_id)
        except ValueError:
            i = -1
        self._prev_btn.setEnabled(i > 0)
        self._next_btn.setEnabled(0 <= i < len(self._visible_ids) - 1)
        self._skip_btn.setEnabled(True)

    def _refresh_position_label(self) -> None:
        if not self._visible_ids or self._current_id is None:
            self._position_label.setText("")
            return
        try:
            i = self._visible_ids.index(self._current_id) + 1
        except ValueError:
            i = 0
        meta = self._meta_for(self._current_id)
        decision = self._decisions.get(self._current_id)
        bits = [f"Image {i} of {len(self._visible_ids)}"]
        if meta is not None:
            file_path = self._first_file_for(self._current_id)
            if file_path:
                bits.append(Path(file_path).name)
            kind = meta.location.kind
            if kind == "pdf" and meta.location.page_index is not None:
                bits.append(f"page {meta.location.page_index + 1}")
            elif kind == "pptx" and meta.location.slide_index is not None:
                bits.append(f"slide {meta.location.slide_index + 1}")
        bits.append(_decision_label(decision))
        self._position_label.setText("  ·  ".join(bits))

    def _update_summary(self) -> None:
        active_file = self._file_combo.currentData() if self._file_combo.count() else ""
        if active_file:
            files = [f for f in self._inventory.files if f.file == active_file]
            scope_label = f"in {Path(active_file).name}"
        else:
            files = list(self._inventory.files)
            n_files = len(files)
            scope_label = (
                f"across {n_files} file{'s' if n_files != 1 else ''}"
                if n_files else ""
            )
        n_total = sum(len(f.images) for f in files)
        unique_ids = {im.image_id for f in files for im in f.images}
        n_unique = len(unique_ids)
        n_redact = sum(
            1
            for d in self._decisions.decisions.values()
            if d.image_id in unique_ids and d.decision == "redact" and d.rects
        )
        n_skip = sum(
            1
            for d in self._decisions.decisions.values()
            if d.image_id in unique_ids and d.decision == "skip"
        )
        if n_total == 0:
            self._summary.setText(
                "No embedded images found. Image redaction has nothing "
                "to do for this project; you can move straight to the "
                "build preview."
            )
            return
        unread = n_unique - n_redact - n_skip
        self._summary.setText(
            f"{n_unique} image{'s' if n_unique != 1 else ''} "
            f"({n_total} occurrence{'s' if n_total != 1 else ''} {scope_label})  ·  "
            f"{n_redact} flagged to redact  ·  "
            f"{n_skip} skipped  ·  "
            f"{unread} not reviewed"
        )

    # ---- inventory helpers ----

    def _meta_for(self, image_id: str) -> Optional[InventoryImage]:
        for f in self._inventory.files:
            for im in f.images:
                if im.image_id == image_id:
                    return im
        return None

    def _first_file_for(self, image_id: str) -> Optional[str]:
        for f in self._inventory.files:
            for im in f.images:
                if im.image_id == image_id:
                    return f.file
        return None

    def _occurrences_of(self, image_id: str) -> list[tuple[str, InventoryImage]]:
        out: list[tuple[str, InventoryImage]] = []
        for f in self._inventory.files:
            for im in f.images:
                if im.image_id == image_id:
                    out.append((f.file, im))
        return out

    def _raw_bytes_for(self, image_id: str) -> Optional[bytes]:
        cached = self._raw_cache.get(image_id)
        if cached is not None:
            return cached
        from anonymize.format_adapters import get_adapter
        for f in self._inventory.files:
            if not any(im.image_id == image_id for im in f.images):
                continue
            file_path = Path(f.file)
            if not file_path.is_absolute() and self._inventory_path is not None:
                candidate = self._inventory_path.parent / file_path
                if candidate.exists():
                    file_path = candidate
            try:
                adapter = get_adapter(file_path)
            except Exception:
                continue
            try:
                raws = adapter.inventory_images(file_path)
            except Exception:
                continue
            for raw in raws:
                rid = compute_image_id(raw.raw_bytes)
                if rid == image_id:
                    self._raw_cache[image_id] = raw.raw_bytes
                    return raw.raw_bytes
        return None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["ImageReviewPanel"]
