"""Shared dataclasses for candidates produced by the Tier 0/1 stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidateOccurrence:
    """One observation of a candidate value in a specific file/segment."""

    value: str
    file_rel: str
    seg_id: str
    offset: int


@dataclass
class Candidate:
    """A unique value flagged as anonymization-worthy by the engine.

    Tier-0 (rules) candidates have ``confidence=1.0`` and ``tier="T0_rules"``;
    Tier-1 (LLM) candidates carry confidence + rationale from the model.
    """

    value: str
    category: str = "other"
    suggested_placeholder: str = ""
    confidence: float = 0.0
    rationale: str = ""
    count: int = 0
    examples: list[str] = field(default_factory=list)
    tier: str = "T1_llm"
    rule_name: str = ""
    # Critic enrichment
    critic_is_real_leak: str = ""  # "yes" | "no" | "uncertain" | ""
    critic_category_correct: str = ""
    critic_placeholder_safe: str = ""
    critic_confidence: float = 0.0
    critic_note: str = ""
    self_consistency: float = 1.0  # ratio of agreement (1.0 = single shot)
    # Operator decision recorded during Review on pending rows:
    # ``"pending"`` (default), ``"approve"``, ``"skip"``.  Round-trips
    # through ``needs_review.yml`` so the approval state survives a
    # GUI restart.
    decision: str = "pending"
    # ``True`` when an operator has hand-edited ``value`` or
    # ``suggested_placeholder`` from the GUI. The pipeline's merge
    # logic uses this flag to refuse to clobber user edits when the
    # scan/detect stages re-run.
    user_edited: bool = False
    # Snapshot of ``value`` the first time the detector produced it.
    # When the operator renames ``value`` we keep ``original_value``
    # untouched so subsequent scan re-runs can still match this row
    # by its original detection key (otherwise the renamed row would
    # look like a brand-new candidate alongside the freshly-detected
    # original).  Empty when the value has never been edited.
    original_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "category": self.category,
            "suggested_placeholder": self.suggested_placeholder,
            "confidence": round(float(self.confidence), 3),
            "rationale": self.rationale,
            "count": int(self.count),
            "examples": list(self.examples),
            "tier": self.tier,
            "rule_name": self.rule_name,
            "critic_is_real_leak": self.critic_is_real_leak,
            "critic_category_correct": self.critic_category_correct,
            "critic_placeholder_safe": self.critic_placeholder_safe,
            "critic_confidence": round(float(self.critic_confidence), 3),
            "critic_note": self.critic_note,
            "self_consistency": round(float(self.self_consistency), 3),
            "decision": self.decision,
            "user_edited": bool(self.user_edited),
            "original_value": self.original_value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        return cls(
            value=str(d.get("value", "")),
            category=str(d.get("category", "other")),
            suggested_placeholder=str(d.get("suggested_placeholder", "")),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            rationale=str(d.get("rationale", "")),
            count=int(d.get("count", 0) or 0),
            examples=list(d.get("examples") or []),
            tier=str(d.get("tier", "T1_llm")),
            rule_name=str(d.get("rule_name", "")),
            critic_is_real_leak=str(d.get("critic_is_real_leak", "")),
            critic_category_correct=str(d.get("critic_category_correct", "")),
            critic_placeholder_safe=str(d.get("critic_placeholder_safe", "")),
            critic_confidence=float(d.get("critic_confidence", 0.0) or 0.0),
            critic_note=str(d.get("critic_note", "")),
            self_consistency=float(d.get("self_consistency", 1.0) or 1.0),
            decision=str(d.get("decision", "pending")),
            user_edited=bool(d.get("user_edited", False)),
            original_value=str(d.get("original_value", "")),
        )


__all__ = ["Candidate", "CandidateOccurrence"]
