"""Regression: the post-promote Build click must produce AT MOST one
"Potential leaks detected" dialog regardless of how many files the
project owns.

The user reported a popup storm: after a Run all completed with one
residual leak, clicking Build floor-flooded the main window with
modal popups. The fix moves the leak-confirmation gate to a single
pre-pipeline check on the Build button, BEFORE any per-file work
starts; this test pins that contract by counting how many times the
confirm dialog factory is invoked for a project with many input
paths.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _wire_proj(proj, tmp_path):
    proj.map_path = tmp_path / "substitution_map.yml"
    proj.pending_path = tmp_path / "pending.yml"
    proj.applied_path = tmp_path / "applied.json"
    proj.verifier_report_path = tmp_path / "verifier.md"
    proj.decisions_path = tmp_path / "decisions.jsonl"
    proj.auto_t0_path = tmp_path / "auto_t0.yml"
    proj.auto_t1_path = tmp_path / "auto_t1.yml"
    return proj


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


def _teardown_window(qapp, w) -> None:
    """Drop the MainWindow and force Qt to actually destroy its
    child QTimers (notably ServerPanel's health-poll timer) so they
    don't fire during a later test's ``qapp.processEvents()``.
    Stopping the timer explicitly is required because Qt 6 defers
    widget deletion: a plain ``close()`` leaves the timer alive
    until the next event-loop iteration, and the ``_poll`` callback
    blocks on a 1s ``requests.get`` that stalls for much longer on
    some Windows network stacks."""
    try:
        w.server_panel._timer.stop()
    except Exception:
        pass
    try:
        w.close()
    except Exception:
        pass
    try:
        w.deleteLater()
    except Exception:
        pass
    qapp.processEvents()
    qapp.processEvents()


def test_build_dialog_fires_once_per_click_with_many_files(qapp, tmp_path):
    """Multi-file folder project + many pending leaks → exactly one
    confirm-dialog invocation per Build click (not one per file, not
    one per leak). This is the pinned regression contract for the
    popup-storm bug."""
    from anonymize.project import Project
    from gui.app import MainWindow

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Synthesize 12 input files; the bug used to spawn one popup per
    # file in the worker emit path. We assert here that the modern
    # code path is N-independent.
    for n in range(12):
        (in_dir / f"f{n}.md").write_text(f"sample {n}\n", encoding="utf-8")

    w = MainWindow()
    try:
        w.state.set_server_online(True)
    except Exception:
        pass
    proj = _wire_proj(Project.for_folder(in_dir, out_dir), tmp_path)
    w.state.set_project(proj)
    w.state.set_candidates(pending=[
        _make_cand(f"leaky-{i}.example", decision="pending") for i in range(7)
    ])

    w._run_stage = MagicMock()
    confirm = MagicMock(return_value=True)
    w._confirm_build_with_leaks = confirm

    w._on_build_requested()

    assert confirm.call_count == 1, (
        f"expected exactly ONE confirm-dialog invocation per Build "
        f"click, got {confirm.call_count}. The popup-storm "
        f"regression has resurfaced."
    )
    _teardown_window(qapp, w)


def test_build_dialog_count_is_not_n_pending(qapp, tmp_path):
    """Even with N pending leaks the dialog must still fire only ONCE
    -- the count is shown in the dialog's body, not by spawning N
    dialogs."""
    from anonymize.project import Project
    from gui.app import MainWindow

    w = MainWindow()
    try:
        w.state.set_server_online(True)
    except Exception:
        pass
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    proj = _wire_proj(Project.for_folder(in_dir, out_dir), tmp_path)
    w.state.set_project(proj)
    n_leaks = 31
    w.state.set_candidates(pending=[
        _make_cand(f"leak-{i}.example", decision="pending") for i in range(n_leaks)
    ])

    confirm = MagicMock(return_value=False)
    w._confirm_build_with_leaks = confirm
    w._run_stage = MagicMock()
    w.review_view.focus_first_leak = MagicMock()

    w._on_build_requested()

    confirm.assert_called_once_with(n_leaks, mode="unreviewed")
    _teardown_window(qapp, w)
