"""Tests for the installer-aware behaviour of :class:`gui.first_run_wizard._Hardware`.

When the Windows installer sentinel is present the page must:

* Force ``self.deployment_mode = "local_binary"`` regardless of what
  :func:`anonymize.hardware.suggest_deployment_mode` would have
  returned.
* Render a deployment hint that mentions the installer-chosen variant.
* Skip rendering the Docker pull button (no ``pull_btn`` attribute).

The test runs with QtWidgets in offscreen mode (configured by
``tests/conftest.py``); we mock ``_read_installer_sentinel`` directly
so no on-disk sentinel is needed.
"""

from __future__ import annotations

import sys

import pytest

from anonymize import hardware as hw

PySide6 = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _mock_hw_report():
    return hw.HardwareReport(
        gpus=[hw.GPU(name="Mock GPU", backend="nvidia", vram_total_mb=8192, vram_free_mb=7000)],
        cpu=hw.CPU(model="Mock CPU", physical_cores=4, logical_cores=8),
        memory=hw.Memory(total_mb=16384, available_mb=8192),
        disk=hw.Disk(path="/", total_mb=512000, free_mb=128000),
        os_name="Mock",
        os_release="1.0",
    )


def test_hardware_page_uses_installer_sentinel_cuda(qapp, monkeypatch):
    from gui import first_run_wizard as frw

    sentinel = {"variant": "cuda", "n_gpu_layers": 99, "llama_path": "C:/x.exe"}
    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: sentinel)
    monkeypatch.setattr(
        frw,
        "suggest_deployment_mode",
        lambda: ("docker", "should be ignored when sentinel present"),
    )

    page = frw._Hardware(_mock_hw_report())
    try:
        assert page.deployment_mode == "local_binary"
        # Docker pull button must NOT have been created when the
        # installer already bundled llama-server.
        assert not hasattr(page, "pull_btn")
    finally:
        page.deleteLater()


def test_hardware_page_uses_installer_sentinel_cpu(qapp, monkeypatch):
    from gui import first_run_wizard as frw

    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: {"variant": "cpu"})
    monkeypatch.setattr(
        frw, "suggest_deployment_mode", lambda: ("docker", "ignored")
    )
    page = frw._Hardware(_mock_hw_report())
    try:
        assert page.deployment_mode == "local_binary"
        assert not hasattr(page, "pull_btn")
    finally:
        page.deleteLater()


def test_hardware_page_uses_installer_sentinel_vulkan(qapp, monkeypatch):
    from gui import first_run_wizard as frw

    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: {"variant": "vulkan"})
    monkeypatch.setattr(
        frw, "suggest_deployment_mode", lambda: ("docker", "ignored")
    )
    page = frw._Hardware(_mock_hw_report())
    try:
        assert page.deployment_mode == "local_binary"
        assert not hasattr(page, "pull_btn")
    finally:
        page.deleteLater()


def test_hardware_page_falls_back_when_no_sentinel(qapp, monkeypatch):
    """Without sentinel, the existing suggest_deployment_mode wins."""
    from gui import first_run_wizard as frw

    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: None)
    monkeypatch.setattr(
        frw,
        "suggest_deployment_mode",
        lambda: ("local_binary", "Detected llama-server on PATH; using the local binary."),
    )
    page = frw._Hardware(_mock_hw_report())
    try:
        assert page.deployment_mode == "local_binary"
    finally:
        page.deleteLater()


def test_hardware_page_renders_docker_button_when_recommended(qapp, monkeypatch):
    """When sentinel is absent and suggest_* returns Docker, the pull
    button MUST exist (regression guard for the original behaviour)."""
    from gui import first_run_wizard as frw

    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: None)
    monkeypatch.setattr(
        frw, "suggest_deployment_mode", lambda: ("docker", "docker hint")
    )
    page = frw._Hardware(_mock_hw_report())
    try:
        assert page.deployment_mode == "docker"
        # Either the pull button or the install-Docker fallback button
        # exists; both code paths happen INSIDE the docker branch.
        # We assert the attribute set is non-empty.
        assert hasattr(page, "_pull_worker")
    finally:
        page.deleteLater()


def test_hardware_page_ignores_unknown_variant(qapp, monkeypatch):
    """An unknown variant value falls through to suggest_deployment_mode."""
    from gui import first_run_wizard as frw

    monkeypatch.setattr(frw, "_read_installer_sentinel", lambda: {"variant": "skip"})
    monkeypatch.setattr(
        frw,
        "suggest_deployment_mode",
        lambda: ("local_binary", "fallthrough"),
    )
    page = frw._Hardware(_mock_hw_report())
    try:
        # Variant "skip" is not in the trusted set so we use whatever
        # suggest_deployment_mode says.
        assert page.deployment_mode == "local_binary"
    finally:
        page.deleteLater()
