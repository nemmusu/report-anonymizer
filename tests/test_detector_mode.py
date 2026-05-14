"""Tests for the ``Project.detector_mode`` switch and the prompt
resolution helper that the multipass detector relies on.

The detector itself talks to llama-server, so these tests don't run
an actual LLM: they verify that the configuration plumbing surfaces
the right list of prompt paths for each mode, that the env-var
override still takes priority (for batch A/B testing), and that
turning on ``"multipass"`` without the per-category prompts on disk
fails loudly instead of silently degrading to one pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from anonymize.pipeline import (
    MULTIPASS_PROMPTS_DIR,
    PROMPTS_DIR,
    _resolve_detector_prompt_paths,
)
from anonymize.project import MULTIPASS_PROMPT_FILES, Project


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Make sure no stray ``ANONYMIZE_DETECTOR_PROMPTS`` leaks across
    tests."""
    monkeypatch.delenv("ANONYMIZE_DETECTOR_PROMPTS", raising=False)


def test_default_mode_resolves_to_single_monolithic_prompt(tmp_path):
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    paths = _resolve_detector_prompt_paths(proj)
    assert paths == [PROMPTS_DIR / "system_detector.txt"]


def test_explicit_single_mode_matches_default(tmp_path):
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    proj.detector_mode = "single"
    paths = _resolve_detector_prompt_paths(proj)
    assert paths == [PROMPTS_DIR / "system_detector.txt"]


def test_multipass_mode_resolves_to_the_eleven_category_prompts(tmp_path):
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    proj.detector_mode = "multipass"
    paths = _resolve_detector_prompt_paths(proj)
    assert len(paths) == 11
    assert paths == [MULTIPASS_PROMPTS_DIR / name for name in MULTIPASS_PROMPT_FILES]
    for p in paths:
        assert p.exists(), f"multipass prompt missing on disk: {p}"


def test_multipass_mode_fails_loud_when_a_prompt_is_missing(
    tmp_path, monkeypatch
):
    """A partially-installed package must not silently degrade to a
    smaller number of passes — that would change behaviour
    invisibly."""
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    proj.detector_mode = "multipass"
    fake_dir = tmp_path / "missing_prompts"
    fake_dir.mkdir()
    monkeypatch.setattr(
        "anonymize.pipeline.MULTIPASS_PROMPTS_DIR", fake_dir
    )
    with pytest.raises(FileNotFoundError) as exc:
        _resolve_detector_prompt_paths(proj)
    assert "multipass" in str(exc.value).lower()


def test_env_var_override_wins_over_project_mode(tmp_path, monkeypatch):
    """The env-var lets the A/B harness (and CI) drive the detector
    without touching project YAML."""
    custom_a = tmp_path / "alt_a.txt"
    custom_b = tmp_path / "alt_b.txt"
    custom_a.write_text("dummy A")
    custom_b.write_text("dummy B")
    # os.pathsep is ':' on POSIX and ';' on Windows -- matching the same
    # convention the production code uses so Windows drive letters
    # (C:\foo) are not mis-split as two segments.
    monkeypatch.setenv(
        "ANONYMIZE_DETECTOR_PROMPTS",
        os.pathsep.join([str(custom_a), str(custom_b)]),
    )
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    proj.detector_mode = "multipass"
    paths = _resolve_detector_prompt_paths(proj)
    assert paths == [custom_a, custom_b]


def test_env_var_strips_empty_segments(tmp_path, monkeypatch):
    real = tmp_path / "only.txt"
    real.write_text("dummy")
    sep = os.pathsep
    monkeypatch.setenv(
        "ANONYMIZE_DETECTOR_PROMPTS", f"{sep}  {sep}{real}{sep}{sep}"
    )
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    paths = _resolve_detector_prompt_paths(proj)
    assert paths == [real]


def test_app_settings_round_trip(tmp_path, monkeypatch):
    """The Server-tab combobox writes through
    ``anonymize.app_settings.set_str`` and the next ``get_str`` must
    return the same value, even after a fresh import (the on-disk
    YAML is the source of truth)."""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(
        "anonymize.app_settings._CONFIG_DIR", cfg_dir
    )
    monkeypatch.setattr(
        "anonymize.app_settings._SETTINGS_PATH",
        cfg_dir / "app_settings.yml",
    )
    from anonymize import app_settings

    assert app_settings.get_str("detector_mode", default="single") == "single"
    app_settings.set_str("detector_mode", "multipass")
    assert app_settings.get_str("detector_mode") == "multipass"
    app_settings.set_str("detector_mode", "single")
    assert app_settings.get_str("detector_mode") == "single"


def test_project_serialises_detector_mode_in_to_dict(tmp_path):
    proj = Project.for_folder(tmp_path / "in", tmp_path / "out")
    proj.detector_mode = "multipass"
    d = proj.to_dict()
    assert d["detector_mode"] == "multipass"


def test_multipass_prompts_shipped_alongside_monolithic(tmp_path):
    """Every prompt named in ``MULTIPASS_PROMPT_FILES`` must exist on
    disk under ``prompts/detector_multipass/``. Catches the case where
    a new category is declared in code but its prompt file was
    forgotten in packaging."""
    for name in MULTIPASS_PROMPT_FILES:
        p = MULTIPASS_PROMPTS_DIR / name
        assert p.exists() and p.stat().st_size > 0, (
            f"missing or empty multipass prompt: {p}"
        )
