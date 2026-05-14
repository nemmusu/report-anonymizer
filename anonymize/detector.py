"""Tier 1a: LLM-driven leak detection.

For each text-like file in the scan we extract segments via the adapter,
chunk them, run the detector LLM in JSON mode (in **parallel batches** of
``max_workers`` chunks at a time so we drive ``llama-server --parallel`` slots
concurrently), and aggregate candidates across the dossier.

Already-mapped values, Tier-0 candidates and ``safe_terms`` are filtered out
before going to the LLM.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml
from jinja2 import Template

from .candidates import Candidate, CandidateOccurrence
from .chunker import Chunk, HugeTextStrategy, chunk_segments
from .decisions_log import DecisionsLog
from .llm_client import ChatJob, LLMClient
from .scanner import ScanResult


def load_safe_terms(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(t) for t in (data.get("safe") or [])}


@dataclass
class DetectorConfig:
    system_prompt_path: Path
    user_template_path: Path
    safe_terms_path: Path
    # Default lowered from 10000 → 5000 because the structured chunker
    # never splits inside a table / code fence / heading group, so
    # smaller chunks are now safe and produce noticeably more accurate
    # detections.
    max_chunk_chars: int = 5000
    chunk_overlap: int = 200
    max_tokens: int = 2048
    base_seed: int = 1
    parallel: int = 4  # max concurrent LLM requests
    # ``"structured"`` (default) splits at Markdown structural
    # boundaries (heading / table / code fence / list / paragraph)
    # and never breaks inside one. ``"flat"`` is the legacy
    # character-count splitter, kept for repro / regression tests.
    chunk_strategy: str = "structured"


def _render_user_prompt(
    template: Template,
    *,
    chunk: Chunk,
    safe_terms_csv: str,
    few_shot: list[dict],
    category_hint: str = "",
) -> str:
    return template.render(
        file_rel=chunk.file_rel,
        seg_id=chunk.seg_id,
        section=getattr(chunk, "section", ""),
        chunk_text=chunk.text,
        safe_terms=safe_terms_csv,
        few_shot=few_shot,
        category_hint=category_hint,
    )


def _normalize_candidate(d: dict) -> Optional[dict]:
    value = str(d.get("value") or "").strip()
    if not value:
        return None
    cat = str(d.get("category") or "other").strip().lower()
    placeholder = str(d.get("suggested_placeholder") or "").strip()
    try:
        conf = float(d.get("confidence") or 0)
    except (ValueError, TypeError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    rationale = str(d.get("rationale") or "")[:120]
    return {
        "value": value,
        "category": cat,
        "suggested_placeholder": placeholder,
        "confidence": conf,
        "rationale": rationale,
    }


def run_detector(
    scan: ScanResult,
    *,
    llm: LLMClient,
    config: DetectorConfig,
    decisions: DecisionsLog,
    existing_map_keys: Optional[set[str]] = None,
    tier0_values: Optional[set[str]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[list[Candidate], list[CandidateOccurrence]]:
    """Run the LLM detector on all text-like files in ``scan``.

    Chunks are dispatched in batches of ``config.parallel`` to llama-server
    via :meth:`LLMClient.chat_many`. Output preserves chunk order so the
    ``seen`` accumulator stays deterministic.
    """
    existing_map_keys = existing_map_keys or set()
    tier0_values = tier0_values or set()
    safe_terms = load_safe_terms(config.safe_terms_path)
    safe_csv = ", ".join(sorted(safe_terms))

    system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
    system_prompt = system_prompt.replace("{{safe_terms}}", safe_csv)

    user_tmpl = Template(config.user_template_path.read_text(encoding="utf-8"))
    few_shot = [
        {
            "value": d.get("value", ""),
            "placeholder": d.get("placeholder", ""),
            "category": d.get("category", ""),
        }
        for d in decisions.few_shot_examples(8)
        if d.get("action") == "promote"
    ]

    seen: dict[str, Candidate] = {}
    occurrences: list[CandidateOccurrence] = []

    text_files = [sf for sf in scan.text_like if not sf.skipped]
    chunks_per_file: list[tuple[object, list[Chunk]]] = []
    total_chunks = 0
    # Emit a progress tick before each extract() call so the UI bar
    # surfaces "which file is being processed" instead of sitting at
    # "scan: detector 0%" for the entire duration of the file-text
    # extraction phase. Without this the user sees an apparent
    # several-seconds freeze every time a PDF or DOCX is parsed,
    # which is misleading: the work is real, just invisible.
    n_files = len(text_files)
    for i, sf in enumerate(text_files):
        if stop_event is not None and stop_event.is_set():
            break
        if progress:
            rel_name = getattr(sf, "rel", None) or getattr(sf, "path", "?")
            try:
                rel_name = str(rel_name).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            except Exception:
                rel_name = "?"
            try:
                progress(i, max(1, n_files), f"extracting {rel_name}")
            except Exception:
                pass
        try:
            segments = sf.adapter.extract(sf.path)
        except Exception:
            continue
        if (config.chunk_strategy or "structured").lower() == "structured":
            from .structure_chunker import chunk_segments_structured

            chunks = list(
                chunk_segments_structured(
                    segments,
                    file_rel=str(sf.rel),
                    max_chars=config.max_chunk_chars,
                    overlap=config.chunk_overlap,
                )
            )
        else:
            chunks = list(
                chunk_segments(
                    segments,
                    file_rel=str(sf.rel),
                    max_chars=config.max_chunk_chars,
                    overlap=config.chunk_overlap,
                )
            )
        chunks_per_file.append((sf, chunks))
        total_chunks += len(chunks)

    flat: list[tuple[object, Chunk]] = []
    for sf, chunks in chunks_per_file:
        for c in chunks:
            flat.append((sf, c))

    parallel = max(1, int(config.parallel))
    done = 0
    for batch_start in range(0, len(flat), parallel):
        if stop_event is not None and stop_event.is_set():
            break
        batch = flat[batch_start : batch_start + parallel]
        jobs: list[ChatJob] = []
        for idx, (sf, chunk) in enumerate(batch):
            user_text = _render_user_prompt(
                user_tmpl,
                chunk=chunk,
                safe_terms_csv=safe_csv,
                few_shot=few_shot,
            )
            jobs.append(
                ChatJob(
                    system=system_prompt,
                    user=user_text,
                    seed=config.base_seed + batch_start + idx,
                    max_tokens=config.max_tokens,
                    json_mode=True,
                    tag=(sf, chunk),
                )
            )
        results = llm.chat_many(jobs, max_workers=parallel, stop_event=stop_event)
        for tag, parsed, _raw in results:
            done += 1
            sf, chunk = tag  # type: ignore[misc]
            if progress:
                progress(done, total_chunks, str(sf.rel))
            if not parsed:
                continue
            cands = parsed.get("candidates") or []
            if not isinstance(cands, list):
                continue
            for raw_c in cands:
                if not isinstance(raw_c, dict):
                    continue
                norm = _normalize_candidate(raw_c)
                if not norm:
                    continue
                value = norm["value"]
                if value in safe_terms or value in existing_map_keys or value in tier0_values:
                    continue
                occurrences.append(
                    CandidateOccurrence(
                        value=value,
                        file_rel=str(sf.rel),
                        seg_id=chunk.seg_id,
                        offset=chunk.start,
                    )
                )
                if value in seen:
                    cand = seen[value]
                    cand.count += 1
                    ex = f"{sf.rel}:{chunk.seg_id}"
                    if len(cand.examples) < 5 and ex not in cand.examples:
                        cand.examples.append(ex)
                    if norm["confidence"] > cand.confidence:
                        cand.confidence = norm["confidence"]
                    continue
                seen[value] = Candidate(
                    value=value,
                    category=norm["category"],
                    suggested_placeholder=norm["suggested_placeholder"],
                    confidence=norm["confidence"],
                    rationale=norm["rationale"],
                    count=1,
                    examples=[f"{sf.rel}:{chunk.seg_id}"],
                    tier="T1_llm",
                )
    return list(seen.values()), occurrences


__all__ = ["DetectorConfig", "run_detector", "load_safe_terms"]
