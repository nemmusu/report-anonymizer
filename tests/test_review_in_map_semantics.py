"""Approve / Unapprove / Delete semantics for the Review pane.

These tests exercise the underlying state model (``AppState`` plus the
``Candidate.decision`` field, which is the in-map analog) without
requiring a full GUI tree. They guard the contract that:

* Approve  → ``decision == "approve"`` → counted as in-map.
* Unapprove → ``decision == "pending"`` → still visible, NOT in map.
* Delete    → removed from ``state.pending`` entirely.
* The build-time leak count only includes pending candidates that
  are NOT approved AND NOT already covered by the substitution map
  or the auto-promoted T0/T1 buckets.

Plus a Qt-headless test for the ``ReviewView`` that simulates the
full unapprove flow on an in-map row and verifies the row stays
visible (the previous bug silently dropped it).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_cand(value: str, decision: str = "pending"):
    from anonymize.candidates import Candidate

    return Candidate(
        value=value,
        category="brand",
        suggested_placeholder=f"{value.upper()}-X",
        confidence=0.9,
        rationale="t",
        count=1,
        examples=[value],
        tier="T1_llm",
        decision=decision,
    )


# ---------------------------------------------------------------------------
# AppState helpers (decision == "approve" is the in_map analog)
# ---------------------------------------------------------------------------


def test_set_included_toggles_pending_decision_flag(qapp):
    """``set_included(value, True)`` flips the matching pending
    candidate's decision to ``"approve"`` (= in_map). Setting it back
    to ``False`` returns it to ``"pending"`` without removing it from
    the list."""
    from gui.state import AppState

    state = AppState()
    a = _make_cand("acme.example", decision="pending")
    b = _make_cand("globex.example", decision="pending")
    state.set_candidates(pending=[a, b])

    assert state.set_included("acme.example", True) is True
    assert a.decision == "approve"
    # Sibling untouched.
    assert b.decision == "pending"
    # Idempotent: second True is a no-op (returns False, no signal).
    assert state.set_included("acme.example", True) is False

    # Toggling back keeps the row visible (still in pending) but
    # outside the map.
    assert state.set_included("acme.example", False) is True
    assert a.decision == "pending"
    assert a in state.pending


def test_iter_unhandled_leaks_excludes_approved_auto_and_mapped(qapp, tmp_path):
    """The leak count must skip:

    * pending candidates the operator approved (decision=="approve"),
    * candidates already covered by the substitution_map,
    * candidates queued in the auto-promoted T0/T1 buckets (which the
      next promote will fold into the map).
    """
    from anonymize.sub_map import SubstitutionMap
    from gui.state import AppState

    smap = SubstitutionMap.load(tmp_path / "substitution_map.yml")
    smap.add("brand", "alreadymapped.example", "MAPPED-1")

    state = AppState()
    state.smap = smap

    approved = _make_cand("approved.example", decision="approve")
    skipped = _make_cand("skipped.example", decision="skip")
    pending = _make_cand("pending.example", decision="pending")
    in_map = _make_cand("alreadymapped.example", decision="pending")
    state.set_candidates(
        auto_t0=[_make_cand("t0.example", decision="approve")],
        auto_t1=[_make_cand("t1.example", decision="approve")],
        pending=[approved, skipped, pending, in_map],
    )

    leaks = state.iter_unhandled_leaks()
    leak_values = sorted(c.value for c in leaks)
    assert leak_values == ["pending.example", "skipped.example"], (
        "leak count must include skipped + un-decided pending rows, "
        "and exclude approved / auto / already-mapped values"
    )


def test_set_included_returns_false_on_unknown_value(qapp):
    from gui.state import AppState

    state = AppState()
    state.set_candidates(pending=[_make_cand("a.example")])
    assert state.set_included("doesnotexist.example", True) is False


# ---------------------------------------------------------------------------
# ReviewView tree behaviour
# ---------------------------------------------------------------------------


def _make_view(qapp):
    from gui.review_view import ReviewView
    from gui.state import AppState

    state = AppState()
    view = ReviewView(state)
    return view, state


def test_unapprove_pending_resets_decision_and_keeps_row(qapp):
    """Approving a pending row then unapproving must NOT remove the
    row from the tree. The row stays visible and goes back to the
    default colour / pending decision (the bug previously hit
    in-map rows; this test pins the parallel pending case)."""
    from gui.review_view import _CandItem, _DECISION_APPROVE, _DECISION_PENDING

    view, state = _make_view(qapp)
    cand = _make_cand("leaky.example", decision=_DECISION_APPROVE)
    state.set_candidates(pending=[cand])

    cand_items = [
        view.tree.topLevelItem(0).child(j)
        for j in range(view.tree.topLevelItem(0).childCount())
    ]
    target = next(it for it in cand_items if isinstance(it, _CandItem))
    target.setSelected(True)

    # Run unapprove with the confirmation auto-accepted; we want to
    # exercise the actual code path including persistence guards.
    from PySide6.QtWidgets import QMessageBox

    with patch(
        "gui.review_view.dismissible_message",
        return_value=QMessageBox.StandardButton.Ok,
    ):
        view._unapprove_selected()

    assert target.decision == _DECISION_PENDING, (
        "approved row should reset to pending after Un-approve"
    )
    assert cand in state.pending, "row must NOT be removed from state"
    # Tree must still hold the row under the same parent.
    parent = view.tree.topLevelItem(0)
    assert any(parent.child(j) is target for j in range(parent.childCount()))
    view.close()


def test_unapprove_in_map_row_keeps_it_visible_as_pending(qapp, tmp_path):
    """In-map rows that the operator un-approves must:

    1. be removed from ``substitution_map.yml`` (so apply will not
       substitute them anymore), AND
    2. stay visible in the candidate tree as a pending row (so the
       operator can re-approve or delete them).
    """
    from anonymize.sub_map import SubstitutionMap
    from gui.review_view import _CandItem, _MapItem

    view, state = _make_view(qapp)
    smap = SubstitutionMap.load(tmp_path / "substitution_map.yml")
    smap.add("brand", "tobetoggled.example", "PLACEHOLDER-1")
    state.smap = smap
    state.map_changed.emit(smap)

    # Find the _MapItem in the tree.
    map_items: list[_MapItem] = []
    for ti in range(view.tree.topLevelItemCount()):
        top = view.tree.topLevelItem(ti)
        for ci in range(top.childCount()):
            ch = top.child(ci)
            if isinstance(ch, _MapItem):
                map_items.append(ch)
    assert map_items, "expected a _MapItem for the seeded map entry"
    target = map_items[0]
    target.setSelected(True)

    from PySide6.QtWidgets import QMessageBox

    with patch(
        "gui.review_view.dismissible_message",
        return_value=QMessageBox.StandardButton.Ok,
    ):
        view._unapprove_selected()

    # 1. Map drop.
    assert smap.find("tobetoggled.example") is None, (
        "un-approve on an in-map row must remove the entry from smap"
    )
    # 2. Re-staged in pending so the row stays visible.
    assert any(
        c.value == "tobetoggled.example" for c in state.pending
    ), "un-approved in-map row must reappear in state.pending"
    # And the tree contains a _CandItem for the recycled value.
    found = False
    for ti in range(view.tree.topLevelItemCount()):
        top = view.tree.topLevelItem(ti)
        for ci in range(top.childCount()):
            ch = top.child(ci)
            if (
                isinstance(ch, _CandItem)
                and ch.cand.value == "tobetoggled.example"
            ):
                found = True
                break
    assert found, "tree should hold a pending _CandItem for the recycled row"
    view.close()


def test_delete_removes_pending_from_state_entirely(qapp):
    """Delete is the "make it disappear" action: the row must drop
    out of ``state.pending`` so neither the tree nor the build map
    sees it again on the next refresh."""
    from gui.review_view import _CandItem

    view, state = _make_view(qapp)
    a = _make_cand("delete-me.example", decision="pending")
    b = _make_cand("keep-me.example", decision="pending")
    state.set_candidates(pending=[a, b])

    target = None
    for ti in range(view.tree.topLevelItemCount()):
        top = view.tree.topLevelItem(ti)
        for ci in range(top.childCount()):
            ch = top.child(ci)
            if isinstance(ch, _CandItem) and ch.cand.value == "delete-me.example":
                target = ch
                break
    assert target is not None
    target.setSelected(True)

    from PySide6.QtWidgets import QMessageBox

    with patch(
        "gui.review_view.dismissible_message",
        return_value=QMessageBox.StandardButton.Ok,
    ):
        view._delete_selected()

    pending_values = [c.value for c in state.pending]
    assert "delete-me.example" not in pending_values
    assert "keep-me.example" in pending_values
    view.close()


def test_skip_renders_in_default_colour_not_red(qapp):
    """Skip used to render rows in the error/red palette, blurring
    the "deselected → original colour" semantics. After Un-approve
    semantics landed, only ``approve`` carries colour; ``skip`` and
    ``pending`` share the default text colour."""
    from PySide6.QtCore import Qt
    from gui.review_view import _CandItem, _DECISION_APPROVE, _DECISION_SKIP
    from gui.theme import PALETTE

    view, state = _make_view(qapp)
    state.set_candidates(pending=[_make_cand("a.example", decision=_DECISION_SKIP)])
    target = view.tree.topLevelItem(0).child(0)
    assert isinstance(target, _CandItem)

    skip_color = target.foreground(1).color().name().lower()
    default_color = (PALETTE["text"]).lower()
    assert skip_color == default_color, (
        f"skip rows must render in default colour ({default_color}), "
        f"got {skip_color}"
    )

    target.decision = _DECISION_APPROVE
    target.cand.decision = _DECISION_APPROVE
    target._refresh_color()
    approve_color = target.foreground(1).color().name().lower()
    ok_color = (PALETTE["ok"]).lower()
    assert approve_color == ok_color, (
        f"approved rows must render in ok colour ({ok_color}), "
        f"got {approve_color}"
    )
    view.close()
