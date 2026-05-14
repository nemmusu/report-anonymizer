"""Smoke tests for the approval gate in MainWindow._on_stage_finished.

The full _run_all() chain is too heavy for an offscreen test (it needs a
project + workers); instead we validate the *gate logic* directly:

- The gate ALWAYS pauses after ``scan`` (even with 0 pending), so the
  user is the one who explicitly greenlights merge on **Approve & promote**.
- The **Approve & promote** card shows PAUSED / Approve & continue (not Scan).
- ``_on_promote_done`` resumes the queue and dedups the queued ``promote``
  entry to avoid running the same stage twice.
- ``_on_approve_continue`` (must pass ``key='promote'``) pops ``promote`` and
  starts the PromoteWorker / ``_run_stage('promote')``.
- ``_run_all`` queues ``promote`` as the first post-scan stage.
"""
from __future__ import annotations

import os
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


def _make_window(qapp):
    from gui.app import MainWindow
    w = MainWindow()
    # The headless test rig has no llama-server running; pretend it
    # is online so the new Run guards don't pop a blocking QMessageBox
    # that the offscreen platform plugin can't dismiss.
    try:
        w.state.set_server_online(True)
    except Exception:
        pass
    return w


def _wire_proj(proj, tmp_path):
    proj.map_path = tmp_path / "substitution_map.yml"
    proj.pending_path = tmp_path / "pending.yml"
    proj.applied_path = tmp_path / "applied.json"
    proj.verifier_report_path = tmp_path / "verifier.md"
    proj.decisions_path = tmp_path / "decisions.jsonl"
    proj.auto_t0_path = tmp_path / "auto_t0.yml"
    proj.auto_t1_path = tmp_path / "auto_t1.yml"
    return proj


def test_approval_gate_pauses_after_scan_when_pending(qapp, tmp_path):
    from anonymize.candidates import Candidate
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w.state.set_candidates(pending=[
        Candidate(
            value="acme.example",
            category="brand",
            suggested_placeholder="vendor.example",
            confidence=0.99,
            rationale="",
            count=1,
            examples=[],
            tier="T1_llm",
        )
    ])

    w._on_stage_finished("scan", True, "T1: 1", {})
    assert w._run_stage.called is False, "queue must NOT advance while paused"
    assert w._run_all_queue == ["promote", "apply", "build", "verify"]
    w.close()


def test_approval_gate_pauses_even_when_no_pending(qapp, tmp_path):
    """Even when pending == 0 AND auto_t0/t1 == 0 (the typical
    re-open of an already-fully-processed project), the gate must
    still pause at Review so the operator can confirm the existing
    map is what they want. Auto-skipping made re-runs feel
    unsupervised, which contradicts the human-in-the-loop guarantee
    the pipeline gives everywhere else.
    """
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w.state.set_candidates(pending=[])

    w._on_stage_finished("scan", True, "T1: 0", {})
    # The queue must NOT advance: the gate held it for the operator.
    w._run_stage.assert_not_called()
    assert w._run_all_queue == ["promote", "apply", "build", "verify"]
    w.close()


def test_approve_continue_resumes_queue(qapp, tmp_path):
    """Clicking 'Approve & continue' on the Approve & promote card runs promote."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w._on_approve_continue("promote")
    w._run_stage.assert_called_once_with("promote", from_queue=True)
    assert w._run_all_queue == ["apply", "build", "verify"]
    w.close()


def test_approve_continue_wrong_key_is_ignored(qapp, tmp_path):
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w._on_approve_continue("scan")
    w._run_stage.assert_not_called()
    assert w._run_all_queue == ["promote", "apply", "build", "verify"]
    w.close()


def test_approve_continue_actually_starts_promote_worker(qapp, tmp_path):
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_all_queue = ["promote", "apply", "build", "verify"]
    FakeWorker = MagicMock()
    with patch("gui.app.PromoteWorker", FakeWorker):
        w._on_approve_continue("promote")
    FakeWorker.assert_called_once()
    args, kwargs = FakeWorker.call_args
    assert args[0] is proj
    assert args[1] is None
    w.close()


def test_run_all_promote_to_apply_chain(qapp, tmp_path):
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["apply", "build", "verify"]
    w._on_stage_finished("promote", True, "merged", {})
    w._run_stage.assert_called_once_with("apply", from_queue=True)
    assert w._run_all_queue == ["build", "verify"]
    w.close()


def test_manual_apply_blocked_while_paused(qapp, tmp_path):
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w._enter_paused_state()
    apply_card = w.pipeline_view.card("apply")
    assert apply_card is not None
    assert apply_card.run_btn.isEnabled() is False
    w.close()


def test_promote_done_defers_run_all_queue_to_build_button(qapp, tmp_path):
    """A successful Promote (whether reached via Run-all or by clicking
    'Promote & build' directly) must route the operator into the
    image-review / build-preview flow, NOT auto-continue the queue.
    The Build button on the preview tab is the single explicit
    'commit to disk' gate, so any leftover apply / build / verify
    tail from a Run-all is dropped here on purpose; clicking Build
    rebuilds the canonical queue from project flags."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = ["promote", "apply", "build", "verify"]
    w._on_promote_done(True, "ok", approved=[])
    # No stage runs: the operator owns the Build trigger now.
    w._run_stage.assert_not_called()
    # And the queue is wiped so a stale tail doesn't fire later.
    assert w._run_all_queue == []
    w.close()


def test_promote_done_routes_to_review_tabs_not_apply(qapp, tmp_path):
    """Standalone Promote (no Run-all queue) must NOT auto-run
    apply: the new flow is text-review -> image-review -> build
    preview -> Build, where Build is the only explicit "commit to
    disk" gate. Auto-applying behind the operator's back would skip
    the image-review and build-preview stages."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = []
    w._on_promote_done(True, "ok", approved=[])
    # No apply runs: the operator owns the Build trigger now.
    w._run_stage.assert_not_called()
    # And the queue is left empty so a stray _on_stage_finished does
    # not start advancing through ghost stages.
    assert w._run_all_queue == []
    w.close()


def test_build_requested_runs_apply_build_verify(qapp, tmp_path):
    """Clicking Build on the Build-preview tab is the explicit
    "commit to disk" gate: it enqueues apply / build / verify (plus
    auto_resolve when the project flag is on) and starts the queue
    immediately."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all_queue = []
    w._on_build_requested()
    w._run_stage.assert_called_once_with("apply", from_queue=True)
    assert "build" in w._run_all_queue and "verify" in w._run_all_queue
    w.close()


def test_auto_resolve_runs_even_after_partial_failure(qapp, tmp_path):
    """Verify and auto_resolve are deterministic housekeeping passes
    that must always close the pipeline. Even if an earlier stage set
    ``_all_failed=True``, the verify→auto_resolve tail must still
    drain, otherwise the user is stuck with an unfinished cleanup
    and has to click manually for residual leaks the auto-resolve
    loop would have caught.
    """
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    # Some upstream stage already failed (e.g. build hit a partial
    # error) so the gate is armed.
    w._all_failed = True
    w._run_all_queue = ["auto_resolve"]
    w._on_stage_finished("verify", True, "0 residual leaks", {})
    # The gate must let auto_resolve through, exactly like verify.
    w._run_stage.assert_called_once_with("auto_resolve", from_queue=True)
    assert w._run_all_queue == []
    w.close()


def test_run_all_resets_failed_flag(qapp, tmp_path):
    """A new Run-all must start with a clean slate so a previous
    aborted run can't poison the queue gate."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._all_failed = True
    w._run_stage = MagicMock()
    w._run_all()
    assert w._all_failed is False
    w.close()


def test_run_all_queue_includes_promote(qapp, tmp_path):
    """Run all must enqueue 'promote' as the first post-scan stage."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    w._run_stage = MagicMock()
    w._run_all()
    # The stage launched is 'scan' (the queue holds what runs AFTER scan).
    w._run_stage.assert_called_once_with("scan", from_queue=True)
    # ``auto_resolve`` only appears when ``project.auto_resolve_residuals``
    # is enabled (default True). The contract under test is that
    # ``promote`` is always first and that ``apply`` follows it; the
    # tail of the queue depends on the project flags.
    assert w._run_all_queue[:3] == ["promote", "apply", "build"], (
        "Run all must include promote so newly-discovered candidates "
        "actually land in substitution_map.yml before apply."
    )
    assert "verify" in w._run_all_queue
    w.close()
