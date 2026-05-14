"""Windows-only Docker hide for ``DeploymentChooserDialog``.

Pinned UX contract: on Windows the Docker (managed by the GUI) row
must be hidden in the deployment chooser dialog because the Setup
wizard already installs a native llama.cpp binary. On Linux/macOS
the row stays visible (Docker is a useful fallback there).

We monkeypatch ``sys.platform`` BEFORE constructing the dialog so the
import-time guard inside ``__init__`` reads the right value. The
PySide6 widget visibility itself is then a runtime Qt attribute, not
a re-evaluated Python check, so the assertions below are stable.
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


def _make_profile():
    from anonymize.server_profile import ServerProfile

    return ServerProfile(
        name="test-profile",
        description="for tests",
        deployment_mode="local_binary",
    )


def _patch_suggest(monkeypatch):
    """The dialog calls ``suggest_deployment_mode`` at construction
    which on a real machine peeks at hardware/PATH; pin it so the
    test stays deterministic and fast on every CI runner."""
    import anonymize.hardware as hw
    import gui.deployment_chooser_dialog as dcd

    fake = lambda: ("local_binary", "fake hint for tests")
    monkeypatch.setattr(hw, "suggest_deployment_mode", fake, raising=False)
    monkeypatch.setattr(
        dcd, "suggest_deployment_mode", fake, raising=False
    )


def test_docker_radio_hidden_on_windows(qapp, monkeypatch):
    """On Windows the Docker radio + helper box must be hidden, while
    Local binary and External stay visible: Docker Desktop is
    unnecessary friction because the Setup wizard already shipped a
    native llama.cpp binary."""
    _patch_suggest(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    # The dialog module captures ``sys`` at import time, so re-importing
    # is unnecessary; the ``sys.platform == "win32"`` check inside
    # ``__init__`` reads the just-patched attribute.
    from gui.deployment_chooser_dialog import DeploymentChooserDialog

    profile = _make_profile()
    dlg = DeploymentChooserDialog(profile)
    try:
        # Show offscreen so visibility flags propagate. Without this
        # call PySide6 reports every child as not-visible because
        # the parent never reached "shown" state.
        dlg.show()
        qapp.processEvents()
        assert dlg.rb_local.isVisible() is True
        assert dlg.rb_external.isVisible() is True
        assert dlg.rb_docker.isVisible() is False, (
            "Docker radio must be hidden on Windows"
        )
        assert dlg.rb_docker.isEnabled() is False, (
            "Docker radio must be disabled too so keyboard-tab focus "
            "cannot reach a hidden control"
        )
        # The docker form box (image + GPU checkbox) must also be hidden.
        assert dlg._docker_box.isVisible() is False
        # And the active selection must NOT be docker on Windows.
        assert dlg.rb_docker.isChecked() is False
    finally:
        dlg.close()
        qapp.processEvents()


def test_docker_radio_visible_on_linux(qapp, monkeypatch):
    """On Linux/macOS the Docker row stays as a legitimate option
    (no native installer there). This is the inverse direction of
    the Windows hide so a regression that hides Docker everywhere
    fails loud."""
    _patch_suggest(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from gui.deployment_chooser_dialog import DeploymentChooserDialog

    profile = _make_profile()
    dlg = DeploymentChooserDialog(profile)
    try:
        dlg.show()
        qapp.processEvents()
        assert dlg.rb_docker.isVisible() is True, (
            "Docker radio must remain visible on non-Windows OSes"
        )
        assert dlg.rb_docker.isEnabled() is True
        assert dlg.rb_local.isVisible() is True
        assert dlg.rb_external.isVisible() is True
    finally:
        dlg.close()
        qapp.processEvents()


def test_preset_editor_omits_docker_on_windows(qapp, monkeypatch):
    """The full preset editor must drop the Docker entry from its
    deployment combo box on Windows, mirroring the chooser dialog
    behaviour, so the user never sees a Docker option that the
    Setup wizard cannot satisfy."""
    monkeypatch.setattr(sys, "platform", "win32")
    from gui.preset_editor import PresetEditor

    profile = _make_profile()
    editor = PresetEditor(profile)
    try:
        ids = [
            editor.deployment.itemData(i)
            for i in range(editor.deployment.count())
        ]
        assert "docker" not in ids, (
            f"docker must NOT appear in the deployment combo on Windows; got {ids!r}"
        )
        assert "local_binary" in ids
        assert "external" in ids
    finally:
        editor.close()
        qapp.processEvents()
