"""LLM-driven final audit on the anonymized output.

The deterministic verifier (regex + map regression check) only
catches occurrences whose surface form matches an existing map entry.
The LLM auditor closes the remaining gap: typos, concatenations and
creative variants ("CustomerWave" → "CustomerWaveServer",
"customrwave", "Pre-CustomerWave-Suffix") that no static regex can
enumerate.

The auditor is **grounded in the map**: it never invents a brand on
its own. Every candidate it returns must be derivable from a value
already present in ``substitution_map.yml``, same enforcement as
``derive_placeholder_for_hit``. Output candidates feed straight into
the existing auto-resolve loop, so the pipeline stays single-source-
of-truth and switching customer needs no code change.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from jinja2 import Template

from .candidates import Candidate
from .chunker import chunk_text
from .llm_client import ChatJob, LLMClient
from .placeholders import clamp_to_value_length, is_overlong_placeholder


@dataclass
class AuditConfig:
    system_prompt_path: Path
    user_template_path: Path
    parallel: int = 4
    max_chars_per_chunk: int = 8000


def _strip_json_envelope(text: str) -> Optional[dict]:
    """Best-effort recovery of a JSON object from an LLM reply."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _map_value_lookup(smap_entries: dict) -> dict[str, tuple[str, dict]]:
    """``lower(from) -> (category, entry)`` for fast grounding checks."""
    out: dict[str, tuple[str, dict]] = {}
    for cat, items in smap_entries.items():
        for it in items or []:
            f = str(it.get("from", "") or "").strip()
            if not f:
                continue
            out.setdefault(f.lower(), (cat, it))
    return out


def _ground_candidate(
    raw: dict,
    *,
    chunk_text_str: str,
    map_lookup: dict[str, tuple[str, dict]],
) -> Optional[Candidate]:
    """Validate one LLM-proposed candidate.

    Rejection rules (return ``None``):
    * value missing or not present in the chunk;
    * placeholder missing, identical to value, or empty;
    * placeholder still contains a known-leak token (the rewrite
      itself would be a leak, the LLM made up an unsafe rewrite);
    * the value cannot be grounded in an existing map entry: either
      its lowercase form matches a known ``from`` directly, or it
      contains one of the known ``from`` lower-cased values as a
      substring (sub-token / concatenation / typo case).

    The shared length-clamp is applied so the audit cannot bring back
    the overflow regression.
    """
    value = str(raw.get("value", "") or "").strip()
    placeholder = str(raw.get("suggested_placeholder", "") or "").strip()
    category = str(raw.get("category", "") or "other").strip().lower() or "other"
    if not value or not placeholder or value == placeholder:
        return None
    if value not in chunk_text_str:
        return None

    vlow = value.lower()
    plow = placeholder.lower()
    grounded_entry: tuple[str, dict] | None = None
    if vlow in map_lookup:
        grounded_entry = map_lookup[vlow]
    else:
        for known_lower, (cat, entry) in map_lookup.items():
            if known_lower and known_lower in vlow:
                grounded_entry = (cat, entry)
                break
    if grounded_entry is None:
        return None

    for known_lower in map_lookup.keys():
        if known_lower and known_lower in plow:
            return None

    if is_overlong_placeholder(value, placeholder):
        placeholder = clamp_to_value_length(value, placeholder)
        if not placeholder or placeholder == value:
            return None

    cat = grounded_entry[0]
    rationale = str(raw.get("rationale", "") or "").strip()[:80] or (
        f"audit: derived from map entry "
        f"{grounded_entry[1].get('id', '')}"
    )
    try:
        confidence = float(raw.get("confidence", 1.0))
    except Exception:
        confidence = 1.0
    return Candidate(
        value=value,
        category=cat,
        suggested_placeholder=placeholder,
        confidence=max(0.0, min(1.0, confidence)),
        rationale=rationale,
        count=1,
        examples=[],
        tier="T3_llm_audit",
    )


def run_audit(
    text: str,
    *,
    smap_entries: dict,
    llm: LLMClient,
    config: AuditConfig,
    file_rel: str = "",
    seg_id: str = "doc",
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event=None,
) -> list[Candidate]:
    """Run the LLM auditor on ``text`` and return grounded candidates."""
    chunks = list(
        chunk_text(
            seg_id=seg_id,
            text=text,
            file_rel=file_rel or seg_id,
            max_chars=config.max_chars_per_chunk,
        )
    )
    if not chunks:
        return []
    map_lookup = _map_value_lookup(smap_entries)
    if not map_lookup:
        # Empty map = no grounding = audit would only hallucinate.
        return []

    flat_entries: list[dict] = []
    for cat, items in smap_entries.items():
        for it in items or []:
            flat_entries.append({
                "from": str(it.get("from", "") or ""),
                "to": str(it.get("to", "") or ""),
                "category": cat,
            })

    system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
    user_tmpl = Template(config.user_template_path.read_text(encoding="utf-8"))

    jobs: list[ChatJob] = []
    chunk_texts: list[str] = []
    for i, ch in enumerate(chunks):
        if stop_event is not None and stop_event.is_set():
            return []
        user = user_tmpl.render(
            file_rel=file_rel,
            seg_id=ch.seg_id or f"chunk{i}",
            map_entries=flat_entries,
            chunk_text=ch.text,
        )
        jobs.append(
            ChatJob(
                system=system_prompt,
                user=user,
                json_mode=True,
                max_tokens=2048,
                tag=i,
            )
        )
        chunk_texts.append(ch.text)

    if not jobs:
        return []
    if progress:
        progress(0, len(jobs), "audit")

    results = llm.chat_many(
        jobs,
        max_workers=max(1, config.parallel),
        stop_event=stop_event,
    )
    if progress:
        progress(len(jobs), len(jobs), "audit")

    out: list[Candidate] = []
    seen_values: set[str] = set()
    for tag, parsed, raw_text in results:
        if stop_event is not None and stop_event.is_set():
            break
        envelope = parsed if isinstance(parsed, dict) else _strip_json_envelope(raw_text or "")
        if not envelope:
            continue
        try:
            chunk_str = chunk_texts[int(tag)]
        except Exception:
            chunk_str = ""
        for raw in envelope.get("candidates", []) or []:
            if not isinstance(raw, dict):
                continue
            cand = _ground_candidate(
                raw, chunk_text_str=chunk_str, map_lookup=map_lookup
            )
            if cand is None:
                continue
            if cand.value in seen_values:
                continue
            seen_values.add(cand.value)
            out.append(cand)

    return out


__all__ = ["AuditConfig", "run_audit"]
