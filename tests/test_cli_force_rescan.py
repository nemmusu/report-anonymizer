"""Smoke tests for ``anonymize-dossier scan --force-rescan``.

We don't spawn the LLM: ``stage_detect_and_critic`` short-circuits when
no LLM URL is reachable, but the rules-pass alone (Tier 0) is enough to
prove ``--force-rescan`` bypasses the substitution-map cache via the
CLI surface.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "anonymize-dossier"


def _seed_inputs(tmp_path: Path) -> tuple[Path, Path]:
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "doc.md").write_text(
        "Public IP under audit: 93.184.216.34 reached the bastion.\n",
        encoding="utf-8",
    )
    return in_dir, out_dir


def _seed_map(map_path: Path) -> None:
    from anonymize.sub_map import SubstitutionMap

    smap = SubstitutionMap.load(map_path)
    smap.add("network", "93.184.216.34", "10.0.0.1")
    smap.save()


def _read_candidates_yaml(path: Path):
    import yaml
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for items in (data.get("candidates_by_category") or {}).values():
        for it in items or []:
            if isinstance(it, dict) and it.get("value"):
                out.append(it["value"])
    return out


def _read_auto_t0(out_dir: Path):
    return _read_candidates_yaml(out_dir / "auto_promoted_t0.yml")


def _run_scan(in_dir: Path, out_dir: Path, map_path: Path, *, fresh: bool) -> int:
    cmd = [
        sys.executable,
        str(BIN),
        "scan",
        str(in_dir),
        "-o",
        str(out_dir),
        "--map",
        str(map_path),
        "--llm-url",
        "http://127.0.0.1:1",  # unreachable -> detector skipped
    ]
    if fresh:
        cmd.append("--force-rescan")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    return proc.returncode


@pytest.mark.skipif(not BIN.exists(), reason="CLI script missing")
def test_cli_default_redetects_cached_value(tmp_path: Path) -> None:
    """Default behaviour: every scan re-detects every leak from scratch.

    The substitution_map.yml is treated as the canonical placeholder
    book, not as a "skip-list", running the scan again on a different
    document (or the same one) must always re-evaluate every match.
    """
    in_dir, out_dir = _seed_inputs(tmp_path)
    map_path = tmp_path / "substitution_map.yml"
    _seed_map(map_path)
    _run_scan(in_dir, out_dir, map_path, fresh=False)
    auto_t0 = _read_auto_t0(out_dir)
    assert "93.184.216.34" in auto_t0, (
        "default scan must re-detect every leak, even ones already in the map"
    )


@pytest.mark.skipif(not BIN.exists(), reason="CLI script missing")
def test_cli_force_rescan_redetects_cached_value(tmp_path: Path) -> None:
    in_dir, out_dir = _seed_inputs(tmp_path)
    map_path = tmp_path / "substitution_map.yml"
    _seed_map(map_path)
    # Pre-populate the run state so we can verify reset removes it.
    (out_dir / "needs_review.yml").write_text("[]", encoding="utf-8")
    (out_dir / "applied_substitutions.json").write_text("{}", encoding="utf-8")

    _run_scan(in_dir, out_dir, map_path, fresh=True)
    auto_t0 = _read_auto_t0(out_dir)
    assert "93.184.216.34" in auto_t0, (
        "with --force-rescan the cached value MUST be re-detected by Tier 0"
    )
    # The map itself must be left untouched.
    assert map_path.exists()
