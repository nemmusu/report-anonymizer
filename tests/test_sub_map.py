"""Unit tests for SubstitutionMap and its invariants."""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize.sub_map import SubstitutionMap


def test_load_empty_map_creates_skeleton(tmp_path: Path) -> None:
    p = tmp_path / "map.yml"
    smap = SubstitutionMap.load(p)
    assert smap.entries == {c: [] for c in smap.entries}


def test_add_and_save_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "map.yml"
    smap = SubstitutionMap.load(p)
    smap.add("brand", "Acme", "Vendor-A")
    smap.save()
    again = SubstitutionMap.load(p)
    assert "Acme" in again.keys()
    cat, rec = again.find("Acme")
    assert cat == "brand" and rec["to"] == "Vendor-A"


def test_to_rules_longest_first(tmp_path: Path) -> None:
    p = tmp_path / "map.yml"
    smap = SubstitutionMap.load(p)
    smap.add("brand", "Acme", "V")
    smap.add("brand", "Acme Corp", "VC")
    rules = smap.to_rules()
    lengths = [len(r.from_) for r in rules]
    assert lengths == sorted(lengths, reverse=True)


def test_invariant_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "map.yml"
    smap = SubstitutionMap.load(p)
    smap.add("brand", "Acme", "Vendor-A")
    violations = smap.validate_invariants()
    assert not [v for v in violations if v.code == "non_idempotent"]


def test_invariant_detects_cycle(tmp_path: Path) -> None:
    p = tmp_path / "map.yml"
    smap = SubstitutionMap.load(p)
    smap.add("brand", "Acme", "Beta")
    smap.add("brand", "Beta", "Acme")
    violations = smap.validate_invariants()
    codes = [v.code for v in violations]
    assert "non_idempotent" in codes
