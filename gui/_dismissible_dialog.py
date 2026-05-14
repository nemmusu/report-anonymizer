"""Click-outside-to-dismiss + lose-focus-to-dismiss helpers.

Users complained that the modal popups in the GUI feel "invadenti"
(invasive): the only way to make them go away is to find the right
button or hit the (small) window X. This module bolts a uniform
"dismissible" behaviour onto any :class:`QDialog` / :class:`QMessageBox`
instance: a single mouse press anywhere outside the dialog's geometry
(or the application losing focus, or pressing Escape) closes the
dialog as if the user had clicked the safe-default button.

Two integration patterns
========================

1. ``make_dismissible(dialog)``: attaches the behaviour to an existing
   :class:`QDialog` instance the caller has constructed manually
   (typical for ``QMessageBox`` subclasses or any custom QDialog with
   buttons / forms). The helper:

   * sets ``Qt.WA_DeleteOnClose`` so memory is reclaimed on close,
   * installs an application-level event filter, parented to the
     dialog so it auto-cleans up when the dialog dies,
   * dismisses on outside-click and on application-deactivate.

2. ``dismissible_message(parent, kind, ...)``: a thin wrapper around
   the static ``QMessageBox.warning`` / ``information`` / ``critical``
   / ``question`` family that constructs a real ``QMessageBox``
   instance, applies :func:`make_dismissible`, executes it, and
   returns the standard button code. Use this everywhere the codebase
   currently calls ``QMessageBox.warning(self, ...)`` etc. so the
   dismiss behaviour is uniform.

Implementation notes
====================

We deliberately do NOT use ``Qt.WindowType.Popup`` because that hides
the window-frame chrome (title, close button, drop shadow) and the
leak-alert dialog needs a real titled chrome with a clear question +
two answer buttons. Instead the event filter approach keeps the
standard ``Qt.WindowType.Dialog`` chrome.

Outside clicks call :meth:`QDialog.reject` (i.e. "user said no"). For
the leak alert the safe default is "Back to review", which is wired
to the reject role, so an outside-click correctly maps to "do not
build". Callers that need the opposite semantic (dismiss = OK, e.g.
for an info-only popup) can pass ``dismiss_action="accept"`` to
:func:`make_dismissible`.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget


_DISMISS_ACCEPT = "accept"
_DISMISS_REJECT = "reject"


def _global_pos(event: QMouseEvent) -> QPoint:
    """Qt 5/6 compatibility: ``globalPos()`` was renamed to
    ``globalPosition()`` (returns ``QPointF``). Try the modern API
    first so we keep working on Qt 6 without a deprecation warning."""
    try:
        gp = event.globalPosition()
    except AttributeError:
        return event.globalPos()  # type: ignore[no-any-return]
    return gp.toPoint()


class _DismissFilter(QObject):
    """Application-level event filter that closes ``dialog`` when the
    user presses a mouse button outside its geometry, or when the
    parent application/window loses focus.

    The filter is parented to ``dialog`` so its lifetime is bounded
    by the dialog's lifetime: when Qt deletes the dialog (via
    ``WA_DeleteOnClose`` or normal cleanup) the filter is also
    deleted, which automatically removes it from the application's
    event-filter list. We additionally hook ``destroyed`` as a
    belt-and-braces guard.
    """

    def __init__(self, dialog: QDialog, dismiss_action: str) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._dismiss_action = dismiss_action
        self._dismissed = False
        # Filter mouse events at the app level so we see clicks that
        # happen on widgets behind the dialog (or on the dialog's own
        # children, which we then let through).
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        try:
            # Use a lambda to keep PySide6 from trying to resolve the
            # slot at the C++ level (where it would warn "Slot not
            # found" because Python-level methods aren't registered as
            # Qt slots). The lambda forwards to the Python method.
            dialog.destroyed.connect(lambda *_: self._on_dialog_destroyed())
        except Exception:
            pass

    def _on_dialog_destroyed(self, *_: object) -> None:
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        self._dialog = None  # type: ignore[assignment]

    def _close_dialog(self) -> None:
        if self._dismissed:
            return
        dlg = getattr(self, "_dialog", None)
        if dlg is None:
            return
        # Avoid re-entrancy: a reject() that triggers another event
        # (e.g. focus change) must not loop back into _close_dialog.
        self._dismissed = True
        try:
            if self._dismiss_action == _DISMISS_ACCEPT and hasattr(dlg, "accept"):
                dlg.accept()
            elif hasattr(dlg, "reject"):
                dlg.reject()
            else:
                dlg.close()
        except Exception:
            try:
                dlg.close()
            except Exception:
                pass

    def _is_inside_dialog(self, global_pt: QPoint) -> bool:
        dlg = getattr(self, "_dialog", None)
        if dlg is None:
            return False
        try:
            visible = dlg.isVisible()
        except (RuntimeError, AttributeError):
            return False
        if not visible:
            return False
        # Use frameGeometry so the title bar / borders also count as
        # "inside". Some popups (e.g. QMessageBox) draw informative
        # text right up against the frame edge; using geometry()
        # alone would treat clicks on the border as "outside" and
        # dismiss prematurely.
        top_left = dlg.mapToGlobal(QPoint(0, 0))
        size = dlg.frameGeometry().size()
        return QRect(top_left, size).contains(global_pt)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: D401
        dlg = getattr(self, "_dialog", None)
        if dlg is None or self._dismissed:
            return False
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            try:
                pt = _global_pos(event)  # type: ignore[arg-type]
            except Exception:
                return False
            if not self._is_inside_dialog(pt):
                self._close_dialog()
                # We do NOT swallow the event so the underlying widget
                # also sees the click (matches the platform convention
                # for non-modal popups). For modal dialogs Qt usually
                # routes clicks back to the dialog only, so this is
                # mostly defensive.
                return False
        elif et == QEvent.Type.ApplicationDeactivate:
            # User Alt-Tabbed away or clicked another app: dismiss so
            # they don't come back to a stale modal.
            self._close_dialog()
        return False


def make_dismissible(
    dialog: QDialog, *, dismiss_action: str = _DISMISS_REJECT
) -> _DismissFilter:
    """Attach click-outside / lose-focus auto-dismiss to ``dialog``.

    ``dismiss_action`` controls whether an outside click maps to
    :meth:`QDialog.reject` (the default, safe for "are you sure?"
    confirmations) or :meth:`QDialog.accept` (for info-only dialogs
    where dismissing means "I read it").

    Returns the installed event-filter object. The caller usually
    does not need to keep a reference to it; it is parented to the
    dialog and cleaned up automatically.
    """
    if dismiss_action not in (_DISMISS_ACCEPT, _DISMISS_REJECT):
        raise ValueError(
            f"dismiss_action must be 'accept' or 'reject', got {dismiss_action!r}"
        )
    try:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    return _DismissFilter(dialog, dismiss_action)


# ---- QMessageBox convenience wrappers ------------------------------------

_KIND_TO_ICON = {
    "info": QMessageBox.Icon.Information,
    "information": QMessageBox.Icon.Information,
    "warning": QMessageBox.Icon.Warning,
    "warn": QMessageBox.Icon.Warning,
    "critical": QMessageBox.Icon.Critical,
    "error": QMessageBox.Icon.Critical,
    "question": QMessageBox.Icon.Question,
}


def dismissible_message(
    parent: Optional[QWidget],
    kind: str,
    title: str,
    text: str,
    *,
    buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
    default_button: Optional[QMessageBox.StandardButton] = None,
    dismiss_action: str = _DISMISS_REJECT,
    dismissible: bool = True,
) -> QMessageBox.StandardButton:
    """Drop-in replacement for ``QMessageBox.warning`` / ``information``
    / ``critical`` / ``question`` static methods that adds
    click-outside / focus-loss dismissal.

    The return value matches what the corresponding static method
    would have returned (a ``QMessageBox.StandardButton`` value).

    ``dismissible`` (default ``True``) controls whether the popup can
    be closed by clicking outside it / Alt-Tabbing away. Pass
    ``dismissible=False`` for confirmation modals that gate a
    destructive action (Delete, Unapprove, Reset, etc.): silently
    treating an accidental outside-click as Cancel makes the user
    perceive the action as broken ("I clicked Delete and nothing
    happened"). Non-dismissible popups can still be closed via the
    Cancel button, the X button, or Escape, but require an explicit
    user gesture so destructive operations don't quietly fail.
    """
    icon = _KIND_TO_ICON.get(kind.lower(), QMessageBox.Icon.NoIcon)
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default_button is not None:
        box.setDefaultButton(default_button)
    # Capture the clicked standard button via signal BEFORE the dialog
    # is closed and (when ``dismissible`` is True) before
    # ``WA_DeleteOnClose`` schedules its destruction. Reading
    # ``box.clickedButton()`` after ``exec()`` returned was racy on
    # PySide6: the underlying C++ widget could already be torn down,
    # ``clickedButton()`` would return ``None`` and the helper would
    # silently downgrade an "Open" / "Yes" choice to the Cancel
    # fallback, which made buttons in confirmation dialogs feel dead
    # ("I clicked Open and nothing happened").
    captured: list[QMessageBox.StandardButton] = []

    def _on_click(btn_obj):
        try:
            std = box.standardButton(btn_obj)
        except Exception:
            return
        if std and std != QMessageBox.StandardButton.NoButton:
            captured.append(std)

    try:
        box.buttonClicked.connect(_on_click)
    except Exception:
        pass
    if dismissible:
        make_dismissible(box, dismiss_action=dismiss_action)
    box.exec()
    btn = captured[0] if captured else None
    if btn is None:
        # User dismissed via outside-click / Escape / focus loss.
        # Map to a "no answer" sentinel that mirrors what the static
        # QMessageBox.* methods return when reject() fires: usually
        # the cancel/no role of the buttons set, or NoButton if none
        # of the buttons match a reject role.
        if QMessageBox.StandardButton.Cancel & buttons:
            return QMessageBox.StandardButton.Cancel
        if QMessageBox.StandardButton.No & buttons:
            return QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.NoButton
    return btn


__all__ = [
    "make_dismissible",
    "dismissible_message",
]
