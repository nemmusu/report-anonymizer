"""Regression: the inline "verifier found N residual leak(s)" banner
on the Pipeline summary must hide as soon as the user acts on it.

Two acknowledged-by-user actions:
1. clicking "Send all to Review" routes the residuals into the Review
   queue and hides the banner.
2. clicking "View report" switches to the Verifier view and hides
   the banner.

Without this fix the banner stayed visible after either action,
luring users into clicking the same button twice.
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


def _make_verifier_report(n: int):
    from anonymize.verifier import LeakHit, VerifierReport

    hits = [
        LeakHit(
            file=f"f{i}.md",
            pattern="email",
            match=f"leaked{i}@example.com",
            snippet=f"contact: leaked{i}@example.com",
        )
        for i in range(n)
    ]
    return VerifierReport(hits=hits, is_clean=False)


def _is_banner_shown(w) -> bool:
    """Read the banner's expected-visibility *intent* directly off
    the widget rather than ``isVisible()``: the test never calls
    ``MainWindow.show()`` (the offscreen platform plugin would fight
    the test runner) so ``isVisible()`` is always ``False`` regardless
    of what ``set_residuals`` did. ``isHidden()`` does follow the
    explicit-show / explicit-hide flag, which is exactly what we
    need here."""
    return not w.pipeline_view.residuals_box.isHidden()


def test_send_all_residuals_hides_banner(qapp, tmp_path):
    from anonymize.project import Project
    from gui.app import MainWindow

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    w = MainWindow()
    proj = _wire_proj(Project.for_folder(in_dir, out_dir), tmp_path)
    w.state.set_project(proj)
    rep = _make_verifier_report(3)
    w.state.set_verifier_report(rep)
    qapp.processEvents()
    assert _is_banner_shown(w) is True

    w._send_all_residuals_to_review()
    qapp.processEvents()
    assert _is_banner_shown(w) is False, (
        "banner must hide after Send all to Review"
    )
    w.close()


def test_view_report_hides_banner(qapp, tmp_path):
    from anonymize.project import Project
    from gui.app import MainWindow

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    w = MainWindow()
    proj = _wire_proj(Project.for_folder(in_dir, out_dir), tmp_path)
    w.state.set_project(proj)
    rep = _make_verifier_report(2)
    w.state.set_verifier_report(rep)
    qapp.processEvents()
    assert _is_banner_shown(w) is True

    w._on_open_verifier_report()
    qapp.processEvents()
    assert _is_banner_shown(w) is False, (
        "banner must hide after View report"
    )
    w.close()
