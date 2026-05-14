"""Triage Tier-1 LLM candidates into auto-promote / needs-review / rejected buckets.

A candidate is **auto-promoted** iff:
* ``confidence >= t_high``
* critic agrees: ``is_real_leak == "yes"`` AND ``placeholder_safe == "yes"``
* (optional) self-consistency vote at confidence in ``[t_low, t_high)`` was
  unanimous

A candidate is **auto-rejected** (silently dropped, not shown to the user) iff:
* the critic is confidently against it: ``is_real_leak == "no"`` AND
  ``critic_confidence >= t_critic_reject``. The detector can over-flag in
  pentest reports (file names, library names, OS versions, ...); a confident
  critic "no" means we should not bother the human.

Anything else goes to ``needs_review`` (the human has to decide).

Self-consistency voting (when enabled) is performed by re-running the
detector on the chunk with multiple seeds; this module is agnostic to how the
ratio gets populated -- it just consumes ``Candidate.self_consistency``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .candidates import Candidate


@dataclass
class TriageConfig:
    t_high: float = 0.92
    t_low: float = 0.75
    t_critic_reject: float = 0.85


@dataclass
class TriageResult:
    auto_t0: list[Candidate]
    auto_t1: list[Candidate]
    needs_review: list[Candidate]
    rejected: list[Candidate] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejected is None:
            self.rejected = []


def _is_auto_promote(c: Candidate, cfg: TriageConfig) -> bool:
    if c.tier == "T0_rules":
        return True
    if c.confidence < cfg.t_low:
        return False
    if c.critic_is_real_leak != "yes":
        return False
    if c.critic_placeholder_safe == "no":
        return False
    # Sanity: a "substitution" that doesn't change anything is not a
    # substitution. The detector occasionally emits placeholder == value
    # (or a trivial case-only variation) -- never auto-promote those: send
    # them to the human reviewer instead.
    if not c.suggested_placeholder.strip():
        return False
    if c.suggested_placeholder.strip() == c.value.strip():
        return False
    if c.suggested_placeholder.strip().lower() == c.value.strip().lower():
        return False
    if c.confidence >= cfg.t_high:
        return True
    # gray zone: require self-consistency vote >= 0.99 to auto-promote
    return c.self_consistency >= 0.99


def _is_auto_rejected(c: Candidate, cfg: TriageConfig) -> bool:
    """A confident critic 'no' silently drops the candidate.

    This avoids drowning the human reviewer in over-flagged noise from the
    detector (file names, library names, OS versions, ...).
    """
    if c.tier == "T0_rules":
        return False
    return (
        c.critic_is_real_leak == "no"
        and c.critic_confidence >= cfg.t_critic_reject
    )


def triage(
    *,
    t0_candidates: list[Candidate],
    t1_candidates: list[Candidate],
    config: TriageConfig | None = None,
) -> TriageResult:
    cfg = config or TriageConfig()
    auto_t1: list[Candidate] = []
    needs_review: list[Candidate] = []
    rejected: list[Candidate] = []
    for c in t1_candidates:
        if _is_auto_promote(c, cfg):
            auto_t1.append(c)
        elif _is_auto_rejected(c, cfg):
            rejected.append(c)
        else:
            needs_review.append(c)
    return TriageResult(
        auto_t0=list(t0_candidates),
        auto_t1=auto_t1,
        needs_review=needs_review,
        rejected=rejected,
    )


# ---- YAML I/O for the buckets ------------------------------------------------


def write_candidates_yaml(path: Path, candidates: list[Candidate]) -> None:
    """Write a sorted-by-category YAML representation of ``candidates``.

    The YAML is hand-editable; the GUI re-reads it after the operator has
    triaged. ``read_candidates_yaml`` is the inverse.
    """
    by_cat: dict[str, list[dict]] = {}
    for c in candidates:
        by_cat.setdefault(c.category or "other", []).append(c.to_dict())
    data = {"version": 1, "candidates_by_category": by_cat}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def read_candidates_yaml(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Candidate] = []
    for cat, items in (data.get("candidates_by_category") or {}).items():
        for it in items or []:
            if not isinstance(it, dict):
                continue
            it = {**it, "category": it.get("category") or cat}
            out.append(Candidate.from_dict(it))
    return out


# ---- auto-resolve helper ---------------------------------------------------


def _match_case(reference: str, target: str) -> str:
    """Apply the case shape of ``reference`` to ``target``.

    Used to derive a placeholder that visually matches the residual hit:
    if the verifier reports ``customerbrand`` (lowercase) and the map
    has ``CustomerBrand -> VendorVoice``, we want the new entry to read
    ``customerbrand -> vendorvoice``. UPPER, Title and lower are handled.
    Mixed / camel / preserved-as-is reference returns ``target``
    unchanged so we don't fabricate weird casings.
    """
    if not reference or not target:
        return target
    if reference.isupper():
        return target.upper()
    if reference.islower():
        return target.lower()
    # Title: only when *all* alpha words are capitalised, avoids
    # treating a camel-case reference as title.
    words = reference.split()
    if words and all(w[:1].isupper() and w[1:].islower() for w in words if w):
        return target.title()
    return target


def derive_placeholder_for_hit(
    hit_value: str,
    smap,
    *,
    pattern: str = "",
):
    """Build a :class:`Candidate` for a residual verifier hit by reusing
    the existing :class:`SubstitutionMap`.

    The auto-resolve loop calls this for every leak the verifier
    reports. When the map already contains a sibling entry for the hit
    (case-insensitively), we derive a new entry that mirrors the hit's
    casing, so a bare ``customerbrand`` residual gets ``vendorvoice``
    when the map has ``CustomerBrand -> VendorVoice``.

    Returns ``None`` when no case-insensitive ancestor for
    ``hit_value`` exists in the map (without an anchor we cannot pick
    a defensible placeholder; the hit falls through to manual review).

    ``regression:from-value`` hits *are* re-emitted: the map already
    knows the placeholder, so the auto-resolve loop can re-run apply
    on the previous output and possibly catch occurrences the in-place
    PDF adapter missed in its first pass (text-fragmentation issues
    sometimes shift after the initial substitutions, exposing matches
    that weren't visible to ``page.search_for`` originally).
    """
    if not hit_value:
        return None

    hit_lower = hit_value.lower()
    best_entry: tuple[str, dict] | None = None
    for cat, items in smap.entries.items():
        for it in items or []:
            f = str(it.get("from", "") or "")
            if not f:
                continue
            if f.lower() == hit_lower:
                # The exact value (case-insensitive) is already mapped.
                # Reuse the existing entry's category and placeholder
                # but with the hit's casing.
                best_entry = (cat, it)
                break
        if best_entry is not None:
            break

    if best_entry is None:
        return None
    cat, it = best_entry
    base_placeholder = str(it.get("to", "") or "")
    if not base_placeholder:
        return None

    new_to = _match_case(hit_value, base_placeholder)
    if new_to == hit_value:
        # Refuse to add a no-op entry (would confuse merge_candidates).
        return None

    # Same length-clamp safety net the rest of the pipeline uses.
    from .placeholders import clamp_to_value_length, is_overlong_placeholder

    if is_overlong_placeholder(hit_value, new_to):
        new_to = clamp_to_value_length(hit_value, new_to)
        if not new_to or new_to == hit_value:
            return None

    return Candidate(
        value=hit_value,
        category=cat,
        suggested_placeholder=new_to,
        confidence=1.0,
        rationale=f"auto-resolved from map entry {it.get('id', '')!s}",
        count=1,
        examples=[],
        tier="T3_auto_residual",
    )


__all__ = [
    "TriageConfig",
    "TriageResult",
    "triage",
    "write_candidates_yaml",
    "read_candidates_yaml",
    "derive_placeholder_for_hit",
]
