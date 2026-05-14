"""Build-time leak warning tests.

Pinned contract: when the operator clicks Build (Build-preview tab)
and there are still un-handled leaks in the Review queue, the
MainWindow shows a friendly modal warning. Clicking "Back to review"
must NOT start the apply / build / verify pipeline; clicking "Build
anyway" proceeds with the current selection.

The warning is suppressed when there are no leaks (zero-detection
path) so the existing happy-path build behaviour is preserved.
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


def _wire_proj(proj, tmp_path):
    proj.map_path = tmp_path / "substitution_map.yml"
    proj.pending_path = tmp_path / "pending.yml"
    proj.applied_path = tmp_path / "applied.json"
    proj.verifier_report_path = tmp_path / "verifier.md"
    proj.decisions_path = tmp_path / "decisions.jsonl"
    proj.auto_t0_path = tmp_path / "auto_t0.yml"
    proj.auto_t1_path = tmp_path / "auto_t1.yml"
    return proj


def _make_window(qapp):
    from gui.app import MainWindow

    w = MainWindow()
    try:
        w.state.set_server_online(True)
    except Exception:
        pass
    return w


def _teardown_window(qapp, w) -> None:
    """Fully destroy a MainWindow so its child QTimers (notably the
    ServerPanel health-poll timer) don't leak into subsequent tests
    that share the module-scoped QApplication. Stopping the timer
    explicitly is the only way that survives Qt 6's deferred
    deletion: ``close()`` alone leaves the timer enqueued, and the
    next ``qapp.processEvents()`` call from ANOTHER test module
    will fire ``ServerPanel._poll`` which blocks on a 1s
    ``requests.get`` that, on some Windows network stacks, ignores
    the timeout and stalls for tens of seconds.
    """
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


def test_build_with_no_leaks_skips_dialog(qapp, tmp_path):
    """Zero pending detections → no warning, build runs straight away."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w._run_stage = MagicMock()

    confirm = MagicMock()
    w._confirm_build_with_leaks = confirm
    w._on_build_requested()

    confirm.assert_not_called()
    w._run_stage.assert_called_once_with("apply", from_queue=True)
    _teardown_window(qapp, w)


def test_build_with_leaks_shows_dialog_back_to_review(qapp, tmp_path):
    """Clicking "Back to review" cancels the build and switches to
    the Review pane / first leak instead of starting apply."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w.state.set_candidates(pending=[
        _make_cand("leaky-1.example", decision="pending"),
        _make_cand("leaky-2.example", decision="skip"),
    ])

    w._run_stage = MagicMock()
    focus_mock = MagicMock()
    w.review_view.focus_first_leak = focus_mock

    # User chose "Back to review" → confirm returns False.
    w._confirm_build_with_leaks = MagicMock(return_value=False)
    w._on_build_requested()

    # 2 unhandled candidates total (1 unreviewed + 1 skipped); the
    # gate prefers the "unreviewed" framing whenever any unreviewed
    # row is present.
    w._confirm_build_with_leaks.assert_called_once_with(2, mode="unreviewed")
    w._run_stage.assert_not_called(), "Back to review must NOT start apply"
    focus_mock.assert_called_once()
    # Pipeline state must be untouched.
    assert w._run_all_queue == []
    _teardown_window(qapp, w)


def test_build_with_leaks_build_anyway_runs_pipeline(qapp, tmp_path):
    """Clicking "Build anyway" proceeds with the current selection
    (no second pass) — exact same enqueue as the no-leak path."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w.state.set_candidates(pending=[
        _make_cand("leaky.example", decision="pending"),
    ])

    w._run_stage = MagicMock()
    w._confirm_build_with_leaks = MagicMock(return_value=True)
    w._on_build_requested()

    w._confirm_build_with_leaks.assert_called_once_with(1, mode="unreviewed")
    w._run_stage.assert_called_once_with("apply", from_queue=True)
    assert "build" in w._run_all_queue and "verify" in w._run_all_queue
    _teardown_window(qapp, w)


def test_build_dialog_debounce_prevents_duplicate_prompts(qapp, tmp_path):
    """If the user clicks Build twice rapidly, only one dialog must
    appear. The re-entrancy guard is set the moment the dialog opens
    and cleared afterwards."""
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w.state.set_candidates(pending=[
        _make_cand("leaky.example", decision="pending"),
    ])

    w._run_stage = MagicMock()
    call_count = {"n": 0}

    def fake_confirm(_n: int, *, mode: str = "skipped") -> bool:
        call_count["n"] += 1
        # Simulate a re-entrant Build click while the modal is "open".
        # The guard must short-circuit the second invocation.
        w._on_build_requested()
        return False

    w._confirm_build_with_leaks = fake_confirm
    w._on_build_requested()

    assert call_count["n"] == 1, (
        "duplicate Build clicks while the dialog is up must be debounced "
        "and not re-show the warning"
    )
    w._run_stage.assert_not_called()
    _teardown_window(qapp, w)


def test_confirm_dialog_button_wording_and_default(qapp, tmp_path):
    """Direct test of the dialog factory: title, button labels,
    default button and informative-text count must all match the spec
    (English wording, "Back to review" as the safer default)."""
    from PySide6.QtWidgets import QMessageBox
    from anonymize.project import Project

    w = _make_window(qapp)
    proj = _wire_proj(
        Project.for_folder(tmp_path / "in", tmp_path / "out"), tmp_path
    )
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)

    captured: dict[str, object] = {}

    def _fake_exec(self):
        # Record everything the production code wired into the box,
        # then click the "Back to review" button programmatically so
        # ``clickedButton()`` returns it (which makes _confirm return
        # False — closest thing to the user clicking No).
        captured["title"] = self.windowTitle()
        captured["info"] = self.informativeText()
        buttons = self.buttons()
        captured["button_labels"] = [b.text() for b in buttons]
        default_btn = self.defaultButton()
        captured["default_label"] = default_btn.text() if default_btn else None
        # Find the "Back to review" button and mark it as clicked
        # without actually showing the modal.
        for b in buttons:
            if b.text() == "Back to review":
                b.click()
                break
        return 0

    with patch.object(QMessageBox, "exec", _fake_exec):
        result = w._confirm_build_with_leaks(3)

    assert result is False, "clicking Back to review must return False"
    assert captured["title"] == "Potential leaks detected"
    labels = captured["button_labels"]
    assert "Build anyway" in labels and "Back to review" in labels
    assert captured["default_label"] == "Back to review", (
        "the safer 'Back to review' must be the default focused button"
    )
    assert "3" in captured["info"], "informative text must mention the count"
    _teardown_window(qapp, w)
