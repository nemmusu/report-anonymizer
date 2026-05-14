"""Tests for :mod:`gui._dismissible_dialog`.

Goal: verify that the click-outside / focus-loss / Escape-to-dismiss
helper actually closes the dialog and reaps it via
``Qt.WA_DeleteOnClose`` -- without breaking the existing
button-click semantics. We exercise the event filter directly with
synthetic ``QMouseEvent`` and ``QEvent`` instances; the offscreen Qt
platform plugin is enough to construct the widgets.

If PySide6 / Qt cannot be initialized (e.g. on a CI runner missing
the offscreen plugin) the entire module is skipped, never failed.
"""
from __future__ import annotations

import os
import sys

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


def _qt_available() -> bool:
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _qt_available(),
    reason="PySide6 (offscreen) not available in this environment",
)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_mouse_event(global_x: int, global_y: int):
    """Construct a ``QMouseEvent`` at a known global position. Qt 6
    requires both a local ``QPointF`` and a global ``QPointF``; we
    pin the local one to (0, 0) since the filter does not look at
    it."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(0, 0),
        QPointF(global_x, global_y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_outside_click_calls_reject(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QDialog
    from gui._dismissible_dialog import make_dismissible

    dlg = QDialog()
    dlg.resize(120, 80)
    dlg.move(100, 100)
    dlg.show()
    qapp.processEvents()
    filt = make_dismissible(dlg, dismiss_action="reject")

    rejected: dict[str, bool] = {"called": False}
    dlg.rejected.connect(lambda: rejected.update(called=True))

    # Click at (5000, 5000), well outside the dialog's geometry.
    evt = _make_mouse_event(5000, 5000)
    filt.eventFilter(qapp, evt)
    qapp.processEvents()
    assert rejected["called"] is True, (
        "an outside-click must trigger reject() on the dialog"
    )


def test_inside_click_does_not_close(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QDialog
    from gui._dismissible_dialog import make_dismissible

    dlg = QDialog()
    dlg.resize(200, 120)
    dlg.move(50, 60)
    dlg.show()
    qapp.processEvents()
    filt = make_dismissible(dlg, dismiss_action="reject")

    rejected: dict[str, bool] = {"called": False}
    dlg.rejected.connect(lambda: rejected.update(called=True))

    # Compute a point that is definitely inside the dialog.
    top_left_global = dlg.mapToGlobal(QPoint(0, 0))
    inside = top_left_global + QPoint(10, 10)
    evt = _make_mouse_event(inside.x(), inside.y())
    filt.eventFilter(qapp, evt)
    qapp.processEvents()
    assert rejected["called"] is False, (
        "a click INSIDE the dialog geometry must NOT close it"
    )
    dlg.close()


def test_escape_key_closes_dialog(qapp):
    """QDialog already maps Esc to reject(); we just sanity-check
    the contract holds when the dialog is dismissible (i.e. the
    helper has not silently disabled the default key handling)."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QDialog
    from gui._dismissible_dialog import make_dismissible

    dlg = QDialog()
    dlg.show()
    qapp.processEvents()
    make_dismissible(dlg, dismiss_action="reject")

    rejected: dict[str, bool] = {"called": False}
    dlg.rejected.connect(lambda: rejected.update(called=True))

    key = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(dlg, key)
    qapp.processEvents()
    assert rejected["called"] is True


def test_wa_delete_on_close_is_set(qapp):
    """``make_dismissible`` must set ``WA_DeleteOnClose`` so the
    dialog is reaped after close, not leaked."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    from gui._dismissible_dialog import make_dismissible

    dlg = QDialog()
    make_dismissible(dlg)
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose), (
        "make_dismissible must set WA_DeleteOnClose so the dialog is "
        "scheduled for deletion when it closes"
    )
    dlg.close()


def test_dismissible_message_returns_clicked_button(qapp, monkeypatch):
    """End-to-end smoke check: ``dismissible_message`` mirrors
    ``QMessageBox.information`` semantics -- click ``Ok`` and the
    function returns ``QMessageBox.StandardButton.Ok``."""
    from PySide6.QtWidgets import QMessageBox
    from gui._dismissible_dialog import dismissible_message

    captured: dict[str, object] = {}

    def fake_exec(self):
        # Simulate "user clicked the Ok button" by invoking the
        # helper that QMessageBox uses to mark the answer.
        for b in self.buttons():
            if self.standardButton(b) == QMessageBox.StandardButton.Ok:
                self.setResult(QMessageBox.DialogCode.Accepted)
                # Mimic clickedButton(); QMessageBox stores the last
                # button via setClickedButton internally on click(),
                # so this is the closest we can get without a real
                # event loop.
                b.click()
                break
        captured["ran"] = True
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    rv = dismissible_message(
        None,
        "information",
        "test",
        "hello",
        buttons=QMessageBox.StandardButton.Ok,
    )
    assert captured.get("ran") is True
    assert rv == QMessageBox.StandardButton.Ok


def test_filter_dismiss_is_idempotent(qapp):
    """Two outside-clicks in a row must not raise (re-entrancy guard)."""
    from PySide6.QtWidgets import QDialog
    from gui._dismissible_dialog import make_dismissible

    dlg = QDialog()
    dlg.resize(80, 50)
    dlg.move(20, 20)
    dlg.show()
    qapp.processEvents()
    filt = make_dismissible(dlg, dismiss_action="reject")

    evt1 = _make_mouse_event(9000, 9000)
    evt2 = _make_mouse_event(9001, 9001)
    filt.eventFilter(qapp, evt1)
    filt.eventFilter(qapp, evt2)
    qapp.processEvents()


def test_dismissible_message_with_dismissible_false_does_not_install_filter(
    qapp, monkeypatch
):
    """``dismissible=False`` must skip the click-outside filter so an
    accidental focus loss / outside click on a destructive-action
    confirmation modal cannot silently map to Cancel.

    We monkeypatch ``make_dismissible`` to a sentinel that records
    whether it was invoked, and force the QMessageBox to close via a
    Cancel click during ``exec()`` so the helper returns synchronously.
    """
    from PySide6.QtWidgets import QMessageBox
    from gui import _dismissible_dialog as ddmod

    calls: dict[str, int] = {"count": 0}

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(ddmod, "make_dismissible", _spy)

    def fake_exec(self):
        for b in self.buttons():
            if self.standardButton(b) == QMessageBox.StandardButton.Cancel:
                self.setResult(QMessageBox.DialogCode.Rejected)
                b.click()
                break
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    rv = ddmod.dismissible_message(
        None,
        "question",
        "Confirm",
        "Are you sure?",
        buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        default_button=QMessageBox.StandardButton.Cancel,
        dismissible=False,
    )

    assert calls["count"] == 0, (
        "make_dismissible must NOT be called when dismissible=False; "
        "destructive-action confirmations must require an explicit "
        "Cancel/X/Esc gesture so silent dismiss never masquerades as "
        "Cancel"
    )
    assert rv == QMessageBox.StandardButton.Cancel


def test_dismissible_message_default_true_installs_filter(qapp, monkeypatch):
    """Sanity guard for the inverse direction: the default behaviour
    (``dismissible`` omitted) keeps installing the click-outside
    filter, so non-destructive popups (e.g. the Build leak alert)
    keep their friendly outside-click-to-dismiss UX."""
    from PySide6.QtWidgets import QMessageBox
    from gui import _dismissible_dialog as ddmod

    calls: dict[str, int] = {"count": 0}

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(ddmod, "make_dismissible", _spy)

    def fake_exec(self):
        for b in self.buttons():
            if self.standardButton(b) == QMessageBox.StandardButton.Ok:
                self.setResult(QMessageBox.DialogCode.Accepted)
                b.click()
                break
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    rv = ddmod.dismissible_message(
        None,
        "information",
        "Heads up",
        "FYI",
        buttons=QMessageBox.StandardButton.Ok,
    )

    assert calls["count"] == 1, (
        "make_dismissible must be called exactly once when dismissible "
        "is left at its default of True"
    )
    assert rv == QMessageBox.StandardButton.Ok
