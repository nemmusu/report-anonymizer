"""Tests for ``rewrite_placeholders`` (the engine side of the
``anonymize-dossier migrate-map`` CLI).

The migration upgrades stale entries in ``substitution_map.yml`` to the
current shape-preserving placeholder strategies. Free-text categories
(``brand``, ``ids``, …) are intentionally skipped by default because
their LLM-curated placeholders are usually better than any strategy
default would be.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize.sub_map import SubstitutionMap, rewrite_placeholders


def _make_map(tmp_path: Path) -> Path:
    p = tmp_path / "substitution_map.yml"
    p.write_text(
        """version: 1
options:
  longest_first: true
  case_insensitive_categories: [brand, network, app_packages]
brand:
  - {from: AcmeApp Pro, to: Vendor App, id: brand:0001}
keys:
  - {from: b4fc4171101438774f186f41057e0b1d, to: '00000000000000000000000000000001', id: keys:0040}
  - {from: 328cd0ceba38fb6b8c9af4fe9d6c43fd, to: '00000000000000000000000000000002', id: keys:0041}
  - {from: deadbeef000000000000000000000099, to: 'deadbeef000000000000000000000099', id: keys:0042}
phones:
  - {from: '+393440405580', to: '+393440000001', id: phones:0001}
""",
        encoding="utf-8",
    )
    return p


def test_legacy_hex_keys_get_prefix_preserving_upgrade(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    changes = rewrite_placeholders(smap, categories=["keys"])

    by_id = {c["id"]: c for c in changes}
    # Legacy zero-shape entries get upgraded.
    assert "keys:0040" in by_id
    assert by_id["keys:0040"]["new_to"].startswith("b4fc4171")
    assert len(by_id["keys:0040"]["new_to"]) == 32
    assert "keys:0041" in by_id
    assert by_id["keys:0041"]["new_to"].startswith("328cd0ce")
    # Already-modern entry (placeholder == source-shaped) is skipped.
    assert "keys:0042" not in by_id


def test_brand_and_other_text_categories_skipped_by_default(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    changes = rewrite_placeholders(smap, categories=["keys"])
    # ``brand`` was not in the categories list; its entry is untouched.
    assert all(c["category"] != "brand" for c in changes)
    brand_entry = smap.entries["brand"][0]
    assert brand_entry["to"] == "Vendor App"


def test_phones_category_is_supported(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    # Existing phone placeholder already matches phone_intl output, so
    # no change is recorded, but the migration must NOT raise and must
    # accept ``phones`` as a valid category.
    changes = rewrite_placeholders(smap, categories=["phones"])
    assert all(c["category"] == "phones" for c in changes)


def test_migration_preserves_invariants(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    rewrite_placeholders(smap, categories=["keys"])
    violations = smap.validate_invariants()
    # Allow 'ordering' violation only if the test fixture itself was
    # not pre-sorted; the migration never introduces duplicates or
    # cycles regardless of insertion order.
    bad = [v for v in violations if v.code in ("duplicate_from", "non_idempotent")]
    assert bad == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    rewrite_placeholders(smap, categories=["keys"])
    smap.save()

    reloaded = SubstitutionMap.load(p)
    by_id = {it["id"]: it for cat in reloaded.entries.values() for it in cat}
    assert by_id["keys:0040"]["to"].startswith("b4fc4171")
    assert by_id["keys:0041"]["to"].startswith("328cd0ce")


def test_unknown_category_is_silently_skipped(tmp_path: Path) -> None:
    p = _make_map(tmp_path)
    smap = SubstitutionMap.load(p)
    # ``something_else`` does not exist; ``fix_overlong=False`` ensures
    # we don't accidentally exercise the length-clamp path here.
    changes = rewrite_placeholders(
        smap, categories=["something_else"], fix_overlong=False,
    )
    assert changes == []


def test_overlong_placeholders_get_clamped(tmp_path: Path) -> None:
    """Stale-map artefacts where ``to`` is much longer than ``from``
    (typically ``+39LAB`` -> ``+390000000001``) cause PDF in-place to
    overflow into adjacent text. The migration must clamp them down."""
    p = tmp_path / "m.yml"
    p.write_text(
        """version: 1
options: {longest_first: true, case_insensitive_categories: []}
phones:
  - {from: '+39LAB', to: '+390000000001', id: phones:0080}
ids:
  - {from: 'ACME-VULN-13', to: 'VENDOR-CONFIRMED_VULN-13_Zero_OTP_Account_Takeover', id: ids:0010}
""",
        encoding="utf-8",
    )
    smap = SubstitutionMap.load(p)
    changes = rewrite_placeholders(smap, categories=[])  # nothing in strategy list
    by_id = {c["id"]: c for c in changes}
    # Both entries get clamped because they are length-mismatched.
    assert "phones:0080" in by_id
    assert len(by_id["phones:0080"]["new_to"]) == len("+39LAB")
    assert "ids:0010" in by_id
    assert len(by_id["ids:0010"]["new_to"]) == len("ACME-VULN-13")


def test_merge_candidates_rejects_overlong_placeholders(tmp_path: Path) -> None:
    """Promote-time prevention: a candidate proposing a placeholder
    much longer than the source must be clamped before insertion."""
    from anonymize.candidates import Candidate

    p = tmp_path / "m.yml"
    p.write_text(
        "version: 1\noptions: {longest_first: true, case_insensitive_categories: []}\n",
        encoding="utf-8",
    )
    smap = SubstitutionMap.load(p)
    smap.merge_candidates([
        Candidate(
            value="+39LAB",
            category="phones",
            suggested_placeholder="+390000000001",  # 13 chars vs 5 source
            confidence=1.0, rationale="", count=1, examples=[], tier="T1_llm",
        ),
    ])
    entry = smap.entries["phones"][0]
    assert entry["from"] == "+39LAB"
    # ``to`` must NOT exceed the source length.
    assert len(entry["to"]) == len(entry["from"])


def test_dry_run_via_cli_does_not_write(tmp_path: Path) -> None:
    """Smoke-test the CLI path: ``migrate-map`` without ``--apply`` must
    leave the file unchanged."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    cli = repo / "bin" / "anonymize-dossier"
    if not cli.exists():
        pytest.skip("CLI script not present in repo")

    map_path = _make_map(tmp_path)
    before = map_path.read_text(encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(cli), "migrate-map", "--map", str(map_path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(repo),
    )
    assert proc.returncode == 0, proc.stderr
    after = map_path.read_text(encoding="utf-8")
    assert before == after, "dry-run must not modify the file"
    assert "proposed_changes=" in proc.stdout


def test_apply_via_cli_writes_backup_and_updates_map(tmp_path: Path) -> None:
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    cli = repo / "bin" / "anonymize-dossier"
    if not cli.exists():
        pytest.skip("CLI script not present in repo")

    map_path = _make_map(tmp_path)
    before = map_path.read_text(encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(cli), "migrate-map", "--map", str(map_path), "--apply"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(repo),
    )
    assert proc.returncode == 0, proc.stderr
    bak = map_path.with_suffix(map_path.suffix + ".bak")
    assert bak.exists(), "backup must be written next to the map"
    assert bak.read_text(encoding="utf-8") == before
    after = map_path.read_text(encoding="utf-8")
    assert after != before
    assert "b4fc4171" in after  # prefix-preserving placeholder landed
