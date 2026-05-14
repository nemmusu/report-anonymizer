"""Tier 1b: LLM critic that double-checks the detector's output.

Critic batches are now sent in **parallel** via ``LLMClient.chat_many`` to
exploit the same ``--parallel`` slots used by the detector.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from jinja2 import Template

from .candidates import Candidate
from .detector import load_safe_terms
from .llm_client import ChatJob, LLMClient


@dataclass
class CriticConfig:
    system_prompt_path: Path
    user_template_path: Path
    safe_terms_path: Path
    batch_size: int = 12
    max_tokens: int = 2048
    base_seed: int = 7
    parallel: int = 4


def _normalize_verdict(d: dict) -> dict:
    yn = lambda x: str(x or "").strip().lower()
    return {
        "value": str(d.get("value") or ""),
        "is_real_leak": yn(d.get("is_real_leak")) if yn(d.get("is_real_leak")) in {"yes", "no", "uncertain"} else "uncertain",
        "category_correct": yn(d.get("category_correct")) if yn(d.get("category_correct")) in {"yes", "no"} else "yes",
        "placeholder_safe": yn(d.get("placeholder_safe")) if yn(d.get("placeholder_safe")) in {"yes", "no"} else "yes",
        "critic_confidence": _clip01(d.get("critic_confidence")),
        "note": str(d.get("note") or "")[:120],
    }


def _clip01(x) -> float:
    try:
        v = float(x)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, min(1.0, v))


def run_critic(
    candidates: list[Candidate],
    *,
    llm: LLMClient,
    config: CriticConfig,
    progress: Optional[Callable[[int, int], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> list[Candidate]:
    """Validate each candidate with a second LLM pass (parallel batches)."""
    if not candidates:
        return candidates

    safe_terms = load_safe_terms(config.safe_terms_path)
    safe_csv = ", ".join(sorted(safe_terms))

    system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
    system_prompt = system_prompt.replace("{{safe_terms}}", safe_csv)

    user_tmpl = Template(config.user_template_path.read_text(encoding="utf-8"))

    # Auto-reject any candidate whose value is in safe_terms
    for c in candidates:
        if c.value in safe_terms:
            c.critic_is_real_leak = "no"
            c.critic_category_correct = "yes"
            c.critic_placeholder_safe = "yes"
            c.critic_confidence = 1.0
            c.critic_note = "value is in safe_terms whitelist"

    pending = [c for c in candidates if not c.critic_is_real_leak]
    bs = max(1, config.batch_size)
    batches = [pending[i : i + bs] for i in range(0, len(pending), bs)]
    parallel = max(1, int(config.parallel))

    done_batches = 0
    total_batches = len(batches)
    for super_start in range(0, len(batches), parallel):
        if stop_event is not None and stop_event.is_set():
            break
        super_batch = batches[super_start : super_start + parallel]
        jobs: list[ChatJob] = []
        for idx, batch in enumerate(super_batch):
            payload = [
                {
                    "value": c.value,
                    "category": c.category,
                    "suggested_placeholder": c.suggested_placeholder,
                    "rationale": c.rationale,
                }
                for c in batch
            ]
            user_text = user_tmpl.render(
                candidates_json=json.dumps(payload, ensure_ascii=False, indent=2),
                context="",
            )
            jobs.append(
                ChatJob(
                    system=system_prompt,
                    user=user_text,
                    seed=config.base_seed + super_start + idx,
                    max_tokens=config.max_tokens,
                    json_mode=True,
                    tag=(super_start + idx, batch),
                )
            )
        results = llm.chat_many(jobs, max_workers=parallel, stop_event=stop_event)
        for tag, parsed, _raw in results:
            _bidx, batch = tag  # type: ignore[misc]
            verdicts: dict[str, dict] = {}
            if parsed:
                arr = parsed.get("verdict") or []
                if isinstance(arr, list):
                    for v in arr:
                        if not isinstance(v, dict):
                            continue
                        n = _normalize_verdict(v)
                        verdicts[n["value"]] = n
            for c in batch:
                v = verdicts.get(c.value)
                if not v:
                    c.critic_is_real_leak = "uncertain"
                    c.critic_category_correct = "yes"
                    c.critic_placeholder_safe = "yes"
                    c.critic_confidence = 0.0
                    c.critic_note = "critic LLM parse failure"
                    continue
                c.critic_is_real_leak = v["is_real_leak"]
                c.critic_category_correct = v["category_correct"]
                c.critic_placeholder_safe = v["placeholder_safe"]
                c.critic_confidence = v["critic_confidence"]
                c.critic_note = v["note"]
            done_batches += 1
            if progress:
                progress(done_batches, total_batches)
    return candidates


__all__ = ["CriticConfig", "run_critic"]
