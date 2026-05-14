"""Canonical substitution map (``config/substitution_map.yml``).

The map is the single source of truth for what gets substituted by the
applier. The Tier-0 / Tier-1 stages produce candidates that, once approved,
end up in this YAML. The applier reads the map back and converts it into
:class:`SubstitutionRule` entries.

Production-grade validators (:func:`validate_invariants`):
  * placeholder uniqueness across categories (warn on collision),
  * no ``from -> to`` cycle (apply twice must be idempotent),
  * longest-match-first ordering of compiled rules (verified at build time).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from .candidates import Candidate
from .format_adapters.base import SubstitutionRule, apply_to_text


_logger = logging.getLogger(__name__)


CANONICAL_CATEGORIES: tuple[str, ...] = (
    "brand",
    "network",
    "app_packages",
    "phones",
    "emails",
    "keys",
    "credentials",
    "headers",
    "user_agents",
    "ids",
    "infra_ids",
    "other",
)

DEFAULT_CASE_INSENSITIVE_CATEGORIES: tuple[str, ...] = (
    "brand", "network", "app_packages",
)

# Categories that disappeared from ``CANONICAL_CATEGORIES`` between
# releases.  When ``SubstitutionMap.load`` finds one of these as a
# top-level YAML key it warns and ignores its entries: the user is
# expected to rename the key by hand (the project is still private
# and the call sites here are stable).
_LEGACY_CATEGORY_KEYS: tuple[str, ...] = ("android",)


def _bump_placeholder(base: str, used: set[str], *, max_tries: int = 1000) -> str:
    """Return a variant of ``base`` not present in ``used``.

    Strategy (preserves the visual format as much as possible):
      1. If ``base`` ends with one or more ASCII digits, increment them while
         preserving the original width (zero-padded). Wraps to a wider field
         when overflowing.
      2. If ``base`` ends with ``[a-fA-F0-9]+`` (hex tail), increment the hex
         tail preserving width.
      3. Otherwise append a numeric suffix ``-N`` and bump it.
    """
    import re as _re

    if not base:
        base = "placeholder"
    cur = base

    m = _re.search(r"(\d+)$", base)
    if m:
        prefix = base[: m.start()]
        digits = m.group(1)
        width = len(digits)
        n = int(digits) + 1
        for _ in range(max_tries):
            cand = f"{prefix}{n:0{width}d}"
            if cand not in used:
                return cand
            n += 1
        return f"{prefix}{n}"

    m = _re.search(r"([a-fA-F0-9]+)$", base)
    if m and len(m.group(1)) >= 4:
        prefix = base[: m.start()]
        hex_tail = m.group(1)
        width = len(hex_tail)
        n = int(hex_tail, 16) + 1
        for _ in range(max_tries):
            cand = f"{prefix}{n:0{width}x}"
            if cand not in used:
                return cand
            n += 1
        return f"{prefix}{n:x}"

    n = 2
    for _ in range(max_tries):
        cand = f"{base}-{n}"
        if cand not in used:
            return cand
        n += 1
    return f"{base}-{n}"


@dataclass
class InvariantViolation:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class SubstitutionMap:
    path: Path
    options: dict = field(default_factory=dict)
    entries: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SubstitutionMap":
        if not path.exists():
            return cls(path=path, options={
                "longest_first": True,
                "case_insensitive_categories": list(DEFAULT_CASE_INSENSITIVE_CATEGORIES),
            }, entries={c: [] for c in CANONICAL_CATEGORIES})
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        opts = data.get("options") or {
            "longest_first": True,
            "case_insensitive_categories": list(DEFAULT_CASE_INSENSITIVE_CATEGORIES),
        }
        entries: dict[str, list[dict]] = {}
        for c in CANONICAL_CATEGORIES:
            entries[c] = list(data.get(c) or [])
        # Legacy category keys (e.g. ``android:`` from before the
        # ``app_packages`` rename) are warned about and ignored.
        # The project doesn't ship a migration helper; renaming the
        # YAML key by hand is the expected fix.
        for legacy in _LEGACY_CATEGORY_KEYS:
            if data.get(legacy):
                _logger.warning(
                    "Ignoring legacy %r category in %s: rename the "
                    "key by hand (see README → 'What it anonymizes').",
                    legacy, path,
                )
        return cls(path=path, options=opts, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out: dict = {"version": 1, "options": self.options}
        for c in CANONICAL_CATEGORIES:
            if self.entries.get(c):
                out[c] = self.entries[c]
        yaml_text = yaml.safe_dump(
            out, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        self.path.write_text(yaml_text, encoding="utf-8")

    # ---- queries ------------------------------------------------------------

    def keys(self) -> set[str]:
        out: set[str] = set()
        for items in self.entries.values():
            for it in items:
                f = it.get("from")
                if f:
                    out.add(str(f))
        return out

    def find(self, value: str) -> tuple[str, dict] | None:
        for cat, items in self.entries.items():
            for it in items:
                if str(it.get("from", "")) == value:
                    return cat, it
        return None

    # ---- mutations ----------------------------------------------------------

    def add(
        self,
        category: str,
        from_: str,
        to: str,
        *,
        mapping_id: str | None = None,
    ) -> str:
        if category not in CANONICAL_CATEGORIES:
            category = "other"
        if not mapping_id:
            existing = sum(len(v) for v in self.entries.values())
            mapping_id = f"{category}:{existing+1:04d}"
        if any(it.get("from") == from_ for it in self.entries.setdefault(category, [])):
            return mapping_id
        self.entries[category].append({"from": from_, "to": to, "id": mapping_id})
        return mapping_id

    def update(self, mapping_id: str, *, to: str | None = None) -> bool:
        for cat, items in self.entries.items():
            for it in items:
                if it.get("id") == mapping_id:
                    if to is not None:
                        it["to"] = to
                    return True
        return False

    def remove(self, mapping_id: str) -> bool:
        for cat, items in self.entries.items():
            for i, it in enumerate(items):
                if it.get("id") == mapping_id:
                    del items[i]
                    return True
        return False

    def merge_candidates(
        self, cands: Iterable[Candidate], *, default_category: str = "other"
    ) -> int:
        from .placeholders import (
            clamp_to_value_length,
            is_overlong_placeholder,
        )

        added = 0
        used_to_by_cat: dict[str, set[str]] = {}
        for cat, items in self.entries.items():
            used_to_by_cat[cat] = {str(it.get("to", "")) for it in items if it.get("to")}

        for c in cands:
            cat = c.category if c.category in CANONICAL_CATEGORIES else default_category
            value = c.value
            placeholder = c.suggested_placeholder
            if any(it.get("from") == value for it in self.entries.get(cat, [])):
                continue
            if not placeholder or placeholder.strip() == value.strip():
                continue
            # Reject placeholders that would overflow the source rect
            # in PDF in-place: a 5-char ``+39LAB`` paired with a 13-char
            # ``+390000000001`` placeholder is a stale-map artefact that
            # the apply stage cannot render without spilling into the
            # adjacent column. Clamp it down to the source's length so
            # downstream rendering stays safe.
            if is_overlong_placeholder(value, placeholder):
                placeholder = clamp_to_value_length(value, placeholder)
            used = used_to_by_cat.setdefault(cat, set())
            if placeholder in used:
                placeholder = _bump_placeholder(placeholder, used)
            used.add(placeholder)
            self.add(cat, value, placeholder)
            added += 1
        return added

    # ---- compile to rules ---------------------------------------------------

    def to_rules(self, *, tier: str = "T2_human") -> list[SubstitutionRule]:
        ci = set(self.options.get("case_insensitive_categories") or [])
        out: list[SubstitutionRule] = []
        for cat in CANONICAL_CATEGORIES:
            for it in self.entries.get(cat, []) or []:
                f = str(it.get("from", ""))
                t = str(it.get("to", ""))
                if not f:
                    continue
                out.append(
                    SubstitutionRule(
                        from_=f,
                        to=t,
                        category=cat,
                        mapping_id=str(it.get("id", "")),
                        tier=tier,
                        case_insensitive=cat in ci,
                    )
                )
        if self.options.get("longest_first", True):
            out.sort(key=lambda r: -len(r.from_))
        return out

    # ---- invariants ---------------------------------------------------------

    def validate_invariants(self) -> list[InvariantViolation]:
        """Check production invariants and return a list of violations."""
        violations: list[InvariantViolation] = []
        # 1. uniqueness of `from` across categories
        seen_from: dict[str, str] = {}
        for cat, items in self.entries.items():
            for it in items:
                f = str(it.get("from", "") or "")
                if not f:
                    continue
                if f in seen_from and seen_from[f] != cat:
                    violations.append(
                        InvariantViolation(
                            code="duplicate_from",
                            message=f"value {f!r} is mapped in two categories ({seen_from[f]} and {cat})",
                            detail={"value": f, "categories": [seen_from[f], cat]},
                        )
                    )
                seen_from.setdefault(f, cat)
        # 2. no cycle: applying the rules twice produces the same text
        rules = self.to_rules()
        for cat, items in self.entries.items():
            for it in items:
                f = str(it.get("from", "") or "")
                t = str(it.get("to", "") or "")
                if not f or not t:
                    continue
                pass1, _ = apply_to_text(f, rules)
                pass2, _ = apply_to_text(pass1, rules)
                if pass1 != pass2:
                    violations.append(
                        InvariantViolation(
                            code="non_idempotent",
                            message=f"rule {f!r} -> {t!r} is not idempotent under the full ruleset",
                            detail={"value": f},
                        )
                    )
        # 3. longest-match-first ordering: first rule should have largest 'from'
        if rules:
            for i in range(1, len(rules)):
                if len(rules[i].from_) > len(rules[i - 1].from_):
                    violations.append(
                        InvariantViolation(
                            code="ordering",
                            message="rules not sorted longest-first",
                            detail={"index": i},
                        )
                    )
                    break
        return violations


_LEGACY_HEX_PLACEHOLDER_RE = __import__("re").compile(r"^0{8,}[a-fA-F0-9]+$")
_HEX_VALUE_RE = __import__("re").compile(r"^[a-fA-F0-9]+$")


def rewrite_placeholders(
    smap: "SubstitutionMap",
    *,
    categories: Iterable[str],
    fix_overlong: bool = True,
) -> list[dict]:
    """Upgrade existing map entries to the current shape-preserving
    strategies (one-shot migration).

    For each entry in the requested ``categories`` whose stored ``to:``
    no longer matches the strategy's output, replace ``to:`` with the
    new placeholder and record the change. Returns a list of
    ``{"id", "category", "from", "old_to", "new_to", "reason"}``
    dicts the caller can render as a diff.

    The migration uses an in-memory :class:`DecisionsLog`; the trailing
    sequential index is assigned in entry-order so a re-run on the
    same input is idempotent (``keys:0040`` → seq 1, ``0041`` → 2, …).

    When ``fix_overlong`` is ``True`` (default) the migration ALSO
    walks **every** category, regardless of the ``categories`` filter,
    and clamps placeholders that are disproportionately longer than
    their ``from`` value (the "+39LAB → +390000000001" stale-map
    artefact). This is the single source of truth for what counts as
    a length-mismatched entry, shared with
    :meth:`SubstitutionMap.merge_candidates`.
    """
    from .decisions_log import DecisionsLog
    from .placeholders import (
        clamp_to_value_length,
        hex_keep_prefix,
        is_overlong_placeholder,
        phone_intl,
    )

    cats = {c.strip().lower() for c in categories if c}
    log = DecisionsLog(path=Path("/dev/null"))
    log._append = lambda rec: None  # type: ignore[assignment]

    changes: list[dict] = []
    for cat in CANONICAL_CATEGORIES:
        for it in smap.entries.get(cat, []) or []:
            f = str(it.get("from", "") or "")
            t = str(it.get("to", "") or "")
            mid = str(it.get("id", "") or "")
            if not f:
                continue
            new_to: str | None = None
            reason = ""
            # Strategy upgrades, only for categories the user opted in.
            if cat in cats:
                if cat == "keys":
                    if (
                        _HEX_VALUE_RE.match(f)
                        and _LEGACY_HEX_PLACEHOLDER_RE.match(t)
                    ):
                        new_to = hex_keep_prefix(
                            f, log=log, rule_name=mid or "_migrate_keys"
                        )
                        reason = "hex_keep_prefix"
                elif cat == "phones":
                    candidate = phone_intl(
                        f, log=log, rule_name=mid or "_migrate_phones"
                    )
                    if candidate != t:
                        new_to = candidate
                        reason = "phone_intl"
            # Length-mismatch fix, applied to ALL categories regardless
            # of strategy. This is what protects PDF in-place from the
            # "table cell overflow" regression on stale entries.
            target_to = new_to if new_to is not None else t
            if fix_overlong and is_overlong_placeholder(f, target_to):
                clamped = clamp_to_value_length(f, target_to)
                if clamped != target_to:
                    new_to = clamped
                    reason = (reason + "+" if reason else "") + "clamp_overlong"
            if new_to is None or new_to == t:
                continue
            changes.append({
                "id": mid, "category": cat, "from": f,
                "old_to": t, "new_to": new_to, "reason": reason,
            })
            it["to"] = new_to
    return changes


__all__ = [
    "SubstitutionMap",
    "CANONICAL_CATEGORIES",
    "InvariantViolation",
    "rewrite_placeholders",
]
