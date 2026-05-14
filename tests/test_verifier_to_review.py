"""Verifier residual hits round-trip into the Review queue.

The user can hit "Send selected to Review" or "Send all to Review" in
the Verifier view; ``MainWindow._on_hits_to_pending`` then turns each
:class:`anonymize.verifier.LeakHit` into a Tier-3 Candidate so it lands
in ``needs_review.yml`` and shows up in the Review tree.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _hit(file_, pattern, match, snippet=""):
    from anonymize.verifier import LeakHit
    return LeakHit(file=file_, pattern=pattern, match=match, snippet=snippet)


def test_hits_become_pending_candidates(qapp, tmp_path):
    from anonymize.project import Project
    from gui.app import MainWindow

    w = MainWindow()
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    proj.pending_path = tmp_path / "needs_review.yml"
    w.state.set_project(proj)
    w.state.set_candidates(pending=[])

    hits = [
        _hit("a.md", "italian_mobile", "+393337310009", "Phone: +393337310009"),
        _hit("b.md", "hex_credentials_64", "deadbeef" * 8),
    ]
    w._on_hits_to_pending(hits)

    values = {c.value for c in w.state.pending}
    assert "+393337310009" in values
    assert "deadbeef" * 8 in values
    cats = {c.value: c.category for c in w.state.pending}
    assert cats["+393337310009"] == "phones"
    assert cats["deadbeef" * 8] == "keys"
    tiers = {c.tier for c in w.state.pending}
    assert tiers == {"T3_verifier"}
    assert proj.pending_path.exists(), "pending list must be persisted to disk"
    w.close()


def test_dedup_already_pending_values(qapp, tmp_path):
    from anonymize.candidates import Candidate
    from anonymize.project import Project
    from gui.app import MainWindow

    w = MainWindow()
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    proj.pending_path = tmp_path / "needs_review.yml"
    w.state.set_project(proj)
    w.state.set_candidates(
        pending=[
            Candidate(
                value="+393337310009",
                category="phones",
                suggested_placeholder="",
                confidence=0.9,
                rationale="",
                count=1,
                examples=[],
                tier="T1_llm",
            )
        ]
    )

    hits = [
        _hit("a.md", "italian_mobile", "+393337310009"),  # duplicate
        _hit("b.md", "italian_mobile", "+393440411155"),  # new
    ]
    w._on_hits_to_pending(hits)

    values = [c.value for c in w.state.pending]
    assert values.count("+393337310009") == 1
    assert "+393440411155" in values
    w.close()


def test_send_to_review_signal_passes_selected_hits(qapp, tmp_path):
    """The view emits the original LeakHit objects (not just text)."""
    from anonymize.verifier import VerifierReport
    from gui.verifier_view import VerifierView
    from gui.state import AppState

    state = AppState()
    view = VerifierView(state)
    report = VerifierReport(
        files_scanned=1,
        pdfs_scanned=0,
        hits=[
            _hit("a.md", "italian_mobile", "+393337310009"),
            _hit("b.md", "italian_mobile", "+393501800754"),
        ],
    )
    state.set_verifier_report(report)

    # Select the first row programmatically.
    view.table.selectRow(0)
    captured = []
    view.send_to_review_requested.connect(lambda hits: captured.append(hits))
    view._send_selected()
    assert captured, "signal must fire when there is a selection"
    sent = captured[0]
    assert len(sent) == 1
    assert sent[0].match == "+393337310009"


def test_send_selected_with_no_selection_shows_toast(qapp, tmp_path):
    from unittest.mock import patch

    from anonymize.verifier import VerifierReport
    from gui.verifier_view import VerifierView
    from gui.state import AppState

    state = AppState()
    view = VerifierView(state)
    report = VerifierReport(
        files_scanned=1,
        pdfs_scanned=0,
        hits=[
            _hit("a.md", "italian_mobile", "+393337310009"),
        ],
    )
    state.set_verifier_report(report)

    with patch("gui.verifier_view.Toaster.notify") as notify:
        view._send_selected()
    notify.assert_called_once()
    call_kw = notify.call_args.kwargs
    assert call_kw.get("kind") == "warn"
