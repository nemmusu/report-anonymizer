"""Tests for server profile loading, extends, and command rendering."""
from __future__ import annotations

import yaml
from pathlib import Path

from anonymize.server_profile import (
    ServerProfile,
    SamplingConfig,
    builtin_profiles_path,
    load_profiles,
    render_command,
    save_user_profile,
)


def test_builtin_profiles_load() -> None:
    profiles = load_profiles()
    names = {p.name for p in profiles}
    assert "default" in names


def test_render_command_includes_required_flags() -> None:
    profile = ServerProfile(
        name="t", binary="/bin/echo", model="/tmp/model.gguf", parallel=4, ctx_size=16384
    )
    cmd = render_command(profile)
    assert "--ctx-size" in cmd and "16384" in cmd
    assert "--parallel" in cmd and "4" in cmd
    assert "--host" in cmd and "--port" in cmd
    assert "--n-gpu-layers" in cmd


def test_render_command_long_context_yarn() -> None:
    profile = ServerProfile(
        name="long",
        binary="/bin/echo",
        model="/tmp/m.gguf",
        ctx_size=500_000,
        rope_scaling="yarn",
        rope_scale=4.0,
        yarn_orig_ctx=131072,
        override_kv=["qwen3.context_length=int:1000000"],
    )
    cmd = render_command(profile)
    assert "--rope-scaling" in cmd and "yarn" in cmd
    assert "--rope-scale" in cmd and "4.0" in cmd
    assert "--yarn-orig-ctx" in cmd and "131072" in cmd
    assert "--override-kv" in cmd
    assert "qwen3.context_length=int:1000000" in cmd


def test_extends_inheritance(tmp_path: Path, monkeypatch) -> None:
    user = tmp_path / "user.yml"
    user.write_text(yaml.safe_dump({
        "version": 1,
        "profiles": [
            {"name": "base", "binary": "/x", "model": "/m.gguf", "parallel": 2},
            {"name": "child", "extends": "base", "parallel": 8},
        ],
    }), encoding="utf-8")
    from anonymize import server_profile as sp
    monkeypatch.setattr(sp, "USER_PROFILES_PATH", user)
    profiles = sp.load_profiles()
    by_name = {p.name: p for p in profiles}
    assert by_name["child"].parallel == 8
    assert by_name["child"].binary == "/x"


def test_save_user_profile_roundtrip(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "user.yml"
    from anonymize import server_profile as sp
    monkeypatch.setattr(sp, "USER_PROFILES_PATH", target)
    monkeypatch.setattr(sp, "CONFIG_DIR", tmp_path)
    prof = ServerProfile(name="custom", model="/m.gguf", parallel=12)
    save_user_profile(prof)
    again = sp.load_profiles()
    found = [p for p in again if p.name == "custom"]
    assert found and found[0].parallel == 12
