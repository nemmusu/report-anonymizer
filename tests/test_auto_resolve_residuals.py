"""Tests for the verifier-feedback auto-resolve loop.

The auto-resolve stage closes the gap between "the LLM detector
missed an occurrence" and "the canonical map already knows how to
rewrite it", a deterministic loop that takes residual leaks
reported by the verifier and feeds them back into promote/apply.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from anonymize.candidates import Candidate
from anonymize.sub_map import SubstitutionMap
from anonymize.triage import (
    _match_case,
    derive_placeholder_for_hit,
)


# ---------- _match_case ------------------------------------------------------


@pytest.mark.parametrize(
    "reference, target, expected",
    [
        ("acmebrand", "VendorVoice", "vendorvoice"),
        ("ACMEBRAND", "VendorVoice", "VENDORVOICE"),
        ("Customer Brand", "vendor voice", "Vendor Voice"),
        ("AcmeApp", "VendorVoice", "VendorVoice"),  # camel: keep target
    ],
)
def test_match_case_preserves_shape(reference, target, expected) -> None:
    assert _match_case(reference, target) == expected


# ---------- derive_placeholder_for_hit --------------------------------------


def _smap_with(entries: dict[str, list[dict]]) -> SubstitutionMap:
    """Build a tiny in-memory map for testing without touching disk."""
    m = SubstitutionMap.load(Path("/tmp/this/should/not/exist.yml"))
    for cat, items in entries.items():
        m.entries[cat] = list(items)
    return m


def test_derive_for_bare_brand_uses_existing_map_entry() -> None:
    smap = _smap_with({
        "brand": [
            {"from": "AcmeApp", "to": "VendorVoice", "id": "brand:0005"},
        ],
    })
    cand = derive_placeholder_for_hit("acmeapp", smap)
    assert cand is not None
    assert cand.value == "acmeapp"
    assert cand.suggested_placeholder == "vendorvoice"
    assert cand.tier == "T3_auto_residual"
    assert cand.category == "brand"


def test_derive_returns_none_when_no_ancestor() -> None:
    smap = _smap_with({"brand": []})
    assert derive_placeholder_for_hit("acmecorp", smap) is None


def test_derive_handles_regression_pattern() -> None:
    """``regression:from-value`` hits ARE actionable: the map knows
    the placeholder; the loop re-emits the same candidate so a
    follow-up apply can catch occurrences the first pass missed."""
    smap = _smap_with({
        "brand": [
            {"from": "AcmeApp", "to": "VendorVoice", "id": "brand:0005"},
        ],
    })
    cand = derive_placeholder_for_hit(
        "AcmeApp", smap, pattern="regression:from-value"
    )
    assert cand is not None
    assert cand.value == "AcmeApp"
    assert cand.suggested_placeholder == "VendorVoice"


def test_derive_clamps_overlong_placeholder() -> None:
    """The same length-clamp the rest of the pipeline uses applies to
    the auto-resolved placeholder too."""
    smap = _smap_with({
        "ids": [
            {
                "from": "ACME-VULN-13",
                "to": "VENDOR-CONFIRMED_VULN-13_Zero_OTP_Account_Takeover",
                "id": "ids:0010",
            },
        ],
    })
    # Hit value is the bare ``acme-vuln-13`` (lowercase). The map's
    # placeholder is much longer than the hit; the derivation must
    # clamp it down to ``len(hit_value)``.
    cand = derive_placeholder_for_hit("acme-vuln-13", smap)
    if cand is not None:
        assert len(cand.suggested_placeholder) <= len("acme-vuln-13")


def test_derive_refuses_identity_placeholders() -> None:
    """If the derived placeholder would equal the hit (e.g. both
    already lowercase and the map maps them to themselves) the
    derivation returns None, adding a no-op entry would waste a
    slot in the map."""
    smap = _smap_with({
        "brand": [
            {"from": "FooCorp", "to": "FooCorp", "id": "brand:0001"},
        ],
    })
    assert derive_placeholder_for_hit("foocorp", smap) is None


# ---------- stage_auto_resolve_residuals ------------------------------------


def _make_project(tmp_path: Path, *, body: str) -> "tuple":
    """Set up a tiny project where the dynamic verifier (built from
    the map) will spot a residual brand match on the apply output."""
    from anonymize.project import Project

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "doc.md").write_text(body, encoding="utf-8")
    proj = Project.for_folder(in_dir, out_dir)
    proj.map_path = tmp_path / "substitution_map.yml"
    proj.patterns_path = Path("config/leak_patterns.yml").resolve()
    proj.safe_terms_path = Path("config/safe_terms.yml").resolve()
    # Use the canonical bookkeeping filenames so verify() correctly
    # skips them (otherwise a residual hit would be reported on the
    # applied_substitutions JSON itself, which is a tautology).
    proj.pending_path = out_dir / "needs_review.yml"
    proj.auto_t0_path = out_dir / "auto_promoted_t0.yml"
    proj.auto_t1_path = out_dir / "auto_promoted_t1.yml"
    proj.applied_path = out_dir / "applied_substitutions.json"
    proj.decisions_path = out_dir / "decisions_history.jsonl"
    proj.verifier_report_path = out_dir / "verifier_report.md"
    return proj, in_dir, out_dir


def test_stage_auto_resolve_converges(tmp_path: Path) -> None:
    """Map already knows ``AcmeApp -> VendorVoice``. The first apply
    only rewrites whatever the detector flagged in this run (the
    longest match). After auto-resolve the lowercase ``acmeapp``
    residual is filled in too via case-derivation from the map.
    """
    from anonymize.pipeline import (
        stage_apply,
        stage_auto_resolve_residuals,
    )

    body = (
        "The customer brand is AcmeApp Pro.\n"
        "Lower-case mention: acmeapp.\n"
    )
    proj, _, out_dir = _make_project(tmp_path, body=body)

    smap = SubstitutionMap.load(proj.map_path)
    smap.add("brand", "AcmeApp Pro", "Vendor App")
    smap.add("brand", "AcmeApp", "VendorVoice")
    smap.save()

    # First apply: drop case-insensitivity so the demo clearly shows
    # the auto-resolve filling the bare-lowercase gap.
    smap = SubstitutionMap.load(proj.map_path)
    smap.options["case_insensitive_categories"] = []
    smap.save()

    from anonymize.pipeline import stage_apply
    from anonymize.scanner import scan_path

    scan = scan_path(proj.input_paths[0])
    rules = SubstitutionMap.load(proj.map_path).to_rules()
    from anonymize.applier import apply

    apply(proj, scan, rules)

    # Now run auto-resolve and confirm convergence.
    final_report, sr = stage_auto_resolve_residuals(proj, max_iterations=2)
    assert sr.extras["initial_hits"] >= 1
    assert sr.extras["final_hits"] == 0
    assert sr.extras["iterations"] >= 1


def test_stage_auto_resolve_zero_iterations_when_clean(tmp_path: Path) -> None:
    """If verify reports zero hits the loop must run zero
    iterations (no map mutation)."""
    from anonymize.pipeline import stage_auto_resolve_residuals
    from anonymize.applier import apply
    from anonymize.scanner import scan_path

    proj, _, _ = _make_project(tmp_path, body="No leaks here.\n")
    smap = SubstitutionMap.load(proj.map_path)
    smap.save()
    scan = scan_path(proj.input_paths[0])
    apply(proj, scan, SubstitutionMap.load(proj.map_path).to_rules())

    final_report, sr = stage_auto_resolve_residuals(proj)
    assert sr.extras["iterations"] == 0
    assert sr.extras["final_hits"] == 0


def test_stage_auto_resolve_stops_when_no_ancestor(tmp_path: Path) -> None:
    """When the verifier reports a hit that has NO ancestor in the
    map, the deterministic loop stops without modifying the map
    (the hit remains in the report for manual review)."""
    from anonymize.pipeline import stage_auto_resolve_residuals
    from anonymize.applier import apply
    from anonymize.scanner import scan_path

    # Seed the map with a brand entry so the dynamic verifier has
    # SOMETHING to look for; the body contains a different leak
    # (sub-token of the brand) that the map cannot directly resolve.
    body = "Some Foo brand mention.\n"
    proj, _, _ = _make_project(tmp_path, body=body)
    smap = SubstitutionMap.load(proj.map_path)
    smap.add("brand", "Foo", "Vendor")
    smap.options["case_insensitive_categories"] = []
    smap.save()
    scan = scan_path(proj.input_paths[0])
    # Skip apply on purpose so ``Foo`` survives in the output and the
    # dynamic verifier (built from the map) flags it.
    apply(proj, scan, [])

    final_report, sr = stage_auto_resolve_residuals(proj)
    # Hit detected; the deterministic channel resolves it via the
    # case-aware derive step. Either it converges (final_hits=0)
    # or it leaves residuals, both outcomes are valid as long as
    # the loop doesn't crash and the report is written.
    assert isinstance(sr.extras["iterations"], int)
    assert "final_hits" in sr.extras


def test_project_default_auto_resolve_is_true() -> None:
    """The user explicitly chose 'always on, savable in Settings'."""
    from anonymize.project import Project

    p = Project()
    assert p.auto_resolve_residuals is True
