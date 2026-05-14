"""Tests for :func:`anonymize.hardware.report_dict`.

Contract: the function returns a JSON-serializable dict whose top-level
keys are stable (``gpus``, ``cpu``, ``memory``, ``disk``, ``os_name``,
``os_release``). The Inno Setup wizard parses this output, so any
breaking change here is a coordinated installer update.
"""

from __future__ import annotations

import json

import pytest

from anonymize import hardware as hw


def test_report_dict_is_json_serializable():
    payload = hw.report_dict()
    serialized = json.dumps(payload)
    assert isinstance(serialized, str) and len(serialized) > 0


def test_report_dict_top_level_keys_stable():
    payload = hw.report_dict()
    expected = {"gpus", "cpu", "memory", "disk", "os_name", "os_release"}
    assert expected.issubset(payload.keys())


def test_report_dict_gpus_is_list():
    payload = hw.report_dict()
    assert isinstance(payload["gpus"], list)
    for g in payload["gpus"]:
        assert isinstance(g, dict)
        assert "name" in g and "backend" in g and "vram_total_mb" in g
        assert isinstance(g["vram_total_mb"], int)


def test_report_dict_cpu_optional_shape():
    payload = hw.report_dict()
    cpu = payload["cpu"]
    if cpu is None:
        return
    assert {"model", "physical_cores", "logical_cores"}.issubset(cpu.keys())
    assert isinstance(cpu["physical_cores"], int)
    assert isinstance(cpu["logical_cores"], int)


def test_report_dict_with_mocked_hardware(monkeypatch):
    """Force a known shape through detect_* mocks; assert flattening."""
    fake = hw.HardwareReport(
        gpus=[hw.GPU(name="Mock GPU", backend="nvidia", vram_total_mb=8192, vram_free_mb=7000)],
        cpu=hw.CPU(model="Mock CPU", physical_cores=4, logical_cores=8),
        memory=hw.Memory(total_mb=16384, available_mb=8192),
        disk=hw.Disk(path="/mock", total_mb=512000, free_mb=128000),
        os_name="MockOS",
        os_release="1.0",
    )
    monkeypatch.setattr(hw, "report", lambda disk_path=".": fake)
    payload = hw.report_dict()
    assert payload["os_name"] == "MockOS"
    assert payload["os_release"] == "1.0"
    assert payload["gpus"][0]["backend"] == "nvidia"
    assert payload["cpu"]["physical_cores"] == 4
    assert payload["memory"]["total_mb"] == 16384
    assert payload["disk"]["free_mb"] == 128000
    json.dumps(payload)


def test_report_dict_handles_empty_gpus(monkeypatch):
    fake = hw.HardwareReport(gpus=[], cpu=None, memory=None, disk=None)
    monkeypatch.setattr(hw, "report", lambda disk_path=".": fake)
    payload = hw.report_dict()
    assert payload["gpus"] == []
    assert payload["cpu"] is None
    assert payload["memory"] is None
    assert payload["disk"] is None
    json.dumps(payload)
