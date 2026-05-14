"""Tier 0 deterministic rules pass.

Reads ``config/leak_patterns.yml`` (the ``auto_promote`` section) and
produces substitutions deterministically without invoking the LLM.

Each rule renders its placeholder via one of two routes:

* ``placeholder_strategy`` (preferred): a named function in
  :mod:`anonymize.placeholders`. Production strategies preserve byte
  length and the source's *shape* (country code + carrier for phones,
  first 8 chars for hex tokens, etc.) so the PDF in-place adapter does
  not have to reflow.
* ``placeholder_template`` (legacy / fallback): literal string with
  ``{n}`` / ``{n:04d}`` / ``{zeros}`` / ``{value}`` substitutions.

The output of :func:`run_rules_pass` is a list of :class:`Candidate`
records tagged ``tier="T0_rules"`` plus per-occurrence telemetry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from .candidates import Candidate, CandidateOccurrence
from .decisions_log import DecisionsLog
from .placeholders import resolve_strategy as _resolve_strategy_fn
from .scanner import ScanResult


@dataclass
class _CompiledRule:
    name: str
    category: str
    pattern: re.Pattern
    placeholder_template: str
    allow: list[re.Pattern] = field(default_factory=list)
    placeholder_strategy: str = ""


def _load_rules(patterns_path: Path) -> list[_CompiledRule]:
    text = patterns_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    auto = data.get("auto_promote") or []
    out: list[_CompiledRule] = []
    for r in auto:
        try:
            out.append(
                _CompiledRule(
                    name=str(r["name"]),
                    category=str(r.get("category") or "other"),
                    pattern=re.compile(r["regex"]),
                    placeholder_template=str(r.get("placeholder_template") or ""),
                    allow=[re.compile(p) for p in (r.get("allow") or [])],
                    placeholder_strategy=str(r.get("placeholder_strategy") or ""),
                )
            )
        except Exception:
            continue
    return out


def _is_allowed(value: str, allow: list[re.Pattern]) -> bool:
    return any(p.fullmatch(value) or p.search(value) for p in allow)


# ---- placeholder strategies -------------------------------------------------


def _resolve_placeholder(
    rule: _CompiledRule,
    value: str,
    *,
    log: DecisionsLog,
) -> str:
    strategy = (rule.placeholder_strategy or "").strip().lower()
    if strategy:
        out = _resolve_strategy_fn(strategy, value, log=log, rule_name=rule.name)
        if out is not None:
            return out
    tmpl = rule.placeholder_template
    if not tmpl:
        return value
    if "{n" in tmpl:
        n = log.next_index_for(rule.name)
        return tmpl.format(n=n, zeros="0" * len(value), value=value)
    if "{zeros}" in tmpl:
        return tmpl.format(n=0, zeros="0" * len(value), value=value)
    if "{value}" in tmpl:
        return tmpl.format(n=0, zeros="0" * len(value), value=value)
    return tmpl


def run_rules_pass(
    scan: ScanResult,
    *,
    patterns_path: Path,
    decisions: DecisionsLog,
    existing_map_keys: Optional[set[str]] = None,
    safe_values: Optional[set[str]] = None,
) -> tuple[list[Candidate], list[CandidateOccurrence]]:
    """Sweep every text-like file in ``scan`` and produce auto-promote
    candidates by applying ``leak_patterns.yml`` ``auto_promote`` rules.

    Already-mapped values (in ``existing_map_keys``) and safe values are
    skipped (they are either already covered by the canonical map or are
    standard terms like AES/SHA-256).
    """
    rules = _load_rules(patterns_path)
    if not rules:
        return [], []
    seen: dict[str, Candidate] = {}
    occurrences: list[CandidateOccurrence] = []
    existing_map_keys = existing_map_keys or set()
    safe_values = safe_values or set()

    # Dedup occurrences across overlapping rules: each (file, seg, offset)
    # is counted at most once even if multiple rules match the same span.
    occ_seen: set[tuple[str, str, int]] = set()

    for sf in scan.text_like:
        if sf.skipped:
            continue
        try:
            segments = sf.adapter.extract(sf.path)
        except Exception:
            continue
        for seg in segments:
            text = seg.text
            if not text:
                continue
            for rule in rules:
                for m in rule.pattern.finditer(text):
                    value = m.group(0)
                    if not value:
                        continue
                    if _is_allowed(value, rule.allow):
                        continue
                    if value in existing_map_keys:
                        continue
                    if value in safe_values:
                        continue
                    occ_key = (str(sf.rel), seg.seg_id, m.start())
                    if occ_key in occ_seen:
                        continue
                    occ_seen.add(occ_key)
                    occurrences.append(
                        CandidateOccurrence(
                            value=value,
                            file_rel=str(sf.rel),
                            seg_id=seg.seg_id,
                            offset=m.start(),
                        )
                    )
                    if value in seen:
                        seen[value].count += 1
                        ex = f"{sf.rel}:{seg.seg_id}@{m.start()}"
                        if len(seen[value].examples) < 5 and ex not in seen[value].examples:
                            seen[value].examples.append(ex)
                        continue
                    placeholder = _resolve_placeholder(
                        rule, value, log=decisions
                    )
                    cand = Candidate(
                        value=value,
                        category=rule.category,
                        suggested_placeholder=placeholder,
                        confidence=1.0,
                        rationale=f"Tier-0 rule {rule.name}",
                        count=1,
                        examples=[f"{sf.rel}:{seg.seg_id}@{m.start()}"],
                        tier="T0_rules",
                        rule_name=rule.name,
                    )
                    seen[value] = cand
                    decisions.record_t0_assignment(rule.name, value, placeholder)
    return list(seen.values()), occurrences


__all__ = ["run_rules_pass"]
