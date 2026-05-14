"""Tests for the triage auto-reject and placeholder-collision logic.

Behaviour verified:

* ``critic_is_real_leak == "no"`` with high ``critic_confidence`` is silently
  dropped (rejected bucket), not shown to the human reviewer.
* A candidate whose ``suggested_placeholder`` matches its ``value`` is never
  auto-promoted, even if the critic says yes with high confidence.
* :func:`anonymize.sub_map._bump_placeholder` produces a unique variant
  preserving width/format.
"""
from __future__ import annotations

from anonymize.candidates import Candidate
from anonymize.sub_map import SubstitutionMap, _bump_placeholder
from anonymize.triage import TriageConfig, triage


def _cand(value, placeholder, *, det=0.95, crit=0.95, leak="yes", cat="phones") -> Candidate:
    c = Candidate(
        value=value,
        category=cat,
        suggested_placeholder=placeholder,
        confidence=det,
        rationale="",
        count=1,
        examples=[],
        tier="T1_llm",
    )
    c.critic_is_real_leak = leak
    c.critic_category_correct = "yes"
    c.critic_placeholder_safe = "yes"
    c.critic_confidence = crit
    c.critic_note = ""
    return c


def test_critic_no_high_confidence_is_rejected():
    cands = [
        _cand("OAuth2", "VendorAuth", det=0.7, crit=0.95, leak="no"),
        _cand("Samsung SM-A920F", "VendorPhone", det=0.7, crit=0.92, leak="no"),
    ]
    res = triage(t0_candidates=[], t1_candidates=cands, config=TriageConfig())
    assert len(res.rejected) == 2
    assert len(res.needs_review) == 0
    assert len(res.auto_t1) == 0


def test_critic_no_low_confidence_goes_to_review():
    cand = _cand("foo.bar", "vendor.bar", det=0.8, crit=0.40, leak="no")
    res = triage(t0_candidates=[], t1_candidates=[cand], config=TriageConfig())
    assert res.rejected == []
    assert len(res.needs_review) == 1


def test_placeholder_equal_to_value_is_not_auto_promoted():
    cand = _cand("131054491", "131054491", det=1.0, crit=0.99, leak="yes")
    res = triage(t0_candidates=[], t1_candidates=[cand], config=TriageConfig())
    assert res.auto_t1 == []
    assert len(res.needs_review) == 1


def test_critic_no_with_blank_placeholder_is_rejected():
    cand = _cand("/api/v1/login", "", det=0.7, crit=0.92, leak="no")
    res = triage(t0_candidates=[], t1_candidates=[cand], config=TriageConfig())
    assert res.rejected and not res.needs_review and not res.auto_t1


def test_bump_placeholder_increments_trailing_digits():
    used = {"+393440000001"}
    out = _bump_placeholder("+393440000001", used)
    assert out == "+393440000002"
    assert len(out) == len("+393440000001")


def test_bump_placeholder_increments_hex_tail():
    used = {"00000000000000000000000000000001"}
    out = _bump_placeholder("00000000000000000000000000000001", used)
    assert out == "00000000000000000000000000000002"
    assert len(out) == 32


def test_bump_placeholder_falls_back_to_suffix():
    used = {"vendor.example"}
    out = _bump_placeholder("vendor.example", used)
    assert out != "vendor.example"
    assert "vendor.example" in out  # variant


def test_merge_candidates_resolves_collision(tmp_path):
    map_path = tmp_path / "substitution_map.yml"
    smap = SubstitutionMap.load(map_path)
    cands = [
        _cand("697150684", "+393440000001"),
        _cand("131054491", "+393440000001"),
    ]
    smap.merge_candidates(cands)
    phones = smap.entries["phones"]
    tos = [it["to"] for it in phones]
    assert "+393440000001" in tos
    assert "+393440000002" in tos
    assert len(set(tos)) == len(tos), "placeholders must be unique"


def test_merge_candidates_drops_value_equal_to_placeholder(tmp_path):
    map_path = tmp_path / "substitution_map.yml"
    smap = SubstitutionMap.load(map_path)
    cands = [_cand("131054491", "131054491")]
    smap.merge_candidates(cands)
    assert smap.entries.get("phones", []) == []
