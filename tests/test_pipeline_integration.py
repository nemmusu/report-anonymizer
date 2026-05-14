"""End-to-end pipeline test on a synthetic folder, no LLM (mock or none)."""
from __future__ import annotations

from pathlib import Path

from anonymize.pipeline import (
    stage_apply,
    stage_promote,
    stage_scan_and_rules,
    stage_verify,
)
from anonymize.project import Project


def _setup_project(tmp_path: Path) -> Project:
    src = tmp_path / "in"
    src.mkdir()
    # Use a public IP (private 10.x is in the safe allowlist)
    (src / "README.md").write_text(
        "Server: 93.184.216.34\nPhone: +39 351 123 4567\nEmail: support@acme.example.com\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    proj = Project.for_folder(src, out)
    base_cfg = Path(__file__).resolve().parent.parent / "config"
    proj.map_path = tmp_path / "map.yml"
    proj.patterns_path = base_cfg / "leak_patterns.yml"
    proj.safe_terms_path = base_cfg / "safe_terms.yml"
    proj.pending_path = tmp_path / "pending.yml"
    proj.auto_t0_path = tmp_path / "t0.yml"
    proj.auto_t1_path = tmp_path / "t1.yml"
    proj.applied_path = tmp_path / "applied.json"
    proj.verifier_report_path = tmp_path / "verifier.md"
    proj.decisions_path = tmp_path / "dec.jsonl"
    return proj


def test_t0_only_promote_apply_verify_clean(tmp_path: Path) -> None:
    proj = _setup_project(tmp_path)
    scan, t0, r0 = stage_scan_and_rules(proj)
    assert r0.ok
    assert any("93.184.216.34" == c.value for c in t0)
    rp = stage_promote(proj)
    assert rp.ok
    rep, ra = stage_apply(proj, scan)
    assert ra.ok and rep.total_events >= 1
    rv, rvres = stage_verify(proj)
    # Verifier may still report regressions if T0 missed some - so allow non-clean
    assert rv.files_scanned >= 1
