"""Tests for the Windows-installer sentinel branch in :mod:`anonymize.hardware`.

Covers :func:`_read_installer_sentinel` (present / absent / malformed)
and the STEP-0 short-circuit inside :func:`suggest_deployment_mode`.

We monkeypatch ``anonymize._paths.user_config_dir`` to point at a
``tmp_path`` directory so the test never touches the real per-user
config dir, regardless of the host OS.
"""

from __future__ import annotations

import json

import pytest

from anonymize import _paths, hardware as hw


@pytest.fixture
def fake_config_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "report-anonymizer"
    cfg.mkdir()
    monkeypatch.setattr(_paths, "user_config_dir", lambda: cfg)
    return cfg


def test_read_installer_sentinel_absent_returns_none(fake_config_dir):
    assert hw._read_installer_sentinel() is None


def test_read_installer_sentinel_present_returns_dict(fake_config_dir):
    payload = {
        "schema_version": 1,
        "variant": "cuda",
        "llama_path": "C:/x/llama-server.exe",
        "n_gpu_layers": 99,
    }
    (fake_config_dir / ".installer_choice.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    out = hw._read_installer_sentinel()
    assert isinstance(out, dict)
    assert out["variant"] == "cuda"
    assert out["n_gpu_layers"] == 99


def test_read_installer_sentinel_malformed_returns_none(fake_config_dir):
    (fake_config_dir / ".installer_choice.json").write_text(
        "not-json{{{{", encoding="utf-8"
    )
    assert hw._read_installer_sentinel() is None


def test_read_installer_sentinel_non_dict_returns_none(fake_config_dir):
    (fake_config_dir / ".installer_choice.json").write_text(
        "[1, 2, 3]", encoding="utf-8"
    )
    assert hw._read_installer_sentinel() is None


# ---- suggest_deployment_mode STEP 0 ---------------------------------------


def test_suggest_deployment_mode_uses_sentinel_cuda(fake_config_dir):
    (fake_config_dir / ".installer_choice.json").write_text(
        json.dumps({"variant": "cuda"}), encoding="utf-8"
    )
    mode, hint = hw.suggest_deployment_mode()
    assert mode == "local_binary"
    assert "cuda" in hint.lower()


def test_suggest_deployment_mode_uses_sentinel_cpu(fake_config_dir):
    (fake_config_dir / ".installer_choice.json").write_text(
        json.dumps({"variant": "cpu"}), encoding="utf-8"
    )
    mode, hint = hw.suggest_deployment_mode()
    assert mode == "local_binary"
    assert "cpu" in hint.lower()


def test_suggest_deployment_mode_uses_sentinel_vulkan(fake_config_dir):
    (fake_config_dir / ".installer_choice.json").write_text(
        json.dumps({"variant": "vulkan"}), encoding="utf-8"
    )
    mode, hint = hw.suggest_deployment_mode()
    assert mode == "local_binary"
    assert "vulkan" in hint.lower()


def test_suggest_deployment_mode_ignores_skip_variant(
    fake_config_dir, monkeypatch
):
    """``variant: "skip"`` (or any unknown value) must NOT short-circuit
    to local_binary; the fallback PATH/Docker logic kicks in."""
    (fake_config_dir / ".installer_choice.json").write_text(
        json.dumps({"variant": "skip"}), encoding="utf-8"
    )
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 1})(),
    )
    mode, hint = hw.suggest_deployment_mode()
    assert mode != "local_binary" or "skip" not in hint.lower()


def test_suggest_deployment_mode_ignores_malformed_sentinel(
    fake_config_dir, monkeypatch
):
    (fake_config_dir / ".installer_choice.json").write_text(
        "not-json", encoding="utf-8"
    )
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 1})(),
    )
    mode, hint = hw.suggest_deployment_mode()
    assert mode in ("local_binary", "docker", "external")
    assert "Setup wizard" not in hint
