"""Tests for §1.1.bis preset path re-rooting in :mod:`anonymize.server_profile`.

Validates that the YAML loader rewrites legacy
``~/.local/share/document-anonymizer/models/...`` paths embedded in the
built-in :file:`config/server_profiles.yml` onto the cross-platform
:func:`_paths.models_dir` location, so the same catalog file works on
Linux/macOS/Windows without manual edits.

We mock :func:`_paths.models_dir` to return a tmp directory and assert
the loader rewrites the path; the test runs untouched on any host OS.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anonymize import server_profile as sp


def _write_preset_yaml(tmp_path: Path, model_value: str) -> Path:
    """Write a minimal one-profile YAML and return its path."""
    yml = tmp_path / "preset.yml"
    yml.write_text(
        "version: 1\n"
        "profiles:\n"
        "  - name: test-preset\n"
        f"    model: {model_value}\n"
        "    is_builtin: true\n",
        encoding="utf-8",
    )
    return yml


def test_rerooting_unix_legacy_prefix(monkeypatch, tmp_path):
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)
    yml = _write_preset_yaml(
        tmp_path,
        "~/.local/share/document-anonymizer/models/myrepo/model.gguf",
    )
    out = sp._load_yaml_profiles(yml, source_label="test")
    assert len(out) == 1
    rerooted = Path(out[0]["model"])
    assert rerooted == fake_models / "myrepo" / "model.gguf"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Backslash-path re-rooting relies on Windows-native Path semantics; "
    "on POSIX, ``Path('~\\\\.local\\\\…')`` is parsed as a single segment "
    "containing literal backslashes and the legacy-prefix detector cannot "
    "split it. The cross-platform unix variant above already covers the "
    "loader logic.",
)
def test_rerooting_windows_legacy_prefix(monkeypatch, tmp_path):
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)
    # Inline backslashes in YAML scalar; YAML safe_load turns the
    # literal string through unchanged. We use single-quoted scalar so
    # the backslashes don't get treated as escape sequences.
    yml = tmp_path / "preset.yml"
    yml.write_text(
        "version: 1\n"
        "profiles:\n"
        "  - name: test-win\n"
        "    model: '~\\.local\\share\\document-anonymizer\\models\\myrepo\\model.gguf'\n",
        encoding="utf-8",
    )
    out = sp._load_yaml_profiles(yml, source_label="test")
    assert len(out) == 1
    rerooted = Path(out[0]["model"])
    assert rerooted == fake_models / "myrepo" / "model.gguf"


def test_non_legacy_paths_pass_through(monkeypatch, tmp_path):
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)
    user_path = "/opt/custom/llama-models/foo.gguf"
    yml = _write_preset_yaml(tmp_path, user_path)
    out = sp._load_yaml_profiles(yml, source_label="test")
    assert out[0]["model"] == user_path


def test_empty_model_field_is_safe(monkeypatch, tmp_path):
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)
    yml = _write_preset_yaml(tmp_path, "''")
    out = sp._load_yaml_profiles(yml, source_label="test")
    assert out[0]["model"] == ""


def test_builtin_catalog_rerooted_to_models_dir(monkeypatch, tmp_path):
    """End-to-end: load the real shipped catalog with mocked models_dir
    and assert all legacy paths got rewritten."""
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)

    out = sp._load_yaml_profiles(
        sp.builtin_profiles_path(), source_label="builtin"
    )
    assert out, "shipped catalog should yield at least one profile"
    for row in out:
        model = row.get("model")
        if isinstance(model, str) and model:
            assert "document-anonymizer/models" not in model.replace("\\", "/"), (
                f"profile {row.get('name')!r} still has legacy path: {model!r}"
            )


def test_reroot_helper_passes_through_non_strings(monkeypatch, tmp_path):
    fake_models = tmp_path / "models-root"
    monkeypatch.setattr(sp, "_models_dir", lambda: fake_models)
    assert sp._reroot_legacy_model_path(None) is None
    assert sp._reroot_legacy_model_path(42) == 42
    assert sp._reroot_legacy_model_path("") == ""
