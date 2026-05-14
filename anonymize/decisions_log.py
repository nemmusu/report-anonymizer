"""Persistent log of the operator's decisions and Tier-0 stable assignments.

Two responsibilities:

* assign deterministic, stable placeholders to Tier-0 numeric templates
  (``+390000000{n:04d}`` etc.) - so that ``+393331111111`` always resolves to
  the same anonymized number across runs of the same project,

* record every operator promote/reject/edit so that future runs can use the
  top-N decisions as few-shot examples in the LLM prompt (active learning).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class DecisionsLog:
    path: Path
    # rule_name -> { value: placeholder } - persistent
    t0_assignments: dict[str, dict[str, str]] = field(default_factory=dict)
    # rule_name -> next free index
    t0_indices: dict[str, int] = field(default_factory=dict)
    # operator decisions appended in order
    decisions: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "DecisionsLog":
        log = cls(path=path)
        if not path.exists():
            return log
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    kind = rec.get("kind")
                    if kind == "t0_assign":
                        rn = rec["rule"]
                        log.t0_assignments.setdefault(rn, {})[rec["value"]] = rec["placeholder"]
                        idx = int(rec.get("n", 0))
                        if idx + 1 > log.t0_indices.get(rn, 0):
                            log.t0_indices[rn] = idx + 1
                    elif kind == "t0_index":
                        log.t0_indices[rec["rule"]] = int(rec["next"])
                    elif kind == "decision":
                        log.decisions.append(rec)
        except Exception:
            pass
        return log

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def next_index_for(self, rule_name: str) -> int:
        idx = self.t0_indices.get(rule_name, 1)
        self.t0_indices[rule_name] = idx + 1
        self._append({"kind": "t0_index", "rule": rule_name, "next": idx + 1})
        return idx

    def record_t0_assignment(
        self, rule_name: str, value: str, placeholder: str
    ) -> None:
        cur = self.t0_assignments.setdefault(rule_name, {})
        if value in cur:
            return
        cur[value] = placeholder
        self._append(
            {
                "kind": "t0_assign",
                "rule": rule_name,
                "value": value,
                "placeholder": placeholder,
            }
        )

    def get_t0_assignment(self, rule_name: str, value: str) -> str | None:
        return self.t0_assignments.get(rule_name, {}).get(value)

    def record_decision(
        self,
        action: str,
        value: str,
        placeholder: str,
        category: str,
        meta: dict | None = None,
    ) -> None:
        rec = {
            "kind": "decision",
            "action": action,
            "value": value,
            "placeholder": placeholder,
            "category": category,
        }
        if meta:
            rec.update(meta)
        self.decisions.append(rec)
        self._append(rec)

    def few_shot_examples(self, n: int = 8) -> list[dict]:
        """Return the most recent ``n`` decisions for use as LLM few-shot."""
        return self.decisions[-n:][::-1]


__all__ = ["DecisionsLog"]
