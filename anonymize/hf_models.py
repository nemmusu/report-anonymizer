"""Hugging Face model search + download backend.

Two responsibilities:

* curated catalog + free-text search over GGUF repos (uses ``huggingface_hub``)
* streaming download with **resume**, **progress callback**, and
  **cooperative cancel** via a ``threading.Event``.

Downloads land in ``~/.local/share/document-anonymizer/models/``. Requires
``huggingface_hub`` and ``requests``; both are pinned in ``requirements.txt``.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from ._paths import user_config_dir as _user_config_dir
from .server_profile import MODELS_DIR


HF_BASE = "https://huggingface.co"
HF_TOKEN_PATH = _user_config_dir() / "hf.token"


def ensure_models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def _safe_repo_dirname(repo_id: str) -> str:
    """Filesystem-safe directory name derived from an HF repo id.

    ``unsloth/Qwen3.6-27B-GGUF`` -> ``unsloth__Qwen3.6-27B-GGUF``.
    The double underscore is a clear separator that never appears in HF
    repo ids, so the round-trip stays unambiguous.
    """
    return (repo_id or "").replace("/", "__").strip() or "unknown_repo"


def repo_models_dir(repo_id: str) -> Path:
    """Per-repo download directory under :data:`MODELS_DIR`.

    All downloads go into a per-repo subdirectory so files that share
    a generic name across repos (notably ``mmproj-BF16.gguf``) never
    overwrite each other.  The flat top level :data:`MODELS_DIR` is
    still scanned for legacy installs by :func:`local_models`.
    """
    return MODELS_DIR / _safe_repo_dirname(repo_id)


def expected_path_for(repo_id: str, filename: str) -> Path:
    """Where a given ``(repo, filename)`` pair gets stored on disk.

    New downloads always land in the per-repo subdirectory; for
    backward compatibility we still consider any pre-existing copy in
    the flat :data:`MODELS_DIR` as "the same file" so existing presets
    and on-disk libraries keep working without a forced re-download.
    """
    flat = MODELS_DIR / filename
    nested = repo_models_dir(repo_id) / filename
    if flat.exists() and not nested.exists():
        return flat
    return nested


def load_hf_token() -> Optional[str]:
    """Read HF token from disk (if present), else return None.

    The file should be ``chmod 0600``. We do not raise if perms are loose, but
    we'll warn from the GUI.
    """
    if not HF_TOKEN_PATH.exists():
        return None
    try:
        token = HF_TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except Exception:
        return None


def save_hf_token(token: str) -> None:
    HF_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HF_TOKEN_PATH.write_text((token or "").strip() + "\n", encoding="utf-8")
    try:
        os.chmod(HF_TOKEN_PATH, 0o600)
    except Exception:
        pass


# ---- Curated catalog -------------------------------------------------------


@dataclass
class CuratedRepo:
    repo_id: str
    display_name: str
    family: str
    description: str
    recommended_files: list[str] = field(default_factory=list)
    # Benchmarked metrics, populated from
    # ``bench/run_precision_benchmark.py``. The Model-Manager UI
    # surfaces these so users see what to expect (VRAM cost / quality)
    # *before* downloading.  ``None`` means "not benchmarked".
    benchmark_f1: Optional[float] = None              # 0.0–1.0
    benchmark_recall: Optional[float] = None          # 0.0–1.0
    benchmark_peak_vram_mb: Optional[int] = None      # peak GPU MB
    benchmark_total_seconds: Optional[float] = None   # 5-PDF corpus
    benchmark_notes: str = ""                         # extra one-liner
    # Compatibility classification, drives the warning style in the
    # Model-Manager download badge. ``"ok"`` for working presets,
    # ``"low_quality"`` for models that respond but with a poor
    # catch / false-alarm balance, ``"incompatible"`` for models
    # that fail outright (e.g. Sliding-Window Attention shorter than
    # our system prompt, safety-tuned models that refuse
    # arbitrary-JSON tasks).  Pairs with ``compatibility_reason``,
    # a one-liner shown next to the warning.
    compatibility_status: str = "ok"
    compatibility_reason: str = ""
    # Lowercased substrings that identify the *underlying* model
    # regardless of who republishes the GGUF.  Used by
    # ``repo_metadata`` to propagate ⚠️/❌ warnings to community
    # mirrors (e.g. ``bartowski/gemma-4-E4B-it-GGUF`` still picks up
    # the SWA-1024 incompatibility note from the canonical Gemma 4
    # entry).  Match is case-insensitive substring against the full
    # ``owner/repo`` id, so patterns should be specific enough to
    # avoid false positives (``gemma-4-e4b`` is fine, ``qwen`` is
    # not).
    name_patterns: list[str] = field(default_factory=list)
    # Optional plain-language label for the *primary* benchmark
    # numbers (the ones in ``benchmark_*`` fields). Surfaces in the
    # GUI badge as the heading above the primary score row so the
    # user knows which variant the headline numbers belong to.
    # Leave empty for single-build entries.
    primary_label: str = ""
    # Per-variant prose blurb for the primary build (BF16, F16,
    # whatever the headline is).  Renders directly under the
    # primary score row so each variant's score is visually
    # grouped with its own description.  Mirror of the ``summary``
    # field on each ``alt_benchmarks`` entry.
    primary_summary: str = ""
    # Per-variant benchmark blocks for repos that ship more than
    # one recommended build (canonical example: Ministral 3 8B
    # Reasoning, BF16 + Q5_K_M).  Each entry mirrors the
    # ``benchmark_*`` schema and gets its own heading, score row
    # and summary in the GUI badge.
    #
    # Schema per item::
    #
    #     {
    #         "label": str,      # plain-language variant name
    #         "filename": str,   # GGUF this row describes
    #         "f1": float,       # 0.0-1.0
    #         "recall": float,   # 0.0-1.0
    #         "vram_mb": int,    # peak GPU memory
    #         "seconds": float,  # wall-clock on the 5-PDF test
    #         "summary": str,    # blurb shown under the score row
    #     }
    alt_benchmarks: list[dict] = field(default_factory=list)


# Curation policy: top 5 by detection quality on our anonymization
# corpus, regardless of weight format (see ``BENCHMARKS.md``).
# Quants are welcome when they earn the slot on F1 (the Q5_K_M
# build of Ministral 3 8B Reasoning is the canonical example: it
# beats every BF16 model below the top 2 at half the VRAM cost).
# Models we tested and dropped live in ``KNOWN_PROBLEMATIC_REPOS``
# below; the Model Manager surfaces them with a ⚠️/❌ warning
# *only* if a user lands on their HF repo id through the free-text
# Search tab, so the curated dropdown stays clean at 5 entries.
CURATED_REPOS: list[CuratedRepo] = [
    CuratedRepo(
        repo_id='unsloth/Ministral-3-8B-Reasoning-2512-GGUF',
        display_name='🥇 Ministral 3 · 8B Reasoning',
        family='Mistral',
        description=(
            'Highest detection quality in our benchmarks. Two recommended downloads, same model: a full-precision build and a lower-memory build.'
        ),
        recommended_files=['Ministral-3-8B-Reasoning-2512-BF16.gguf', 'Ministral-3-8B-Reasoning-2512-Q5_K_M.gguf'],
        benchmark_f1=0.825,
        benchmark_recall=0.909,
        benchmark_peak_vram_mb=18940,
        benchmark_total_seconds=244.4,
        benchmark_notes='best overall quality',
        name_patterns=['ministral-3-8b-reasoning-2512'],
        primary_label='full-precision build',
        primary_summary='Catches almost every leak (about 9 of every 10) and raises few false alarms. The strongest version of the model; pick this when you have the GPU memory.',
        alt_benchmarks=[{'label': 'lower-memory build', 'filename': 'Ministral-3-8B-Reasoning-2512-Q5_K_M.gguf', 'f1': 0.762, 'recall': 0.909, 'vram_mb': 9171, 'seconds': 112.0, 'summary': 'Same model in a smaller footprint. Catches the same fraction of leaks as the full-precision build but raises a few more false alarms. Fits a 12 GB GPU.'}],
    ),
    CuratedRepo(
        repo_id='rtila-corporation/rtila-assistant-lite-1.5',
        display_name='🥈 rtila Assistant Lite · 9B (Qwen 3.5)',
        family='Qwen',
        description=(
            'Quantised Qwen 3.5 9B fine-tune (Q4_K_M). Quality essentially tied with the leader at a fraction of the VRAM: ~7 GB peak vs ~18 GB for the leader. Catches about 9 of every 10 leaks. Best quality-per-VRAM in the catalog, pick this on a 12 GB GPU.'
        ),
        recommended_files=['qwen3.5-9b-Q4_K_M.gguf'],
        benchmark_f1=0.82,
        benchmark_recall=0.909,
        benchmark_peak_vram_mb=7135,
        benchmark_total_seconds=79.0,
        benchmark_notes='near-leader quality at 1/3 the VRAM',
        name_patterns=['rtila-assistant-lite'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3.5-4B-Claude-4.6-Opus-Reasoning-Distilled-GGUF',
        display_name='🥉 Qwen 3.5 · 4B Claude-Opus distill',
        family='Qwen',
        description=(
            "4B Claude-4.6-Opus reasoning distill (Q4_K_M, only ~2.5 GB on disk). Ranks above several 8-9B models on our corpus and fits a 6 GB GPU. Best 'small + good' pick, beats the legacy 4B BF16 default by 14 points on Quality at one-third the size."
        ),
        recommended_files=['Qwen3.5-4B.Q4_K_M.gguf'],
        benchmark_f1=0.78,
        benchmark_recall=0.773,
        benchmark_peak_vram_mb=4820,
        benchmark_total_seconds=185.0,
        benchmark_notes='best small-model pick (~2.5 GB disk)',
        name_patterns=['qwen3.5-4b-claude-4.6-opus-reasoning-distilled'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Qwen3.5-9B-GGUF',
        display_name='⭐ Qwen 3.5 · 9B',
        family='Qwen',
        description=(
            "BF16 9B with the fewest false alarms in our benchmarks. Pick when you'd rather miss one leak than chase a false alert. Catches about 7-8 of every 10 leaks. Needs ~17.6 GB GPU memory and ~3.5 minutes on the 5-PDF test."
        ),
        recommended_files=['Qwen3.5-9B-BF16.gguf'],
        benchmark_f1=0.777,
        benchmark_recall=0.75,
        benchmark_peak_vram_mb=18024,
        benchmark_total_seconds=210.0,
        benchmark_notes='fewest false alarms',
        name_patterns=['qwen3.5-9b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-GGUF',
        display_name='⭐ Qwen 3.5 · 9B Claude-Opus distill',
        family='Qwen',
        description=(
            '9B Claude-4.6-Opus reasoning distill (Q4_K_M, ~5.2 GB). Quality matches the Ministral 8B Reasoning Q5_K_M build but uses ~7 GB peak VRAM instead of ~9 GB, alternative to rtila Assistant Lite if you want the unmodified Jackrong distill chain.'
        ),
        recommended_files=['Qwen3.5-9B.Q4_K_M.gguf'],
        benchmark_f1=0.76,
        benchmark_recall=0.773,
        benchmark_peak_vram_mb=7081,
        benchmark_total_seconds=207.0,
        benchmark_notes='Q5-Reasoning-class quality at smaller VRAM',
        name_patterns=['qwen3.5-9b-claude-4.6-opus-reasoning-distilled'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Qwen3.5-4B-GGUF',
        display_name='Qwen 3.5 · 4B  ★ recommended default',
        family='Qwen',
        description=(
            'Recommended default. Lightest setup that runs on any machine: the Q5_K_M quant on CPU. Smallest download (~3 GB), no GPU required. The BF16 build is also pinned for users on GPU. Quality 64 / 100, for higher quality at the same size, see the 4B Claude-Opus distill.'
        ),
        recommended_files=['Qwen3.5-4B-Q5_K_M.gguf', 'Qwen3.5-4B-BF16.gguf'],
        benchmark_f1=0.638,
        benchmark_recall=0.682,
        benchmark_peak_vram_mb=10282,
        benchmark_total_seconds=88.2,
        benchmark_notes='recommended default, runs on any machine',
        name_patterns=['qwen3.5-4b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/granite-4.1-8b-GGUF',
        display_name='Granite 4.1 · 8B  (inconsistent but usable)',
        family='Granite',
        description=(
            'IBM Granite 4.1 8B (BF16, ~16 GB). Quality 63 / 100 on average, but the per-PDF score swings a lot: very good on some documents, near zero on others. Recall 63.6 %, precision 62.2 %. Listed as an alternative; pick the curated leaders when consistency matters.'
        ),
        recommended_files=[],
        benchmark_f1=0.629,
        benchmark_recall=0.636,
        benchmark_peak_vram_mb=19908,
        benchmark_total_seconds=100.0,
        benchmark_notes='inconsistent across documents',
        name_patterns=['granite-4.1-8b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mistralai/Ministral-3-8B-Instruct-2512-GGUF',
        display_name='Ministral 3 · 8B Instruct  (high-recall alternative)',
        family='Mistral',
        description=(
            "Instruct version of the Ministral 3 8B family (BF16, ~16 GB). Quality 65 / 100: precision 51.4 %, recall 86.4 % (the highest of the 8B bracket outside the curated Reasoning leader). Pick when you want maximum coverage and don't mind reviewing more candidates."
        ),
        recommended_files=['Ministral-3-8B-Instruct-2512-BF16.gguf'],
        benchmark_f1=0.645,
        benchmark_recall=0.864,
        benchmark_peak_vram_mb=18768,
        benchmark_total_seconds=213.8,
        benchmark_notes='catches most leaks, faster than the reasoning version',
        name_patterns=['ministral-3-8b-instruct-2512'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='s3dev-ai/Ministral-3-14B-Reasoning-2512-gguf',
        display_name='Ministral 3 · 14B Reasoning  (usable above the 8B cousin)',
        family='Mistral',
        description=(
            '14B Reasoning variant (Q4_K_M, ~7.7 GB). Quality 67 / 100: precision 57 %, recall 82 %. Higher VRAM than the curated 8B Reasoning leader (~11 GB) for a 16-point quality drop, but stays in the Usable band on every PDF of the corpus. Pick when you want a single 14B reasoning model and have the GPU headroom.'
        ),
        recommended_files=['Ministral-3-14B-Reasoning-2512-Q4_K_M.gguf'],
        benchmark_f1=0.67,
        benchmark_recall=0.82,
        benchmark_peak_vram_mb=10965,
        benchmark_total_seconds=174.0,
        benchmark_notes='usable but outperformed by 8B Reasoning',
        name_patterns=['ministral-3-14b-reasoning'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/OpenSonnet-Lite-GGUF',
        display_name='OpenSonnet Lite  (mid-quality 8B alt)',
        family='Other',
        description=(
            'Mid-quality general model. Quality 57 / 100 in Q8_0 (~4 GB), drops to 42 / 100 in Q4_K_M. Listed for completeness; the curated 4B / 9B distills outperform it at every size tier but the Q8 build still lands in the Usable band.'
        ),
        recommended_files=['OpenSonnet-Lite.Q8_0.gguf', 'OpenSonnet-Lite.Q4_K_M.gguf'],
        benchmark_f1=0.57,
        benchmark_recall=0.59,
        benchmark_peak_vram_mb=7416,
        benchmark_total_seconds=393.0,
        benchmark_notes='Q8_0 row; Q4_K_M variant scores 42 / 100',
        name_patterns=['opensonnet-lite'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='bartowski/Mistral-Nemo-Instruct-2407-GGUF',
        display_name='Mistral Nemo · 12B Instruct  (mid-quality alt)',
        family='Mistral',
        description=(
            'Mistral Nemo 12B Instruct (Q4_K_M, ~7 GB). Quality 56 / 100: precision 48 %, recall 66 %. Usable when you need a 12B size point between the curated 8B and the 14B options but the recall is below the curated leaders.'
        ),
        recommended_files=['Mistral-Nemo-Instruct-2407-Q4_K_M.gguf'],
        benchmark_f1=0.56,
        benchmark_recall=0.66,
        benchmark_peak_vram_mb=10239,
        benchmark_total_seconds=146.0,
        benchmark_notes='below cut; outperformed by Ministral 8B Reasoning',
        name_patterns=['mistral-nemo-instruct-2407'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Qwen3.5-9B-DeepSeek-V4-Flash-GGUF',
        display_name='Qwen 3.5 · 9B DeepSeek V4 Flash  (slow but usable)',
        family='Qwen',
        description=(
            'Qwen 3.5 9B DeepSeek V4 Flash distill (Q4_K_M, ~5.2 GB). Quality 55 / 100, precision 82 %, recall 41 %. The highest-precision 9B model on the corpus but ~10 min on the 5-PDF test; pick when false alarms are the main concern and you can spend wall time.'
        ),
        recommended_files=['Qwen3.5-9B-DeepSeek-V4-Flash.Q4_K_M.gguf'],
        benchmark_f1=0.55,
        benchmark_recall=0.41,
        benchmark_peak_vram_mb=7088,
        benchmark_total_seconds=624.0,
        benchmark_notes='surgical but slow',
        name_patterns=['qwen3.5-9b-deepseek-v4-flash'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='bartowski/Deepthink-Reasoning-7B-GGUF',
        display_name='Deepthink Reasoning · 7B  (mid-tier reasoning)',
        family='Other',
        description=(
            '7B reasoning distill (Q4_K_M, ~4.4 GB). Quality 53 / 100: precision 54 %, recall 52 %. Balanced but lower than the curated 8B Reasoning leaders. Lands in the Usable band on most documents.'
        ),
        recommended_files=['Deepthink-Reasoning-7B-Q4_K_M.gguf'],
        benchmark_f1=0.53,
        benchmark_recall=0.52,
        benchmark_peak_vram_mb=6449,
        benchmark_total_seconds=240.0,
        benchmark_notes='no edge over Ministral 8B Reasoning Q5_K_M',
        name_patterns=['deepthink-reasoning-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3.5-0.8B-Claude-4.6-Opus-Reasoning-Distilled-GGUF',
        display_name='Qwen 3.5 · 0.8B Claude-Opus distill  (sub-1 GB pick)',
        family='Qwen',
        description=(
            'Tiny 0.8B Claude-4.6-Opus distill (Q8_0, ~0.8 GB). Quality 56 / 100, the best of the sub-1 GB tier (precision 67.7 %, recall 47.7 %). Pick when disk and VRAM are scarce and the task fits the regex + Tier-1 baseline. Peak VRAM ~2.9 GB.'
        ),
        recommended_files=['Qwen3.5-0.8B.Q8_0.gguf'],
        benchmark_f1=0.56,
        benchmark_recall=0.477,
        benchmark_peak_vram_mb=2903,
        benchmark_total_seconds=164.0,
        benchmark_notes='best sub-1 GB pick; below curated cut',
        name_patterns=['qwen3.5-0.8b-claude'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Thatoneguy69/Opus4.7-GODs.Ghost.Codex-4B.GGuF',
        display_name='Opus 4.7 · 4B GODs Ghost Codex distill',
        family='Other',
        description=(
            'Claude-4.7 reasoning distill at 4B (Q4_K_M, ~2.5 GB). Quality 72 / 100: precision 78.4 %, recall 65.9 %. The best of the 4B class outside the curated top-5, just 4 quality points behind the recommended distill on a fraction of the VRAM. Peak VRAM ~4.8 GB.'
        ),
        recommended_files=['Opus4.7-Distill-GODsGhost-Codex-4B-Q4_K_M.gguf'],
        benchmark_f1=0.716,
        benchmark_recall=0.659,
        benchmark_peak_vram_mb=4798,
        benchmark_total_seconds=155.0,
        benchmark_notes='second-best 4B in the Usable band',
        name_patterns=['opus4.7-distill-godsghost', 'opus4-7-distill-godsghost'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwopus3.5-4B-v3-GGUF',
        display_name='Qwopus 3.5 · 4B v3 distill',
        family='Qwen',
        description=(
            'Jackrong Qwopus 3.5 v3 (Q4_K_M, ~2.5 GB). Quality 71 / 100: precision 76.3 %, recall 65.9 %. Same 4B Qwen 3.5 base as the recommended distill but a newer Opus-reasoning training pass; lands one quality point below the v1 in our corpus.'
        ),
        recommended_files=['Qwen3.5-4B.Q4_K_M.gguf'],
        benchmark_f1=0.707,
        benchmark_recall=0.659,
        benchmark_peak_vram_mb=4765,
        benchmark_total_seconds=456.0,
        benchmark_notes='newer Opus distill of Qwen 3.5 4B',
        name_patterns=['qwopus3.5-4b-v3', 'qwopus3-5-4b-v3'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Opus4.7-GODs.Ghost.Codex-4B.GGuF',
        display_name='Opus 4.7 · 4B GODs Ghost Codex (WithinUsAI mirror)',
        family='Other',
        description=(
            'WithinUsAI re-publication of the Opus 4.7 GODs Ghost Codex 4B distill (Q4_K_M, ~2.5 GB). Quality 69 / 100 on our corpus; close to the Thatoneguy69 mirror at Q 72. Use either one, they share the underlying weights.'
        ),
        recommended_files=['Opus4.7-Distill-GODsGhost-Codex-4B-Q4_K_M.gguf'],
        benchmark_f1=0.69,
        benchmark_recall=0.682,
        benchmark_peak_vram_mb=4785,
        benchmark_total_seconds=155.0,
        benchmark_notes='mirror of Thatoneguy69 GODs Ghost Codex 4B',
        name_patterns=['withinusai-opus4.7-godsghost'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Tesslate/OmniCoder-9B-GGUF',
        display_name='OmniCoder · 9B distill',
        family='Qwen',
        description=(
            'Coder-focused 9B distill (Q4_0, ~4.9 GB). Quality 69 / 100 with the best balance of speed and recall in the 9B Q4 bracket: 113 s on the 5-PDF corpus, precision 69.8 %, recall 68.2 %, peak VRAM 6.7 GB.'
        ),
        recommended_files=['omnicoder-9b-q4_0.gguf'],
        benchmark_f1=0.69,
        benchmark_recall=0.682,
        benchmark_peak_vram_mb=6673,
        benchmark_total_seconds=113.0,
        benchmark_notes='fastest 9B Q4 in the Good band',
        name_patterns=['omnicoder-9b', 'omni-coder-9b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='marcoariette/OmniClaw-Qwen3.5-9B-Claude-4.6-Opus-Uncensored-v2-GGUF',
        display_name='OmniClaw · Qwen 3.5 9B Uncensored Opus v2',
        family='Qwen',
        description=(
            'Uncensored Claude-Opus distill of Qwen 3.5 9B (Q4_K_M, ~5.2 GB). Quality 67 / 100 driven by very high recall (88.6 %, near the curated leaders) at the cost of precision (53.4 %): expect more candidates to triage in Review. Peak VRAM ~7.1 GB.'
        ),
        recommended_files=['OmniClaw-KL-Q4_K_M.gguf'],
        benchmark_f1=0.667,
        benchmark_recall=0.886,
        benchmark_peak_vram_mb=7069,
        benchmark_total_seconds=260.0,
        benchmark_notes='high recall, lower precision',
        name_patterns=['omniclaw-qwen3.5-9b', 'omniclaw-kl'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3.5-4B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF',
        display_name='Qwen 3.5 · 4B Claude-Opus distill v2',
        family='Qwen',
        description=(
            'Second-generation Claude-4.6-Opus distill of Qwen 3.5 4B (Q4_K_M, ~2.5 GB). Quality 67 / 100; the v1 (Q 78) still beats it on our corpus but the v2 keeps the same 4.8 GB VRAM footprint.'
        ),
        recommended_files=['Qwen3.5-4B.Q4_K_M.gguf'],
        benchmark_f1=0.667,
        benchmark_recall=0.614,
        benchmark_peak_vram_mb=4833,
        benchmark_total_seconds=198.0,
        benchmark_notes='v2 trails v1 by 11 quality points',
        name_patterns=['qwen3.5-4b-claude-4.6-opus-reasoning-distilled-v2'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='ertghiu256/qwen3-4b-claude-sonnet-x-gemini-reasoning-gguf',
        display_name='Qwen 3 · 4B Claude-Sonnet x Gemini reasoning',
        family='Qwen',
        description=(
            "Cross-family reasoning distill of Qwen 3 4B (IQ4_NL, ~2.2 GB). Quality 65 / 100, precision 56.9 %, recall 75 %. Balanced pick when you want a 4B with stronger recall than the curated default and don't mind the IQ4_NL quant."
        ),
        recommended_files=['qwen3-4b-claude-sonnet-x-gemini-reasoning-gguf-IQ4_NL.gguf'],
        benchmark_f1=0.647,
        benchmark_recall=0.75,
        benchmark_peak_vram_mb=5584,
        benchmark_total_seconds=241.0,
        benchmark_notes='strong recall 4B IQ4_NL',
        name_patterns=['qwen3-4b-claude-sonnet-x-gemini-reasoning'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='TeichAI/Qwen3-4B-Thinking-MiniMax-M2.1-Coder-GGUF',
        display_name='Qwen 3 · 4B Thinking MiniMax Coder',
        family='Qwen',
        description=(
            'Coder-flavoured MiniMax thinking distill of Qwen 3 4B (Q4_K_M, ~2.3 GB). Quality 64 / 100 driven by the highest recall in this batch (93.2 %); precision sits at 48.2 % so Review will see more candidates. Pick when missing leaks is the bigger risk.'
        ),
        recommended_files=['Qwen3-4B-MiniMax-M2.1-Coder.q4_k_m.gguf'],
        benchmark_f1=0.636,
        benchmark_recall=0.932,
        benchmark_peak_vram_mb=5708,
        benchmark_total_seconds=382.0,
        benchmark_notes='highest recall 4B in the round',
        name_patterns=['qwen3-4b-thinking-minimax-m2.1-coder', 'qwen3-4b-minimax-m2.1-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/OpenThinker2-7B-GGUF',
        display_name='OpenThinker 2 · 7B reasoning',
        family='Other',
        description=(
            'Open-weight reasoning model at 7B (Q4_K_M, ~4.4 GB). Quality 62 / 100, precision 72.7 %, recall 54.5 %, peak VRAM 6.4 GB. Solid balanced pick when the curated 4B distill is too small but a 9B Q4 is too heavy.'
        ),
        recommended_files=['OpenThinker2-7B-Q4_K_M.gguf'],
        benchmark_f1=0.623,
        benchmark_recall=0.545,
        benchmark_peak_vram_mb=6412,
        benchmark_total_seconds=437.0,
        benchmark_notes='balanced 7B reasoning',
        name_patterns=['openthinker2-7b', 'open-thinker2-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Qwen3-Space.Agent.Claude.Uncensored-4B.GGUF',
        display_name='Qwen 3 · 4B Space-Agent Uncensored',
        family='Qwen',
        description=(
            'Agent-themed uncensored Qwen 3 4B distill (Q4_K_M, ~2.5 GB). Quality 62 / 100 with the highest recall of the 4B class (90.9 %, matching the BF16 curated leaders) at the cost of precision (47.6 %). Pick when 6 GB of VRAM has to deliver maximum coverage.'
        ),
        recommended_files=['Qwen3-Space.Agent.Claude-Uncensored-4B.Q4_K_M.gguf'],
        benchmark_f1=0.625,
        benchmark_recall=0.909,
        benchmark_peak_vram_mb=5662,
        benchmark_total_seconds=177.0,
        benchmark_notes='highest-recall 4B Q4 on this corpus',
        name_patterns=['qwen3-space.agent.claude.uncensored', 'qwen3-space-agent-claude-uncensored'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='TeichAI/Qwen3-4B-Thinking-2507-MiniMax-M2.1-Distill-GGUF',
        display_name='Qwen 3 · 4B Thinking MiniMax 2.1 distill',
        family='Qwen',
        description=(
            'MiniMax 2.1 thinking distill of Qwen 3 4B (Q4_K_M, ~2.3 GB). Quality 60 / 100, precision 62.5 %, recall 56.8 %, peak VRAM 5.8 GB. Bottom of the curated set; expect ~6.5 min wall time on a 5-PDF corpus.'
        ),
        recommended_files=['Qwen3-4B-Thinking-2507-MiniMax-M2.1-Distill.q4_k_m.gguf'],
        benchmark_f1=0.595,
        benchmark_recall=0.568,
        benchmark_peak_vram_mb=5783,
        benchmark_total_seconds=396.0,
        benchmark_notes='balanced 4B thinking distill',
        name_patterns=['qwen3-4b-thinking-2507-minimax-m2.1-distill'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='TeichAI/Qwen3-4B-Thinking-2507-Gemini-3-Pro-Preview-High-Reasoning-Distill-GGUF',
        display_name='Qwen 3 · 4B Thinking Gemini 3 Pro distill',
        family='Qwen',
        description=(
            'Gemini 3 Pro reasoning distill of Qwen 3 4B (Q4_K_M, ~2.3 GB). Quality 59 / 100, precision 63.2 %, recall 54.5 %, peak VRAM 5.9 GB. Slightly faster than the MiniMax sibling and similarly balanced.'
        ),
        recommended_files=['Qwen3-4B-Thinking-2507-Gemini-3-Pro-Preview-High-Reasoning-Distill.q4_k_m.gguf'],
        benchmark_f1=0.585,
        benchmark_recall=0.545,
        benchmark_peak_vram_mb=5861,
        benchmark_total_seconds=188.0,
        benchmark_notes='Gemini-distilled 4B thinking',
        name_patterns=['qwen3-4b-thinking-2507-gemini-3-pro'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Qwen3-4B-2507-Geminized-v1-GGUF',
        display_name='Qwen 3 · 4B 2507 Geminized v1',
        family='Qwen',
        description=(
            'Gemini-style training-time geminization of Qwen 3 4B (Q4_K_M, ~2.5 GB). Quality 54 / 100 with the highest precision in the 4B Q4 tier (78.3 %) at the cost of lower recall (40.9 %). Pick when false alarms are the bigger problem.'
        ),
        recommended_files=['Qwen3-4B-2507-Geminized-v1.Q4_K_M.gguf'],
        benchmark_f1=0.537,
        benchmark_recall=0.409,
        benchmark_peak_vram_mb=5846,
        benchmark_total_seconds=424.0,
        benchmark_notes='high-precision low-recall 4B',
        name_patterns=['qwen3-4b-2507-geminized-v1', 'qwen3-4b-2507-geminized'],
        alt_benchmarks=[],
    ),
]


# Models we benchmarked and decided not to ship as built-in presets.
# These never appear in the curated dropdown (kept clean at 5
# entries), they're consulted only when the user lands on one of
# their HF repo ids via the free-text Search tab, in which case the
# Model Manager surfaces a ⚠️/❌ warning + reason so the user can
# avoid downloading several GB before discovering the model doesn't
# work for this task.  Same dataclass as ``CURATED_REPOS`` so the
# badge renderer doesn't need to special-case the source.
KNOWN_PROBLEMATIC_REPOS: list[CuratedRepo] = [
    CuratedRepo(
        repo_id='unsloth/Ministral-3-3B-Reasoning-2512-GGUF',
        display_name='⚠️ Ministral 3 · 3B Reasoning  (over-detects)',
        family='Mistral',
        description=(
            'Smaller cousin of the quality leader. Catches about 7 of every 10 leaks but raises far too many false alarms. Listed here for comparison only. Pick the 8B Reasoning model instead if you have the GPU memory for it.'
        ),
        recommended_files=[],
        benchmark_f1=0.397,
        benchmark_recall=0.682,
        benchmark_peak_vram_mb=9610,
        benchmark_total_seconds=141.0,
        benchmark_notes='over-detects (many false alarms)',
        compatibility_status='low_quality',
        compatibility_reason='Catches some leaks but raises many false alarms.',
        name_patterns=['ministral-3-3b-reasoning'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='LiquidAI/LFM2.5-1.2B-Thinking-GGUF',
        display_name='⚠️ LFM 2.5 · 1.2B  (misses most leaks)',
        family='Other',
        description=(
            'Very small footprint (about 3.9 GB GPU memory) but misses most leaks: catches roughly 3 of every 10. Listed here so users on tiny GPUs see why we do not recommend it as a fallback.'
        ),
        recommended_files=[],
        benchmark_f1=0.381,
        benchmark_recall=0.273,
        benchmark_peak_vram_mb=3880,
        benchmark_total_seconds=119.0,
        benchmark_notes='misses most leaks',
        compatibility_status='low_quality',
        compatibility_reason='Too small for this task; misses most leaks.',
        name_patterns=['lfm2.5-1.2b', 'lfm-2.5-1.2b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/deepseek-coder-6.7b-instruct-GGUF',
        display_name='⚠️ DeepSeek Coder · 6.7B  (over-detects, slow)',
        family='Other',
        description=(
            'Treats nearly every code identifier as a leak and is the slowest model we tested (about 5.7 minutes on the 5-PDF test). Catches only about 3 of every 10 real leaks. Listed for comparison; not recommended.'
        ),
        recommended_files=[],
        benchmark_f1=0.338,
        benchmark_recall=0.273,
        benchmark_peak_vram_mb=22221,
        benchmark_total_seconds=339.0,
        benchmark_notes='over-detects code identifiers, slow',
        compatibility_status='low_quality',
        compatibility_reason='Over-detects code identifiers and is very slow.',
        name_patterns=['deepseek-coder-6.7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Qwen3-4B-Qwen3.6-plus-Reasoning-Slerp-GGUF',
        display_name='⚠️ Qwen3 4B Reasoning Slerp  (misses most leaks, slow)',
        family='Qwen',
        description=(
            'A community merge of Qwen3 4B with a reasoning fine-tune. Catches only about 2 of every 10 leaks and is unusually slow for a 4B (about 6.5 minutes on the 5-PDF test). Listed here so users searching for community Qwen merges see why we do not recommend this one.'
        ),
        recommended_files=[],
        benchmark_f1=0.338,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=8172,
        benchmark_total_seconds=387.0,
        benchmark_notes='misses most leaks, slow',
        compatibility_status='low_quality',
        compatibility_reason='Catches only about 2 of every 10 leaks and is slow.',
        name_patterns=['qwen3-4b-qwen3.6-plus-reasoning-slerp'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF',
        display_name='⚠️ Nemotron 3 Nano · 4B  (too small)',
        family='Other',
        description=(
            'Tiny (about 4.4 GB GPU memory) and very fast (about 40 seconds on the 5-PDF test) but catches only about 3 of every 10 leaks. Listed for users on small GPUs; the recommended default catches twice as many leaks for about twice the GPU memory.'
        ),
        recommended_files=[],
        benchmark_f1=0.338,
        benchmark_recall=0.273,
        benchmark_peak_vram_mb=4436,
        benchmark_total_seconds=40.0,
        benchmark_notes='fast but misses most leaks',
        compatibility_status='low_quality',
        compatibility_reason='Catches only about 3 of every 10 leaks.',
        name_patterns=['nemotron-3-nano-4b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3.5-2B-Claude-4.6-Opus-Reasoning-Distilled-GGUF',
        display_name='Qwen 3.5 · 2B Claude-Opus distill  (just below cut)',
        family='Qwen',
        description=(
            '2B Claude-4.6-Opus distill, three builds. The Q8_0 (~1.9 GB) is the headline pick: highest precision in the small bracket (~83 %) but recall stops at 43 %. Quality 57 / 100. Pick when you want few false alarms on a tiny GPU and accept missing some leaks. The Q4_K_M and v2-imat Q5_K_M variants trade some quality for smaller disk / different recall-precision profile, see the per-build breakdown below.'
        ),
        recommended_files=['Qwen3.5-2B.Q8_0.gguf', 'Qwen3.5-2B.Q4_K_M.gguf', 'qwen3.5-2b-claude-opus-4.6-high-resoning-v2-sft-q5_k_m-imat.gguf'],
        benchmark_f1=0.57,
        benchmark_recall=0.43,
        benchmark_peak_vram_mb=3975,
        benchmark_total_seconds=178.0,
        benchmark_notes='high-precision tiny pick',
        name_patterns=['qwen3.5-2b-claude-4.6-opus-reasoning-distilled'],
        primary_label='Q8_0',
        primary_summary='Headline build. Quality 57 / 100, precision 83 %, recall 43 %.',
        alt_benchmarks=[{'label': 'Q4_K_M', 'filename': 'Qwen3.5-2B.Q4_K_M.gguf', 'f1': 0.459, 'recall': 0.318, 'vram_mb': 3367, 'seconds': 124.0, 'summary': 'Smaller (~1.2 GB) at Quality 46 / 100. Precision 82.4 %, recall 31.8 %. Pick when disk pressure matters more than the last 11 quality points.'}, {'label': 'v2 SFT Q5_K_M imat', 'filename': 'qwen3.5-2b-claude-opus-4.6-high-resoning-v2-sft-q5_k_m-imat.gguf', 'f1': 0.324, 'recall': 0.386, 'vram_mb': 3671, 'seconds': 144.0, 'summary': 'Higher-reasoning v2 SFT (~1.3 GB). Quality 32 / 100: trades precision for slightly higher recall and floods Review with false positives. Not recommended over the Q8_0 baseline.'}],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/Meta-Llama-3-8B-Instruct-GGUF',
        display_name='⚠️ Meta Llama 3 · 8B Instruct',
        family='Llama',
        description=(
            'Vanilla Llama 3 8B Instruct (Q4_K_M, ~4.6 GB). Quality 47 / 100, too many false alarms, useful only for comparison.'
        ),
        recommended_files=['Meta-Llama-3-8B-Instruct-Q4_K_M.gguf'],
        benchmark_f1=0.47,
        benchmark_recall=0.61,
        benchmark_peak_vram_mb=7437,
        benchmark_total_seconds=124.0,
        benchmark_notes='too many false alarms',
        compatibility_status='low_quality',
        compatibility_reason='Below the quality cut; many false alarms.',
        name_patterns=['meta-llama-3-8b-instruct'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='sujinwo/Qwen3.5-0.8B-Claude-4.6-Opus',
        display_name='⚠️ Qwen 3.5 · 0.8B Claude-Opus distill  (smallest GGUF)',
        family='Qwen',
        description=(
            'Tiny 0.8B Claude-4.6-Opus distill (Q8_0, ~775 MB, smallest GGUF in the catalog). Quality 42 / 100, below curated cut. Pick only when disk / VRAM rules out everything else.'
        ),
        recommended_files=['Qwen3.5-0.8B.Q8_0.gguf'],
        benchmark_f1=0.419,
        benchmark_recall=0.386,
        benchmark_peak_vram_mb=2769,
        benchmark_total_seconds=115.9,
        benchmark_notes='smallest GGUF; below the cut',
        compatibility_status='low_quality',
        compatibility_reason='Below the quality cut; listed for size only.',
        name_patterns=['qwen3.5-0.8b-claude-4.6-opus'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/GLM-4.6V-Flash-GGUF',
        display_name='⚠️ GLM 4.6V Flash  (vision model, low quality)',
        family='Other',
        description=(
            "GLM 4.6V Flash vision-language model (Q5_K_M, ~6.6 GB). Quality 40 / 100 on text-only anonymization. Architecture isn't tuned for the JSON-output task this pipeline runs."
        ),
        recommended_files=['GLM-4.6V-Flash-Q5_K_M.gguf'],
        benchmark_f1=0.4,
        benchmark_recall=0.41,
        benchmark_peak_vram_mb=8532,
        benchmark_total_seconds=80.0,
        benchmark_notes='vision-language model on a text task',
        compatibility_status='low_quality',
        compatibility_reason='Vision-language model; weak on text-only JSON output.',
        name_patterns=['glm-4.6v-flash'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Seed-Coder-8B-Reasoning-GGUF',
        display_name='⚠️ Seed-Coder · 8B Reasoning  (low quality, slow)',
        family='Other',
        description=(
            'Code-tuned 8B reasoning (Q4_K_XL, ~4.8 GB). Quality 36 / 100. Recall is very low (catches only ~3 of 10 leaks); also slow on reasoning traces.'
        ),
        recommended_files=['Seed-Coder-8B-Reasoning-UD-Q4_K_XL.gguf'],
        benchmark_f1=0.36,
        benchmark_recall=0.27,
        benchmark_peak_vram_mb=7694,
        benchmark_total_seconds=660.0,
        benchmark_notes='misses most leaks; slow',
        compatibility_status='low_quality',
        compatibility_reason='Misses most leaks; very slow.',
        name_patterns=['seed-coder-8b-reasoning'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='bartowski/Llama-3.2-3B-Instruct-GGUF',
        display_name='⚠️ Llama 3.2 · 3B Instruct  (low quality)',
        family='Llama',
        description=(
            'Llama 3.2 3B Instruct (Q5_K_M, ~2.2 GB). Quality 31 / 100. Floods Review with false positives (precision ~23 %).'
        ),
        recommended_files=['Llama-3.2-3B-Instruct-Q5_K_M.gguf'],
        benchmark_f1=0.31,
        benchmark_recall=0.46,
        benchmark_peak_vram_mb=5172,
        benchmark_total_seconds=196.0,
        benchmark_notes='floods Review with false positives',
        compatibility_status='low_quality',
        compatibility_reason='Floods Review with false positives.',
        name_patterns=['llama-3.2-3b-instruct'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='alpha-ai/llama-3.2-3B-Reason-Reflect-Lite-GGUF',
        display_name='⚠️ Llama 3.2 · 3B Reason-Reflect Lite  (low quality)',
        family='Llama',
        description=(
            'Reasoning-tuned 3B (Q4_K_M, ~1.9 GB). Decent precision but very low recall, Quality 31 / 100. Listed for comparison; the 4B Claude-Opus distill is a much better small-model pick.'
        ),
        recommended_files=['llama-3.2-3B-Reason-Reflect-Lite.Q4_K_M.gguf'],
        benchmark_f1=0.31,
        benchmark_recall=0.2,
        benchmark_peak_vram_mb=4797,
        benchmark_total_seconds=35.0,
        benchmark_notes='misses most leaks',
        compatibility_status='low_quality',
        compatibility_reason='Misses most leaks.',
        name_patterns=['llama-3.2-3b-reason-reflect-lite'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mistralai/Magistral-Small-2507-GGUF',
        display_name='⚠️ Magistral Small · 24B  (huge, low quality)',
        family='Mistral',
        description=(
            'Mistral Magistral Small 24B (Q4_K_M, ~13.3 GB on disk, ~17 GB peak VRAM). Despite the size, Quality is 30 / 100 on this corpus: high precision but recall stops at 18 %.'
        ),
        recommended_files=['Magistral-Small-2507-Q4_K_M.gguf'],
        benchmark_f1=0.3,
        benchmark_recall=0.18,
        benchmark_peak_vram_mb=16782,
        benchmark_total_seconds=698.0,
        benchmark_notes='huge model, very low recall',
        compatibility_status='low_quality',
        compatibility_reason='High precision but recall stops at 18 %.',
        name_patterns=['magistral-small-2507'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='faunix/QwenSeek-2B-GGUF',
        display_name='⚠️ QwenSeek · 2B  (low quality)',
        family='Qwen',
        description=(
            'QwenSeek 2B BF16 (~3.6 GB). Quality 30 / 100, floods Review with false positives. The 4B Q5_K_M default and the Claude-Opus distills cover the same disk-size tier with much higher quality.'
        ),
        recommended_files=['QwenSeek-2B-BF16.gguf'],
        benchmark_f1=0.295,
        benchmark_recall=0.386,
        benchmark_peak_vram_mb=5597,
        benchmark_total_seconds=175.0,
        benchmark_notes='floods Review with false positives',
        compatibility_status='low_quality',
        compatibility_reason='Floods Review with false positives.',
        name_patterns=['qwenseek-2b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Viesar/gemma-3-4b-opus-reasoning-distill-GGUF',
        display_name='⚠️ Gemma 3 · 4B Opus distill  (low quality)',
        family='Gemma',
        description=(
            'Gemma 3 4B reasoning distill (Q4_K_M, ~2.3 GB). Quality 21 / 100, almost no useful detection. Listed for comparison; the curated 4B Claude-Opus distill is the right small-model pick.'
        ),
        recommended_files=['gemma-3-4b-it.Q4_K_M.gguf'],
        benchmark_f1=0.21,
        benchmark_recall=0.25,
        benchmark_peak_vram_mb=4618,
        benchmark_total_seconds=522.0,
        benchmark_notes='almost no useful detection',
        compatibility_status='low_quality',
        compatibility_reason='Almost no useful detection on this task.',
        name_patterns=['gemma-3-4b-opus-reasoning-distill'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Deecon-SecurityAnalyst-1.5B-GGUF',
        display_name='⚠️ Deecon Security Analyst · 1.5B  (Tier-0 baseline)',
        family='Other',
        description=(
            'Security-themed 1.5B model (Q8_0, ~1.5 GB). Quality 30 / 100: the LLM rarely returns useful candidates, so the pipeline ends up running on the Tier-0 regex layer alone. Fastest of the 1.5 GB tier (~56 s on the 5-PDF test) but recall stops at 18 %. Listed for comparison.'
        ),
        recommended_files=['Deecon-SecurityAnalyst-1.5B.Q8_0.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=3692,
        benchmark_total_seconds=56.0,
        benchmark_notes='LLM rarely contributes; falls back to Tier-0',
        compatibility_status='low_quality',
        compatibility_reason='Catches only ~2 of every 10 leaks; pipeline ends up on the regex baseline.',
        name_patterns=['deecon-securityanalyst'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='squ11z1/DeepSeek-R1-Opus',
        display_name='⚠️ DeepSeek R1 · Opus distill  (Tier-0 baseline)',
        family='Other',
        description=(
            'Compact DeepSeek-R1 / Claude-Opus distill (Q8_0, ~1.8 GB). Quality 30 / 100 (precision 80 %, recall 18 %): the model is conservative and rarely surfaces candidates, so the pipeline collapses to the Tier-0 regex layer. Listed for comparison.'
        ),
        recommended_files=['model-q8_0.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=3914,
        benchmark_total_seconds=208.0,
        benchmark_notes='conservative; misses most leaks',
        compatibility_status='low_quality',
        compatibility_reason='Catches only ~2 of every 10 leaks; pipeline collapses to the regex baseline.',
        name_patterns=['deepseek-r1-opus'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Cicikus-v3-1.4B-Opus4.6-Powered-GGUF',
        display_name='⚠️ Cicikus v3 · 1.4B Opus4.6  (Tier-0 baseline)',
        family='Other',
        description=(
            'Tiny 1.4B Opus4.6-powered model (Q8_0, ~1.4 GB). Quality 30 / 100, recall 18 %. The LLM rarely contributes useful candidates; the pipeline ends up running on the Tier-0 regex layer.'
        ),
        recommended_files=['Cicikus-v3-1.4B-Opus4.6-Powered.Q8_0.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=3778,
        benchmark_total_seconds=99.0,
        benchmark_notes='LLM rarely contributes; falls back to Tier-0',
        compatibility_status='low_quality',
        compatibility_reason='Catches only ~2 of every 10 leaks; pipeline ends up on the regex baseline.',
        name_patterns=['cicikus-v3', 'cicikus-1.4b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='DavidAU/Qwen3-Zero-Coder-Reasoning-V2-0.8B-NEO-EX-GGUF',
        display_name='⚠️ Qwen3 Zero-Coder Reasoning · 0.8B  (low quality)',
        family='Qwen',
        description=(
            'Coder-focused Qwen3 distill (F16, ~1.5 GB). Quality 30 / 100: precision 43.5 % and recall 22.7 %, the model treats too many code identifiers as leaks while missing real ones. Listed for comparison.'
        ),
        recommended_files=['Qwen3-Zro-Cdr-Reason-V2-0.8B-NEO-EX-D_AU-F16.gguf'],
        benchmark_f1=0.299,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=5338,
        benchmark_total_seconds=135.0,
        benchmark_notes='coder bias hurts detection',
        compatibility_status='low_quality',
        compatibility_reason='Coder bias mis-categorises code identifiers; low recall on real leaks.',
        name_patterns=['qwen3-zero-coder-reasoning'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Nguuma/security-slm-unsloth-1.5b',
        display_name='⚠️ Security SLM · 1.5B (unsloth)  (Tier-0 baseline)',
        family='Other',
        description=(
            'Security-themed small language model (F16, ~1.0 GB). Quality 27 / 100, the lowest of this batch. The LLM rarely returns useful structured candidates; the pipeline runs on the regex baseline.'
        ),
        recommended_files=['security-slm-finetuned.gguf'],
        benchmark_f1=0.267,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=3172,
        benchmark_total_seconds=217.0,
        benchmark_notes='LLM rarely contributes; falls back to Tier-0',
        compatibility_status='low_quality',
        compatibility_reason='Catches only ~2 of every 10 leaks; pipeline ends up on the regex baseline.',
        name_patterns=['security-slm-unsloth', 'security-slm-finetuned'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/gemma-4-E4B-it-GGUF',
        display_name='❌ Gemma 4 (any size)  (does not work here)',
        family='Gemma',
        description=(
            'The Gemma 4 family does not work in this pipeline. Its architecture uses a short attention window that cannot fit the long instructions the detector needs, so the model never returns useful results. Verified on the small (E2B, E4B) sizes and shared by the 26B and 31B sizes. Listed here so users see the issue before downloading several gigabytes of weights.'
        ),
        recommended_files=[],
        benchmark_f1=0.302,
        benchmark_recall=0.0,
        benchmark_notes='returns no usable results',
        compatibility_status='incompatible',
        compatibility_reason='Architecture cannot fit the detector instructions; the model never returns useful results.',
        name_patterns=['gemma-4-'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Qwen3Guard-Gen-4B-GGUF',
        display_name='❌ Qwen3 Guard (any size)  (refuses the task)',
        family='Qwen',
        description=(
            'The Qwen3 Guard family is a safety classifier designed to gate other models. It refuses to produce the structured answers the anonymizer needs, so the pipeline gets nothing back. The behaviour is shared by every size in the family.'
        ),
        recommended_files=[],
        benchmark_f1=0.302,
        benchmark_recall=0.0,
        benchmark_notes='refuses the task',
        compatibility_status='incompatible',
        compatibility_reason='Safety classifier; refuses to produce structured answers.',
        name_patterns=['qwen3guard', 'qwen3-guard'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='AngelSlim/Hy-MT1.5-1.8B-2bit-GGUF',
        display_name='❌ Hy-MT 1.5 · 1.8B 2-bit  (unsupported quant)',
        family='Other',
        description=(
            "Aggressive 2-bit quantisation (tensor type 2) that the shipped llama.cpp build cannot load. Server fails at startup with 'load_model: failed to load model'. The model itself may be fine on a build compiled with the required GGML flags; for now it is unusable in the default deployment. Pick a Q4_K_M or Q8_0 build of the same model when one is published."
        ),
        recommended_files=[],
        benchmark_notes='server fails to load 2-bit weights',
        compatibility_status='incompatible',
        compatibility_reason='2-bit GGUF quantisation not supported by the bundled llama.cpp build; server fails to load the model.',
        name_patterns=['hy-mt1.5', 'hy-mt1-5', 'hy-mt-1.5'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Qwen3.5-2B-GGUF',
        display_name='⚠️ Qwen 3.5 · 2B (Unsloth UD Q4_K_XL)  (Q 49/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 49 / 100 on the 5-PDF anonymization corpus. Below cut on this corpus. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3.5-2B-UD-Q4_K_XL.gguf'],
        benchmark_f1=0.486,
        benchmark_recall=0.386,
        benchmark_peak_vram_mb=3265,
        benchmark_total_seconds=120.0,
        benchmark_notes='below cut on this corpus',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 49 vs. 76).',
        name_patterns=['qwen3.5-2b-ud-q4', 'unsloth-qwen3.5-2b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Liontix/Qwen3-4B-Sonnet-4-GPT-5-Distill-GGUF',
        display_name='⚠️ Qwen 3 · 4B Sonnet 4 GPT-5 distill  (Q 48/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 48 / 100 on the 5-PDF anonymization corpus. High recall, low precision. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['qwen3-4B-Claude-Sonnet-4-distill_Q4_K_M.gguf'],
        benchmark_f1=0.477,
        benchmark_recall=0.705,
        benchmark_peak_vram_mb=5684,
        benchmark_total_seconds=147.0,
        benchmark_notes='high recall, low precision',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 48 vs. 76).',
        name_patterns=['qwen3-4b-sonnet-4-gpt-5-distill'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/wavecoder-ultra-6.7b-GGUF',
        display_name='⚠️ WaveCoder Ultra · 6.7B  (Q 47/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 47 / 100 on the 5-PDF anonymization corpus. Needs 11+ gb vram for a poor-band quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['wavecoder-ultra-6.7b-IQ4_NL.gguf'],
        benchmark_f1=0.468,
        benchmark_recall=0.409,
        benchmark_peak_vram_mb=11113,
        benchmark_total_seconds=515.0,
        benchmark_notes='needs 11+ GB VRAM for a Poor-band quality',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 47 vs. 76).',
        name_patterns=['wavecoder-ultra-6.7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/WithIn-Us-Coder-4B.gguf',
        display_name='⚠️ WithIn-Us Coder · 4B  (Q 47/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 47 / 100 on the 5-PDF anonymization corpus. Coder bias hurts detection. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['WithIn-Us-Coder-4B.Q4_K_M.gguf'],
        benchmark_f1=0.468,
        benchmark_recall=0.409,
        benchmark_peak_vram_mb=4762,
        benchmark_total_seconds=157.0,
        benchmark_notes='coder bias hurts detection',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 47 vs. 76).',
        name_patterns=['within-us-coder', 'withinus-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Qwen3.5-0.8B-GGUF',
        display_name='⚠️ Qwen 3.5 · 0.8B (Unsloth UD Q8_K_XL)  (Q 44/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 44 / 100 on the 5-PDF anonymization corpus. Sub-1 gb but below cut. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3.5-0.8B-UD-Q8_K_XL.gguf'],
        benchmark_f1=0.439,
        benchmark_recall=0.409,
        benchmark_peak_vram_mb=3067,
        benchmark_total_seconds=160.0,
        benchmark_notes='sub-1 GB but below cut',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 44 vs. 76).',
        name_patterns=['qwen3.5-0.8b-ud-q8'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Darwin-2B-Opus-GGUF',
        display_name='⚠️ Darwin · 2B Opus distill  (Q 42/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 42 / 100 on the 5-PDF anonymization corpus. Below cut. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Darwin-2B-Opus.Q4_K_M.gguf'],
        benchmark_f1=0.417,
        benchmark_recall=0.455,
        benchmark_peak_vram_mb=3128,
        benchmark_total_seconds=106.0,
        benchmark_notes='below cut',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 42 vs. 76).',
        name_patterns=['darwin-2b-opus'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Evelyn67/Qwen-3.5-2B-Uncensored-High-Opus-4.6-GGUF',
        display_name='⚠️ Qwen 3.5 · 2B Uncensored Opus (Q6_K)  (Q 42/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 42 / 100 on the 5-PDF anonymization corpus. High recall low precision. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['qwen3.5-2b-uncensored-Q6_K.gguf'],
        benchmark_f1=0.418,
        benchmark_recall=0.636,
        benchmark_peak_vram_mb=3400,
        benchmark_total_seconds=186.0,
        benchmark_notes='high recall low precision',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 42 vs. 76).',
        name_patterns=['qwen-3.5-2b-uncensored-high-opus', 'qwen3.5-2b-uncensored'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/Qwen3.5-0.8B-GGUF',
        display_name='⚠️ Qwen 3.5 · 0.8B (Unsloth UD Q4_K_XL)  (Q 41/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 41 / 100 on the 5-PDF anonymization corpus. Smallest tier, below cut. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3.5-0.8B-UD-Q4_K_XL.gguf'],
        benchmark_f1=0.413,
        benchmark_recall=0.432,
        benchmark_peak_vram_mb=2530,
        benchmark_total_seconds=144.0,
        benchmark_notes='smallest tier, below cut',
        compatibility_status='low_quality',
        compatibility_reason='Below the curated cut (Q 41 vs. 76).',
        name_patterns=['qwen3.5-0.8b-ud-q4'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Nvidia.Agentic.Coder-4B-GGUF',
        display_name='⚠️ Nvidia Agentic Coder · 4B  (Q 38/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 38 / 100 on the 5-PDF anonymization corpus. Coder-tuned, low recall. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['IBM-Agentic-Nvidia-Q4_K_M.gguf'],
        benchmark_f1=0.375,
        benchmark_recall=0.273,
        benchmark_peak_vram_mb=4253,
        benchmark_total_seconds=33.0,
        benchmark_notes='coder-tuned, low recall',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 38 vs. 76).',
        name_patterns=['nvidia.agentic.coder', 'nvidia-agentic-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Agent.Nano.Coder-2B-gguf',
        display_name='⚠️ Agent Nano Coder · 2B  (Q 35/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 35 / 100 on the 5-PDF anonymization corpus. Very slow, low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Agent.Nano.Coder-Q4_K_M.gguf'],
        benchmark_f1=0.352,
        benchmark_recall=0.432,
        benchmark_peak_vram_mb=4102,
        benchmark_total_seconds=543.0,
        benchmark_notes='very slow, low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 35 vs. 76).',
        name_patterns=['agent.nano.coder', 'agent-nano-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/OlympicCoder-7B-GGUF',
        display_name='⚠️ OlympicCoder · 7B  (Q 33/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 33 / 100 on the 5-PDF anonymization corpus. Coder bias, very low recall. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['OlympicCoder-7B-Q4_K_M.gguf'],
        benchmark_f1=0.327,
        benchmark_recall=0.205,
        benchmark_peak_vram_mb=6412,
        benchmark_total_seconds=453.0,
        benchmark_notes='coder bias, very low recall',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 33 vs. 76).',
        name_patterns=['olympiccoder-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/Skywork-OR1-7B-Preview-GGUF',
        display_name='⚠️ Skywork OR1 · 7B Preview  (Q 32/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 32 / 100 on the 5-PDF anonymization corpus. Low quality on this corpus. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Skywork-OR1-7B-Preview-Q4_K_M.gguf'],
        benchmark_f1=0.32,
        benchmark_recall=0.273,
        benchmark_peak_vram_mb=6412,
        benchmark_total_seconds=219.0,
        benchmark_notes='low quality on this corpus',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 32 vs. 76).',
        name_patterns=['skywork-or1-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/IBM-Opus4.7-Obscure.Reasoner.3B.GGUF',
        display_name='⚠️ IBM Opus 4.7 · 3B Obscure Reasoner  (Q 32/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 32 / 100 on the 5-PDF anonymization corpus. Conservative, low recall. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['IBM-Opus4.7-Obscure.Reasoner.3B-Q4_K_M.gguf'],
        benchmark_f1=0.321,
        benchmark_recall=0.205,
        benchmark_peak_vram_mb=4452,
        benchmark_total_seconds=112.0,
        benchmark_notes='conservative, low recall',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 32 vs. 76).',
        name_patterns=['ibm-opus4.7-obscure-reasoner'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='rikunarita-2/Qwen3.5-2B-Claude-Opus-4.6-high-resoning-v2-SFT-imatrix-Q5_K_M-GGUF',
        display_name='⚠️ Qwen 3.5 · 2B Claude-Opus v2 SFT imat Q5 (rikunarita-2 mirror)  (Q 31/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 31 / 100 on the 5-PDF anonymization corpus. False-positive flood. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['qwen3.5-2b-claude-opus-4.6-high-resoning-v2-sft-q5_k_m-imat.gguf'],
        benchmark_f1=0.314,
        benchmark_recall=0.432,
        benchmark_peak_vram_mb=3261,
        benchmark_total_seconds=146.0,
        benchmark_notes='false-positive flood',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 31 vs. 76).',
        name_patterns=['rikunarita-2/qwen3.5-2b-claude-opus'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Mythoseek-GGUF',
        display_name='⚠️ Mythoseek  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Slow, mid-low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Mythoseek.Q4_K_M.gguf'],
        benchmark_f1=0.304,
        benchmark_recall=0.318,
        benchmark_peak_vram_mb=7139,
        benchmark_total_seconds=533.0,
        benchmark_notes='slow, mid-low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['mythoseek'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/LLaDA-MoE-7B-A1B-Instruct-TD-GGUF',
        display_name='⚠️ LLaDA-MoE · 7B A1B Instruct TD  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Fast but tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['LLaDA-MoE-7B-A1B-Instruct-TD.Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=5687,
        benchmark_total_seconds=22.0,
        benchmark_notes='fast but Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['llada-moe-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/SmolLM2-135M-Instruct-GGUF',
        display_name='⚠️ SmolLM 2 · 135M Instruct  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Too small for this task. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['SmolLM2-135M-Instruct-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=1690,
        benchmark_total_seconds=70.0,
        benchmark_notes='too small for this task',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['smollm2-135m'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/zeta-GGUF',
        display_name='⚠️ Zeta  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['zeta-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=6410,
        benchmark_total_seconds=185.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['zeta-gguf'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/SmolLM3-3B-GGUF',
        display_name='⚠️ SmolLM 3 · 3B  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. High recall but flood. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['SmolLM3-3B-Q4_K_M.gguf'],
        benchmark_f1=0.299,
        benchmark_recall=0.432,
        benchmark_peak_vram_mb=4241,
        benchmark_total_seconds=272.0,
        benchmark_notes='high recall but flood',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['smollm3-3b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/OpenCoder-1.5B-Instruct-GGUF',
        display_name='⚠️ OpenCoder · 1.5B Instruct  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['OpenCoder-1.5B-Instruct-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=5134,
        benchmark_total_seconds=22.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['opencoder-1.5b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/openhands-lm-1.5b-v0.1-GGUF',
        display_name='⚠️ OpenHands LM · 1.5B v0.1  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['openhands-lm-1.5b-v0.1-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=2881,
        benchmark_total_seconds=168.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['openhands-lm-1.5b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/OpenReasoning-Nemotron-1.5B-GGUF',
        display_name='⚠️ OpenReasoning Nemotron · 1.5B  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['OpenReasoning-Nemotron-1.5B-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=2882,
        benchmark_total_seconds=281.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['openreasoning-nemotron-1.5b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Llama-Coyote.Coder-4B.gguf',
        display_name='⚠️ Llama Coyote Coder · 4B  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Llama-Coyote.Coder-4B-Q4_K_M.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=8140,
        benchmark_total_seconds=218.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['llama-coyote.coder', 'llama-coyote-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Entity-27th/opus-1.5-Q4_K_M-GGUF',
        display_name='⚠️ Opus 1.5  (Q 30/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 30 / 100 on the 5-PDF anonymization corpus. Tier-0 baseline. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['opus-1.5-q4_k_m.gguf'],
        benchmark_f1=0.296,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=2417,
        benchmark_total_seconds=22.0,
        benchmark_notes='Tier-0 baseline',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 30 vs. 76).',
        name_patterns=['opus-1.5-q4'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='jalpan04/qwen-researcher',
        display_name='⚠️ Qwen Researcher (F16 fallback)  (Q 29/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 29 / 100 on the 5-PDF anonymization corpus. F16 fallback, tier-0. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['qwen-researcher-f16.gguf'],
        benchmark_f1=0.291,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=2784,
        benchmark_total_seconds=148.0,
        benchmark_notes='F16 fallback, Tier-0',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 29 vs. 76).',
        name_patterns=['qwen-researcher'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='MaziyarPanahi/Qwen3-4B-Thinking-2507-GGUF',
        display_name='⚠️ Qwen 3 · 4B Thinking 2507 (MaziyarPanahi)  (Q 29/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 29 / 100 on the 5-PDF anonymization corpus. Thinking burns context. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3-4B-Thinking-2507.Q4_K_M.gguf'],
        benchmark_f1=0.286,
        benchmark_recall=0.182,
        benchmark_peak_vram_mb=5676,
        benchmark_total_seconds=512.0,
        benchmark_notes='thinking burns context',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 29 vs. 76).',
        name_patterns=['qwen3-4b-thinking-2507', 'maziyarpanahi/qwen3-4b-thinking'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='MaziyarPanahi/WizardLM-2-7B-GGUF',
        display_name='⚠️ WizardLM 2 · 7B  (Q 29/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 29 / 100 on the 5-PDF anonymization corpus. Low precision, low recall. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['WizardLM-2-7B.Q4_K_M.gguf'],
        benchmark_f1=0.288,
        benchmark_recall=0.341,
        benchmark_peak_vram_mb=7028,
        benchmark_total_seconds=280.0,
        benchmark_notes='low precision, low recall',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 29 vs. 76).',
        name_patterns=['wizardlm-2-7b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF',
        display_name='⚠️ DeepSeek R1 · Distill Qwen 1.5B  (Q 27/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 27 / 100 on the 5-PDF anonymization corpus. 1.5b too small. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['DeepSeek-R1-Distill-Qwen-1.5B-UD-Q4_K_XL.gguf'],
        benchmark_f1=0.27,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=2885,
        benchmark_total_seconds=85.0,
        benchmark_notes='1.5B too small',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 27 vs. 76).',
        name_patterns=['deepseek-r1-distill-qwen-1.5b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/Falcon3-3B-Instruct-GGUF',
        display_name='⚠️ Falcon 3 · 3B Instruct  (Q 26/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 26 / 100 on the 5-PDF anonymization corpus. Low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Falcon3-3B-Instruct-Q4_K_M.gguf'],
        benchmark_f1=0.256,
        benchmark_recall=0.341,
        benchmark_peak_vram_mb=4332,
        benchmark_total_seconds=130.0,
        benchmark_notes='low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 26 vs. 76).',
        name_patterns=['falcon3-3b-instruct'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/ZR1-1.5B-GGUF',
        display_name='⚠️ ZR1 · 1.5B  (Q 26/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 26 / 100 on the 5-PDF anonymization corpus. Low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['ZR1-1.5B-Q4_K_M.gguf'],
        benchmark_f1=0.263,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=2866,
        benchmark_total_seconds=130.0,
        benchmark_notes='low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 26 vs. 76).',
        name_patterns=['zr1-1.5b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='TeichAI/LFM2.5-1.2B-Thinking-Pony-Alpha-Distill-GGUF',
        display_name='⚠️ LFM 2.5 · 1.2B Thinking Pony Alpha distill  (Q 26/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 26 / 100 on the 5-PDF anonymization corpus. Low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['LFM2.5-1.2B-Thinking-Pony-Alpha-Distill-gguf-q4_0.gguf'],
        benchmark_f1=0.265,
        benchmark_recall=0.205,
        benchmark_peak_vram_mb=2302,
        benchmark_total_seconds=142.0,
        benchmark_notes='low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 26 vs. 76).',
        name_patterns=['lfm2.5-1.2b-thinking-pony-alpha-distill'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='prism-ml/Bonsai-8B-gguf',
        display_name='⚠️ Bonsai · 8B (Q1_0 experimental)  (Q 23/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 23 / 100 on the 5-PDF anonymization corpus. Q1_0 experimental quant. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Bonsai-8B-Q1_0.gguf'],
        benchmark_f1=0.234,
        benchmark_recall=0.386,
        benchmark_peak_vram_mb=4373,
        benchmark_total_seconds=393.0,
        benchmark_notes='Q1_0 experimental quant',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 23 vs. 76).',
        name_patterns=['bonsai-8b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/cogito-v1-preview-llama-3B-GGUF',
        display_name='⚠️ Cogito v1 Preview Llama · 3B  (Q 23/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 23 / 100 on the 5-PDF anonymization corpus. High recall, very low precision. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['cogito-v1-preview-llama-3B-Q4_K_M.gguf'],
        benchmark_f1=0.228,
        benchmark_recall=0.523,
        benchmark_peak_vram_mb=4834,
        benchmark_total_seconds=311.0,
        benchmark_notes='high recall, very low precision',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 23 vs. 76).',
        name_patterns=['cogito-v1-preview-llama-3b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/ERNIE-4.5-0.3B-GGUF',
        display_name='⚠️ ERNIE 4.5 · 0.3B  (Q 21/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 21 / 100 on the 5-PDF anonymization corpus. Tiny, low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['ERNIE-4.5-0.3B-Q4_K_M.gguf'],
        benchmark_f1=0.212,
        benchmark_recall=0.205,
        benchmark_peak_vram_mb=1917,
        benchmark_total_seconds=14.0,
        benchmark_notes='tiny, low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 21 vs. 76).',
        name_patterns=['ernie-4.5-0.3b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/EXAONE-4.0-1.2B-GGUF',
        display_name='⚠️ EXAONE 4.0 · 1.2B  (Q 21/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 21 / 100 on the 5-PDF anonymization corpus. Small, low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['EXAONE-4.0-1.2B-GGUF-Q4_K_M.gguf'],
        benchmark_f1=0.206,
        benchmark_recall=0.25,
        benchmark_peak_vram_mb=2970,
        benchmark_total_seconds=76.0,
        benchmark_notes='small, low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 21 vs. 76).',
        name_patterns=['exaone-4.0-1.2b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/Qwen2.5-Coder-3B-GGUF',
        display_name='⚠️ Qwen 2.5 Coder · 3B  (Q 19/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 19 / 100 on the 5-PDF anonymization corpus. Coder bias, low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen2.5-Coder-3B-Q4_K_M.gguf'],
        benchmark_f1=0.191,
        benchmark_recall=0.205,
        benchmark_peak_vram_mb=3864,
        benchmark_total_seconds=261.0,
        benchmark_notes='coder bias, low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 19 vs. 76).',
        name_patterns=['qwen2.5-coder-3b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='JairoDanielMT/qwen3-1.7B-finetuning-claude-opus.4.6-5K-GUFF',
        display_name='⚠️ Qwen 3 · 1.7B Claude-Opus 4.6 5K finetune  (Q 21/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 21 / 100 on the 5-PDF anonymization corpus. Low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3-1.7B.Q4_K_M.gguf'],
        benchmark_f1=0.208,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=3962,
        benchmark_total_seconds=240.0,
        benchmark_notes='low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 21 vs. 76).',
        name_patterns=['jairodanielmt/qwen3-1.7b-finetuning-claude-opus.4.6-5k'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='Jackrong/Qwen3-1.7B-Gemini-3-Pro-Distilled-GGUF',
        display_name='⚠️ Qwen 3 · 1.7B Gemini 3 Pro distill  (Q 19/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 19 / 100 on the 5-PDF anonymization corpus. Gemini distill too small. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['qwen3-1.7b.Q4_K_M.gguf'],
        benchmark_f1=0.19,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=4029,
        benchmark_total_seconds=319.0,
        benchmark_notes='Gemini distill too small',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 19 vs. 76).',
        name_patterns=['jackrong/qwen3-1.7b-gemini-3-pro-distilled'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='TeichAI/Qwen3-1.7B-Gemini-2.5-Flash-Lite-Preview-Distill-GGUF',
        display_name='⚠️ Qwen 3 · 1.7B Gemini 2.5 Flash Lite distill (F16)  (Q 19/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 19 / 100 on the 5-PDF anonymization corpus. F16 fallback, low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen3-1.7B-Gemini-2.5-Flash-Lite-Preview-Distill-f16.gguf'],
        benchmark_f1=0.187,
        benchmark_recall=0.364,
        benchmark_peak_vram_mb=6391,
        benchmark_total_seconds=287.0,
        benchmark_notes='F16 fallback, low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 19 vs. 76).',
        name_patterns=['teichai/qwen3-1.7b-gemini-2.5-flash-lite'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='lmstudio-community/aya-23-8B-GGUF',
        display_name='⚠️ Aya 23 · 8B  (Q 15/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 15 / 100 on the 5-PDF anonymization corpus. False-positive flood. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['aya-23-8B-IQ4_NL.gguf'],
        benchmark_f1=0.152,
        benchmark_recall=0.432,
        benchmark_peak_vram_mb=7907,
        benchmark_total_seconds=428.0,
        benchmark_notes='false-positive flood',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 15 vs. 76).',
        name_patterns=['aya-23-8b'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/IBM-Grok4-Ultra.Fast.Coder-1B-GGUF',
        display_name='⚠️ IBM Grok 4 · 1B UltraFast Coder  (Q 14/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 14 / 100 on the 5-PDF anonymization corpus. Coder bias, very low quality. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['IBM-Grok4-UltraFast-Coder-1B.Q4_K_M.gguf'],
        benchmark_f1=0.142,
        benchmark_recall=0.227,
        benchmark_peak_vram_mb=3410,
        benchmark_total_seconds=231.0,
        benchmark_notes='coder bias, very low quality',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 14 vs. 76).',
        name_patterns=['ibm-grok4-ultra.fast.coder', 'ibm-grok4-ultrafast-coder'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='mradermacher/Qwen-3-0.6B-Claude-4.7-Opus-Distilled-GGUF',
        display_name='⚠️ Qwen 3 · 0.6B Claude-Opus distill  (Q 14/100)',
        family='Qwen',
        description=(
            'Round-3 bench entry. Quality 14 / 100 on the 5-PDF anonymization corpus. Too small. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Qwen-3-0.6B-Claude-4.7-Opus-Distilled.Q4_K_M.gguf'],
        benchmark_f1=0.138,
        benchmark_recall=0.25,
        benchmark_peak_vram_mb=3280,
        benchmark_total_seconds=159.0,
        benchmark_notes='too small',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 14 vs. 76).',
        name_patterns=['qwen-3-0.6b-claude-4.7-opus-distilled'],
        alt_benchmarks=[],
    ),
    CuratedRepo(
        repo_id='WithinUsAI/Llama3.2-Agent.Hermes.Coder-3B-gguf',
        display_name='⚠️ Llama 3.2 · Agent Hermes Coder 3B  (Q 4/100)',
        family='Other',
        description=(
            'Round-3 bench entry. Quality 4 / 100 on the 5-PDF anonymization corpus. Lowest quality of the round. Listed for completeness; the curated picks above are better choices for this task at the same VRAM tier.'
        ),
        recommended_files=['Llama3.2-AgentHermes-Coder-3B-Q4_K_M.gguf'],
        benchmark_f1=0.044,
        benchmark_recall=0.136,
        benchmark_peak_vram_mb=4820,
        benchmark_total_seconds=402.0,
        benchmark_notes='lowest quality of the round',
        compatibility_status='low_quality',
        compatibility_reason='Low quality (Q 4 vs. 76).',
        name_patterns=['llama3.2-agent.hermes.coder', 'llama3.2-agenthermes-coder'],
        alt_benchmarks=[],
    ),
]


def _hf_base_model(repo_id: str, *, token: Optional[str] = None) -> Optional[str]:
    """Return the ``base_model`` value from a repo's HF card.

    Most community GGUF re-publishers set ``base_model:
    <canonical-org>/<canonical-repo>`` in their model-card YAML
    front-matter; following that field is by far the most reliable
    way to identify *which* underlying model a mirror packages,
    even when the repo name has been renamed beyond recognition
    (``someone/MyMix-of-Gemma4-GGUF``).  Returns ``None`` when:

    * ``huggingface_hub`` is not installed,
    * the repo doesn't exist or the request fails,
    * the card has no ``base_model`` field.

    Cached at module level so repeated badge renders for the same
    repo don't re-hit the HF API.
    """
    # ``dict.get`` defaults to ``None``, which is itself a valid
    # cached value (the repo has no base_model). Use the sentinel
    # explicitly so a cache *miss* falls through to the HF call.
    cached = _BASE_MODEL_CACHE.get(repo_id, _BASE_MODEL_SENTINEL)
    if cached is not _BASE_MODEL_SENTINEL:
        return cached  # type: ignore[return-value]
    try:
        from huggingface_hub import HfApi  # type: ignore
    except Exception:
        _BASE_MODEL_CACHE[repo_id] = None
        return None
    try:
        api = HfApi(token=token or load_hf_token())
        info = api.model_info(repo_id)
    except Exception:
        _BASE_MODEL_CACHE[repo_id] = None
        return None
    card = getattr(info, "card_data", None)
    base = getattr(card, "base_model", None) if card else None
    # The ``base_model`` field is a string for single-base repos and
    # a list for merges/finetunes that cite multiple parents, we
    # take the first entry which, in practice, is the dominant
    # parent.
    if isinstance(base, list):
        base = base[0] if base else None
    if not isinstance(base, str) or not base.strip():
        _BASE_MODEL_CACHE[repo_id] = None
        return None
    base = base.strip()
    _BASE_MODEL_CACHE[repo_id] = base
    return base


# Sentinel + dict-based cache instead of ``functools.lru_cache`` so
# tests can clear it cheaply via ``_BASE_MODEL_CACHE.clear()`` and
# patch the underlying HF API call without LRU bookkeeping noise.
_BASE_MODEL_SENTINEL: object = object()
_BASE_MODEL_CACHE: dict[str, Optional[str]] = {}


def _local_repo_metadata(repo_id: str) -> Optional[CuratedRepo]:
    """The cheap, network-free portion of :func:`repo_metadata`.

    Resolution order:

    1. Exact id match in :data:`CURATED_REPOS`.
    2. Exact id match in :data:`KNOWN_PROBLEMATIC_REPOS`.
    3. Case-insensitive substring match against
       :attr:`CuratedRepo.name_patterns` of the curated list, so
       community mirrors of a top-5 model still surface its badge
       and description in the Search HF tab.
    4. Same substring match against the problematic list, so
       community mirrors of a known-bad model trigger the warning.

    Exact matches always win over patterns.  The ★ "recommended
    file" tag is *not* shared automatically with mirrors: it lives
    in :attr:`CuratedRepo.recommended_files`, which is matched on
    filename inside :func:`pick_recommended_files`.  A mirror that
    keeps the original GGUF filename will inherit the ★ on that
    file; one that renames the file will not.
    """
    for r in CURATED_REPOS:
        if r.repo_id == repo_id:
            return r
    for r in KNOWN_PROBLEMATIC_REPOS:
        if r.repo_id == repo_id:
            return r
    rid = (repo_id or "").lower()
    if not rid:
        return None
    for r in CURATED_REPOS:
        for pat in r.name_patterns:
            if pat and pat.lower() in rid:
                return r
    for r in KNOWN_PROBLEMATIC_REPOS:
        for pat in r.name_patterns:
            if pat and pat.lower() in rid:
                return r
    return None


def repo_metadata(
    repo_id: str,
    *,
    follow_base_model: bool = True,
    token: Optional[str] = None,
) -> Optional[CuratedRepo]:
    """Look up benchmark / compatibility metadata for an HF repo id.

    Search order:

    1. Exact match in :data:`CURATED_REPOS` (preserves the canonical
       ``recommended_files`` pinning for shipped presets).
    2. Exact match in :data:`KNOWN_PROBLEMATIC_REPOS`.
    3. Case-insensitive substring match against
       :attr:`CuratedRepo.name_patterns` of the problematic list, so
       community republishers (``bartowski/gemma-4-E4B-it-GGUF``,
       ``mradermacher/...``) still surface the ⚠️/❌ warning even
       if we never indexed their exact ``owner/repo`` id.
    4. (When ``follow_base_model`` is true and steps 1–3 miss.)
       Fetch the repo's ``card_data.base_model`` from HF and recurse
       once with ``follow_base_model=False``, catches mirrors whose
       repo name doesn't include the family identifier but which
       declare the canonical parent in their model-card YAML
       front-matter.

    Pattern matching is intentionally one-way: we never auto-curate
    a republisher of a top-5 model, only auto-warn about a
    republisher of a known-bad model.  That keeps the ★ recommended
    star tied to the publisher we actually tested.
    """
    hit = _local_repo_metadata(repo_id)
    if hit is not None:
        return hit
    if not follow_base_model:
        return None
    base = _hf_base_model(repo_id, token=token)
    if not base or base == repo_id:
        return None
    # Recurse with the upstream parent, but disable further
    # base-model chasing so a malformed card can't loop us across
    # the HF API.
    return _local_repo_metadata(base)


# ---- API (search / list files) --------------------------------------------


@dataclass
class RepoFile:
    filename: str
    size_bytes: int
    is_recommended: bool = False


@dataclass
class RepoCard:
    repo_id: str
    family: str = "Other"
    license: str = "unknown"
    downloads: int = 0
    likes: int = 0
    last_modified: str = ""
    pipeline_tag: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class SearchFilters:
    family: Optional[set[str]] = None
    quant: Optional[set[str]] = None
    size_buckets: Optional[set[str]] = None
    vision: Optional[bool] = None


def pick_recommended_files(
    filenames: Iterable[str],
    curated: Iterable[str] = (),
) -> set[str]:
    """Derive the "★ recommended" set for a repo's GGUF listing.

    Rule (matches the in-app star priority):

    1. Always include any explicit ``curated`` entries (single source
       of truth for preset/profile pinning).
    2. Add every full-precision file (anything with ``f16``,
       ``bf16``, or ``fp16`` in the name).
    3. If the repo has no full-precision file at all, fall back to
       the single best-quality file via :func:`quality_rank` so each
       repo always exposes at least one star.

    Vision-projector files (``mmproj-*.gguf``) are filtered upstream
    before this helper sees them.
    """
    candidates = [n for n in filenames if "mmproj" not in n.lower()]
    out: set[str] = {n for n in curated if n}
    for n in candidates:
        ln = n.lower()
        # All three full-precision spellings the publishers use:
        # ``BF16`` (bfloat16), ``F16`` (legacy llama.cpp half), and
        # ``FP16`` (HF/PyTorch convention). ``f16`` is a substring of
        # ``bf16`` but *not* of ``fp16`` (the ``p`` breaks the
        # match), so we check both explicitly.
        if "bf16" in ln or "fp16" in ln or "f16" in ln:
            out.add(n)
    if not out and candidates:
        winner = min(candidates, key=lambda x: (quality_rank(x), x.lower()))
        out.add(winner)
    return out


def list_repo_files(
    repo_id: str, *, token: Optional[str] = None
) -> list[RepoFile]:
    """List GGUF files in a repo with their sizes.

    Falls back gracefully if huggingface_hub is unavailable.
    """
    try:
        from huggingface_hub import HfApi  # type: ignore
    except Exception:
        return []
    api = HfApi(token=token or load_hf_token())
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except Exception:
        return []
    meta = repo_metadata(repo_id)
    curated_recs = list(meta.recommended_files) if meta else []
    raw: list[tuple[str, int]] = []
    for sib in getattr(info, "siblings", []) or []:
        name = getattr(sib, "rfilename", "") or ""
        if not name.endswith(".gguf"):
            continue
        # The pipeline is text-only, filter out vision projector
        # heads (``mmproj-*.gguf``) so the user only sees usable
        # weight files in the Model Manager.
        if "mmproj" in name.lower():
            continue
        size = int(getattr(sib, "size", 0) or 0)
        raw.append((name, size))
    rec = pick_recommended_files((n for n, _ in raw), curated_recs)
    out = [
        RepoFile(filename=n, size_bytes=s, is_recommended=n in rec)
        for n, s in raw
    ]
    out.sort(key=lambda f: (-int(f.is_recommended), f.filename.lower()))
    return out


def search_repos(
    query: str,
    *,
    filters: Optional[SearchFilters] = None,
    sort: str = "downloads",
    limit: int = 30,
    token: Optional[str] = None,
) -> list[RepoCard]:
    try:
        from huggingface_hub import HfApi  # type: ignore
    except Exception:
        return []
    api = HfApi(token=token or load_hf_token())
    try:
        models = list(
            api.list_models(
                search=query or None,
                filter="gguf",
                sort=sort,
                limit=limit,
            )
        )
    except Exception:
        return []
    cards: list[RepoCard] = []
    for m in models:
        repo_id = getattr(m, "id", "") or getattr(m, "modelId", "") or ""
        if not repo_id:
            continue
        family = "Other"
        for fam in ("Qwen", "Llama", "Mistral", "Phi", "Gemma"):
            if fam.lower() in repo_id.lower():
                family = fam
                break
        license_ = getattr(m, "tags", []) or []
        license_str = "unknown"
        for t in license_:
            if isinstance(t, str) and t.startswith("license:"):
                license_str = t.split(":", 1)[1]
                break
        cards.append(
            RepoCard(
                repo_id=repo_id,
                family=family,
                license=license_str,
                downloads=int(getattr(m, "downloads", 0) or 0),
                likes=int(getattr(m, "likes", 0) or 0),
                last_modified=str(getattr(m, "last_modified", "") or ""),
                pipeline_tag=str(getattr(m, "pipeline_tag", "") or ""),
                tags=list(license_),
            )
        )
    if filters and filters.family:
        cards = [c for c in cards if c.family in filters.family]
    return cards


# ---- Local library --------------------------------------------------------


# Quality priority, lower is better.  Used to pick the "best
# quality" file in a repo when there's no explicit recommended_files
# entry, and to mark the ★ in the Library tab.  Order:
#
#   1. Full-precision (BF16 / F16 / FP16)
#   2. Unsloth dynamic quants (UD_Q*_K_XL, slightly better than the
#      same Q level from other publishers)
#   3. Standard k-quants Q8 → Q4
#   4. IQ4_* (very-low-bit but acceptable)
#   5. Q3 / Q2 (only as last resort)
_QUALITY_PRIORITY: tuple[str, ...] = (
    "bf16", "f16", "fp16",
    "ud-q8_k_xl", "ud-q8_k_m", "ud-q8_k", "q8_k_xl", "q8_0", "q8_k",
    "ud-q6_k_xl", "ud-q6_k", "q6_k_xl", "q6_k",
    "ud-q5_k_xl", "ud-q5_k_m", "q5_k_m", "q5_0",
    "ud-q4_k_xl", "ud-q4_k_m", "ud-q4_k", "q4_k_m", "q4_0",
    "iq4_xs", "iq4_nl",
    "q3_k_l", "q3_k_m", "q3_k_s",
    "iq3_m", "iq3_s", "iq3_xs",
    "q2_k_l", "q2_k",
)


def quality_rank(filename: str) -> int:
    """Return a *lower-is-better* rank for the given GGUF filename.

    Picks the longest matching key from :data:`_QUALITY_PRIORITY` so
    ``UD-Q8_K_XL`` correctly outranks plain ``Q8_K`` and ``Q8_0``.
    Files that match no key (unknown quant suffix) get the worst
    rank so they sort last.
    """
    n = filename.lower()
    best = len(_QUALITY_PRIORITY)
    for i, key in enumerate(_QUALITY_PRIORITY):
        if key in n:
            best = min(best, i)
            # Keep scanning, a longer key further down might still
            # match better. Order in _QUALITY_PRIORITY is canonical.
    return best


def quant_tag(filename: str) -> str:
    """Return the human-readable quant tag of a GGUF filename
    (``"BF16"``, ``"Q5_K_M"``, ``"UD-Q4_K_XL"``, …) or an empty
    string when the filename carries no recognisable tag.

    Uses the same canonical order as :data:`_QUALITY_PRIORITY` so a
    file like ``model-UD-Q8_K_XL.gguf`` resolves to ``"UD-Q8_K_XL"``
    instead of plain ``"Q8_K"``.  The returned tag preserves
    underscores and is uppercased for display.
    """
    n = filename.lower()
    for key in _QUALITY_PRIORITY:
        if key in n:
            return key.upper()
    return ""


def best_local_per_repo(paths: list[Path]) -> set[Path]:
    """Return the subset of ``paths`` to flag with ★ in the Library
    tab.

    Two rules, applied per directory:

    1. **Curated allowlist wins when present.** If the directory
       name decodes to an HF repo we recognise (curated top-5 or a
       known mirror via :func:`repo_metadata`), every on-disk file
       whose name is listed in :attr:`CuratedRepo.recommended_files`
       gets ★.  This is what makes the BF16 *and* the Q5_K_M build
       of Ministral 3 8B Reasoning both light up under the same
       ``unsloth__Ministral-3-8B-Reasoning-2512-GGUF`` directory.
    2. **Best-quality fallback otherwise.** For directories we
       don't recognise (user-imported GGUFs, repos we never
       indexed), pick the single best-quality file via
       :func:`quality_rank` so each repo still exposes at least
       one ★.

    ``mmproj-*.gguf`` files are filtered out: the pipeline is
    text-only.
    """
    by_dir: dict[Path, list[Path]] = {}
    for p in paths:
        if "mmproj" in p.name.lower():
            continue
        by_dir.setdefault(p.parent, []).append(p)
    out: set[Path] = set()
    for d, files in by_dir.items():
        if not files:
            continue
        # Reverse the ``owner/repo -> owner__repo`` directory
        # convention from ``_safe_repo_dirname``.
        repo_id = d.name.replace("__", "/")
        meta = repo_metadata(repo_id, follow_base_model=False)
        if meta and meta.recommended_files:
            rec_names = set(meta.recommended_files)
            picks = {p for p in files if p.name in rec_names}
            if picks:
                out.update(picks)
                continue
        winner = min(files, key=lambda x: (quality_rank(x.name), x.name.lower()))
        out.add(winner)
    return out


def local_models() -> list[Path]:
    """Return every weight ``*.gguf`` file under :data:`MODELS_DIR`.

    Walks one level of subdirectories so per-repo downloads created by
    :func:`download_model` are visible alongside any flat-layout files
    from older installs.  Vision projector files (``mmproj-*.gguf``)
    are filtered out, the pipeline is text-only and the Library
    tab should only surface usable weights.
    """
    ensure_models_dir()
    out: list[Path] = []
    for p in MODELS_DIR.glob("*.gguf"):
        if "mmproj" in p.name.lower():
            continue
        out.append(p)
    for sub in MODELS_DIR.iterdir():
        if sub.is_dir():
            for p in sub.glob("*.gguf"):
                if "mmproj" in p.name.lower():
                    continue
                out.append(p)
    return sorted(out)


def delete_local(path: Path) -> bool:
    """Delete a GGUF entry from the on-disk library.

    Important: callers may pass paths that are *symlinks* under
    ``MODELS_DIR`` pointing OUTSIDE of it (e.g. a user-imported
    model that lives elsewhere on disk). We must:
      1. Validate that the **link path** itself sits under MODELS_DIR
         (so we never delete arbitrary files via attacker-controlled
         input).
      2. Unlink the symlink as-is, without following it. The
         underlying target file is left alone.
    For regular files we resolve symlinks and apply the same MODELS_DIR
    check after resolution.
    """
    raw = Path(path)
    models_root = MODELS_DIR.resolve()
    # Guard 1: link-level. ``raw.expanduser().absolute()`` does NOT
    # follow symlinks, so we can safely test "is the entry itself
    # inside MODELS_DIR?".
    link_path = raw.expanduser().absolute()
    if not str(link_path).startswith(str(models_root)):
        return False
    is_symlink = link_path.is_symlink()
    if not is_symlink:
        # Regular file, re-check after resolving (defense-in-depth
        # for any TOCTOU between expanduser/absolute and unlink).
        target = raw.resolve()
        if not str(target).startswith(str(models_root)):
            return False
    try:
        link_path.unlink()
        try:
            parent = link_path.parent
            if (
                parent != models_root
                and parent.is_dir()
                and not any(parent.iterdir())
            ):
                parent.rmdir()
        except Exception:
            pass
        return True
    except Exception:
        return False


# ---- Download with resume + cancel ---------------------------------------


class GatedRepoError(Exception):
    """Raised when HF returns 401/403 indicating the repo requires accept."""

    def __init__(self, repo_id: str, message: str = "") -> None:
        self.repo_id = repo_id
        super().__init__(message or f"Gated repo: {repo_id}")


class OfflineMode(Exception):
    pass


@dataclass
class DownloadResult:
    ok: bool
    path: Optional[Path] = None
    error: str = ""
    cancelled: bool = False


ProgressCb = Callable[[int, int, float, int], None]
# (downloaded_bytes, total_bytes, speed_bps, eta_seconds)

PhaseCb = Callable[[str], None]
# "lookup" | "downloading" | "finalizing" | "verifying" | "done"


def download_model(
    repo_id: str,
    filename: str,
    *,
    dst: Optional[Path] = None,
    progress_cb: Optional[ProgressCb] = None,
    phase_cb: Optional[PhaseCb] = None,
    stop_event: Optional[threading.Event] = None,
    token: Optional[str] = None,
    chunk_bytes: int = 128 * 1024,
    max_retries: int = 3,
    offline: bool = False,
) -> DownloadResult:
    """Download a single file from a HF repo with resume + cancel."""
    if offline:
        raise OfflineMode("Offline mode: HF download disabled")
    ensure_models_dir()
    if dst is None:
        # Default destination is per-repo so files like ``mmproj-BF16.gguf``
        # that exist in multiple repos never clobber each other.
        dst = repo_models_dir(repo_id) / filename
    else:
        dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    url = f"{HF_BASE}/{repo_id}/resolve/main/{filename}"
    headers = {}
    tk = token or load_hf_token()
    if tk:
        headers["Authorization"] = f"Bearer {tk}"

    if phase_cb:
        phase_cb("lookup")

    # HEAD to learn size and access
    try:
        head = requests.head(url, headers=headers, allow_redirects=True, timeout=30)
        if head.status_code in (401, 403):
            raise GatedRepoError(repo_id, f"HF returned {head.status_code} on {filename}")
        head.raise_for_status()
        total = int(head.headers.get("content-length") or 0)
    except GatedRepoError:
        raise
    except Exception as e:
        return DownloadResult(ok=False, error=f"HEAD failed: {e}")

    resume_from = part.stat().st_size if part.exists() else 0
    if resume_from >= total > 0:
        # Already complete on disk - finalize and return
        if phase_cb:
            phase_cb("finalizing")
        try:
            os.replace(part, dst)
        except Exception:
            pass
        if phase_cb:
            phase_cb("done")
        return DownloadResult(ok=True, path=dst)

    if phase_cb:
        phase_cb("downloading")
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        if stop_event and stop_event.is_set():
            return DownloadResult(ok=False, cancelled=True, error="cancelled")
        try:
            range_headers = dict(headers)
            if resume_from > 0:
                range_headers["Range"] = f"bytes={resume_from}-"
            with requests.get(
                url, headers=range_headers, stream=True, timeout=60, allow_redirects=True
            ) as r:
                if r.status_code in (401, 403):
                    raise GatedRepoError(repo_id, f"HF returned {r.status_code}")
                r.raise_for_status()
                mode = "ab" if resume_from > 0 and r.status_code == 206 else "wb"
                if mode == "wb":
                    resume_from = 0
                downloaded = resume_from
                start_t = time.monotonic()
                last_emit = 0.0
                speed_ema = 0.0
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=chunk_bytes):
                        if stop_event and stop_event.is_set():
                            return DownloadResult(
                                ok=False, cancelled=True, error="cancelled"
                            )
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        elapsed = max(0.001, now - start_t)
                        inst = (downloaded - resume_from) / elapsed
                        speed_ema = 0.7 * speed_ema + 0.3 * inst if speed_ema else inst
                        if progress_cb and (now - last_emit) >= 0.2:
                            eta = int((total - downloaded) / max(1, speed_ema)) if total else 0
                            progress_cb(downloaded, total, speed_ema, eta)
                            last_emit = now
                if progress_cb and total:
                    progress_cb(downloaded, total, speed_ema, 0)
            # Finalize
            if phase_cb:
                phase_cb("finalizing")
            os.replace(part, dst)
            if phase_cb:
                phase_cb("done")
            return DownloadResult(ok=True, path=dst)
        except GatedRepoError:
            raise
        except Exception as e:
            last_exc = e
            time.sleep(min(2 ** attempt, 8))
            resume_from = part.stat().st_size if part.exists() else 0
    return DownloadResult(ok=False, error=f"download failed after {max_retries} attempts: {last_exc}")


__all__ = [
    "MODELS_DIR",
    "HF_TOKEN_PATH",
    "ensure_models_dir",
    "repo_models_dir",
    "expected_path_for",
    "load_hf_token",
    "save_hf_token",
    "CuratedRepo",
    "CURATED_REPOS",
    "KNOWN_PROBLEMATIC_REPOS",
    "repo_metadata",
    "RepoFile",
    "RepoCard",
    "SearchFilters",
    "list_repo_files",
    "pick_recommended_files",
    "quality_rank",
    "best_local_per_repo",
    "search_repos",
    "local_models",
    "delete_local",
    "DownloadResult",
    "download_model",
    "GatedRepoError",
    "OfflineMode",
]
