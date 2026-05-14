"""Estimate the on-disk size of a GGUF model from its filename / repo id.

The Model Manager curated tree and the first-run wizard's preset list
both need to surface a "~X GB" disk-size label next to a model before
the file is on the user's machine. They used to do it independently:

* The wizard parsed the parameter count + quantisation tag and
  multiplied by a per-quant bytes-per-param factor.
* The Model Manager used a different heuristic (``vram_mb / 1024 /
  1.2``) which inflated quantised models by ~50% (Q4_K_M of a 4B
  model showed ~3.9 GB instead of the real ~2.5 GB).

This module centralises the accurate logic so both call sites can
reuse it. There is intentionally NO synchronous HF API call here —
labels render on every paint of the curated tree, we cannot afford
the network round-trip.

Strategy, in order:

1. If an on-disk path is supplied AND exists, return the exact size
   from ``stat().st_size``.
2. Otherwise parse the parameter count (``\\d+(?:\\.\\d+)?\\s*[Bb]``)
   plus a known quantisation tag (``q4_k_m``, ``bf16`` …) out of any
   of the hint strings, multiply by the published per-quant bytes
   factor, return the rounded label.
3. Return ``""`` so the caller can fall back to its own placeholder.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union


# Bytes-per-parameter for the common GGUF quantisations, ordered
# longest-tag-first so ``q5_k_m`` matches before plain ``q5``. Numbers
# are derived from llama.cpp's published per-tensor sizes, rounded to
# two decimals. Cross-checked against real downloads: matches within
# ~5% for every curated entry in our catalog.
_QUANT_FACTORS: list[tuple[str, float]] = [
    ("bf16", 2.00),
    ("f16",  2.00),
    ("fp16", 2.00),
    ("q8_0", 1.06),
    ("q6_k", 0.82),
    ("q5_k_m", 0.70),
    ("q5_k_s", 0.66),
    ("q5_0", 0.70),
    ("q5_1", 0.75),
    ("q4_k_m", 0.60),
    ("q4_k_s", 0.56),
    ("q4_0", 0.56),
    ("q4_1", 0.61),
    ("q3_k_l", 0.50),
    ("q3_k_m", 0.48),
    ("q3_k_s", 0.45),
    ("q2_k", 0.36),
]

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def _format_gb(gb: float) -> str:
    if gb < 0.5:
        return f"~{gb * 1024:.0f} MB"
    if gb < 10.0:
        return f"~{gb:.1f} GB"
    return f"~{gb:.0f} GB"


def _format_path_size(path: Path) -> str:
    try:
        n_bytes = path.stat().st_size
    except OSError:
        return ""
    if n_bytes <= 0:
        return ""
    gb = n_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"~{gb:.1f} GB"
    mb = n_bytes / (1024 ** 2)
    return f"~{mb:.0f} MB"


def estimate_gguf_disk_label(
    *hints: str,
    on_disk: Optional[Union[str, Path]] = None,
) -> str:
    """Return a short ``"~X GB"`` / ``"~Y MB"`` size label for a GGUF.

    ``hints`` is any number of strings that can plausibly contain
    parameter-count + quantisation info (filename, repo_id, display
    name, …). They are concatenated and lower-cased before parsing.

    ``on_disk`` is an optional path. When the file exists, its real
    size on disk wins over the heuristic estimate. ``None`` and
    non-existent paths fall through to parsing the hints.

    Returns an empty string when neither path nor hints yielded a
    confident estimate. The caller is expected to render its own
    placeholder (e.g. ``"—"``).
    """
    if on_disk is not None:
        try:
            p = Path(on_disk)
            if p.exists():
                label = _format_path_size(p)
                if label:
                    return label
        except (TypeError, OSError):
            pass

    haystack = " ".join(h for h in hints if isinstance(h, str) and h).lower()
    if not haystack:
        return ""

    m = _PARAM_RE.search(haystack)
    if not m:
        return ""
    try:
        params_b = float(m.group(1))
    except ValueError:
        return ""
    if params_b <= 0:
        return ""

    factor: Optional[float] = None
    for tag, f in _QUANT_FACTORS:
        if tag in haystack:
            factor = f
            break
    if factor is None:
        return ""

    return _format_gb(params_b * factor)


__all__ = ["estimate_gguf_disk_label"]
