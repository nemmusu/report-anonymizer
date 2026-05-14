"""Tests for the paused-state lifecycle helpers.

Covers:

- ``mark_paused`` / ``clear_pause_marker`` round-trip in
  ``<output>/.anon/state.json``.
- ``MainWindow._clear_pipeline_state`` empties the run-all queue, drops
  the fresh-rescan flag and removes the persisted ``paused`` marker.
- ``closeEvent`` clears the pipeline state (even when no project is open).
"""
from __future__ import annotations

import json
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


def test_mark_and_clear_pause_round_trip(tmp_path):
    from anonymize.pipeline import clear_pause_marker, load_state, mark_paused
    from anonymize.project import Project

    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)

    mark_paused(proj, why="awaiting approval")
    state = load_state(proj)
    assert "paused" in state
    assert state["paused"]["why"] == "awaiting approval"

    clear_pause_marker(proj)
    state = load_state(proj)
    assert "paused" not in state


def test_clear_pause_marker_is_idempotent(tmp_path):
    from anonymize.pipeline import clear_pause_marker
    from anonymize.project import Project

    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    clear_pause_marker(proj)
    clear_pause_marker(proj)


def test_clear_pipeline_state_drops_queue_and_marker(qapp, tmp_path):
    from anonymize.pipeline import load_state, mark_paused
    from anonymize.project import Project
    from gui.app import MainWindow

    w = MainWindow()
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj)
    w._run_all_queue = ["promote", "apply"]
    mark_paused(proj, why="test")

    w._clear_pipeline_state(reason="silent")
    assert w._run_all_queue == []
    # Fresh-rescan is the new default; it must be preserved (not cleared)
    # so subsequent runs never reuse stale results.
    assert w._fresh_rescan is True
    assert proj.force_rescan is True
    state = load_state(proj)
    assert "paused" not in state
    w.close()


def test_close_event_clears_state_with_no_project(qapp, tmp_path):
    from gui.app import MainWindow

    w = MainWindow()
    w._run_all_queue = ["promote", "apply"]
    w._stop_all = MagicMock()
    w.close()
    assert w._run_all_queue == []
    # The fresh-rescan flag stays ``True`` (the new default contract).
    assert w._fresh_rescan is True


def test_open_paths_clears_previous_state(qapp, tmp_path, monkeypatch):
    """Switching project must drop the queue from the previous one."""
    from anonymize.project import Project
    from gui.app import MainWindow

    w = MainWindow()
    proj_a = Project.for_folder(tmp_path / "a_in", tmp_path / "a_out")
    (tmp_path / "a_in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "a_out").mkdir(parents=True, exist_ok=True)
    w.state.set_project(proj_a)
    w._run_all_queue = ["promote", "apply"]

    proj_b = Project.for_folder(tmp_path / "b_in", tmp_path / "b_out")
    (tmp_path / "b_in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "b_out").mkdir(parents=True, exist_ok=True)

    # Stub the dialog: pretend the user accepted and selected ``proj_b``.
    class _StubDialog:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return True

        def to_project(self):
            return proj_b

    monkeypatch.setattr("gui.app.ImportDialog", _StubDialog)
    w._open_paths([tmp_path / "b_in"])
    assert w._run_all_queue == []
    # Fresh-rescan must be ``True`` after switching projects so the
    # next scan reprocesses the new document from scratch.
    assert w._fresh_rescan is True
    w.close()
