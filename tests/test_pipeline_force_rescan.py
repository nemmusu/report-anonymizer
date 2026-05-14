"""``force_rescan`` propagates through the engine pipeline.

When the operator clicks "Reset run state" (or passes ``--force-rescan``
on the CLI) the deterministic rules pass and the LLM detector must
ignore the ``substitution_map.yml`` cache for that single run, so every
leak is re-detected. This is the "no cache" behaviour the user asked for.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _bootstrap_project(tmp_path: Path):
    from anonymize.project import Project

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    sample = in_dir / "doc.md"
    # Deliberately use a public IP that the default Tier-0 patterns
    # consider a leak (no allowlist hit).
    sample.write_text(
        "Public IP under audit: 93.184.216.34 reached the bastion.\n",
        encoding="utf-8",
    )
    proj = Project.for_folder(in_dir, out_dir)
    proj.map_path = tmp_path / "substitution_map.yml"
    proj.patterns_path = Path("config/leak_patterns.yml").resolve()
    proj.safe_terms_path = Path("config/safe_terms.yml").resolve()
    proj.pending_path = out_dir / "needs_review.yml"
    proj.auto_t0_path = out_dir / "auto_t0.yml"
    proj.auto_t1_path = out_dir / "auto_t1.yml"
    proj.applied_path = out_dir / "applied.json"
    proj.decisions_path = out_dir / "decisions.jsonl"
    proj.verifier_report_path = out_dir / "verifier.md"
    return proj


def _seed_map(proj):
    from anonymize.sub_map import SubstitutionMap

    smap = SubstitutionMap.load(proj.map_path)
    smap.add("network", "93.184.216.34", "10.0.0.1")
    smap.save()
    return smap


def test_default_redetects_values_in_the_map(tmp_path):
    """Default contract: every scan re-detects every leak from scratch.

    The substitution_map.yml is the canonical placeholder book, not a
    skip-list, running scan again on a different document (or the same
    one) must always re-evaluate every match so the user never sees stale
    results from a previous project.
    """
    from anonymize.pipeline import stage_scan_and_rules

    proj = _bootstrap_project(tmp_path)
    _seed_map(proj)

    _scan, cands, _r = stage_scan_and_rules(proj)
    assert any(c.value == "93.184.216.34" for c in cands), (
        "default scan must re-detect every leak, even ones already in the map"
    )


def test_explicit_force_rescan_false_uses_map_cache(tmp_path):
    """Power-user opt-out: pass ``force_rescan=False`` to *re-enable* the
    map-cache filter. The default Project field is ``True`` but the
    ``force_rescan`` argument of :func:`stage_scan_and_rules` still wins
    when set, so callers that want the old behaviour can ask for it."""
    from anonymize.pipeline import stage_scan_and_rules

    proj = _bootstrap_project(tmp_path)
    _seed_map(proj)

    _scan, cands, _r = stage_scan_and_rules(proj, force_rescan=False)
    assert all(c.value != "93.184.216.34" for c in cands)


def test_force_rescan_ignores_map_cache(tmp_path):
    from anonymize.pipeline import stage_scan_and_rules

    proj = _bootstrap_project(tmp_path)
    _seed_map(proj)

    _scan, cands, _r = stage_scan_and_rules(proj, force_rescan=True)
    assert any(c.value == "93.184.216.34" for c in cands), (
        "with force_rescan=True the map cache MUST be bypassed and the "
        "value must be re-detected as a candidate"
    )


def test_project_force_rescan_field_drives_default(tmp_path):
    """No explicit ``force_rescan`` arg: the project field is used."""
    from anonymize.pipeline import stage_scan_and_rules

    proj = _bootstrap_project(tmp_path)
    _seed_map(proj)
    proj.force_rescan = True

    _scan, cands, _r = stage_scan_and_rules(proj)
    assert any(c.value == "93.184.216.34" for c in cands)


def test_reset_run_state_removes_state_files(tmp_path):
    from anonymize.pipeline import reset_run_state, save_state

    proj = _bootstrap_project(tmp_path)
    _seed_map(proj)
    proj.auto_t0_path.write_text("[]", encoding="utf-8")
    proj.auto_t1_path.write_text("[]", encoding="utf-8")
    proj.pending_path.write_text("[]", encoding="utf-8")
    proj.applied_path.write_text("{}", encoding="utf-8")
    proj.decisions_path.write_text("", encoding="utf-8")
    proj.verifier_report_path.write_text("# verifier", encoding="utf-8")
    save_state(proj, stage="scan")

    rep = reset_run_state(proj)
    assert "auto_t0.yml" in rep["removed"]
    assert "needs_review.yml" in rep["removed"]
    assert "applied.json" in rep["removed"]
    assert "verifier.md" in rep["removed"]
    assert "state.json" in rep["removed"]
    assert not proj.auto_t0_path.exists()
    assert not proj.pending_path.exists()
    # The global substitution map is NEVER touched by reset.
    assert proj.map_path.exists()
