"""Read a finished ``run_precision_benchmark.py`` JSON report and
patch the curated catalog + builtin presets with the measured F1 /
recall / peak VRAM / runtime.

Edits in place:

* ``anonymize/hf_models.py`` , sets ``benchmark_*`` fields on each
  matching ``CuratedRepo`` (matched by ``model_repo`` of the linked
  preset, or by direct repo-id mapping below).
* ``config/server_profiles.yml``, rewrites the preset description
  with the measured peak-VRAM in place of the hand-written estimate.

Usage:

  .venv/bin/python bench/apply_bench_to_catalog.py /tmp/anonbench/precision_bf16/report.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
HF_MODELS = REPO / "anonymize" / "hf_models.py"
PROFILES_YML = REPO / "config" / "server_profiles.yml"

# Map preset name → curated repo_id used by the catalog.
_PRESET_TO_REPO = {
    "default":                       "unsloth/Qwen3.5-4B-GGUF",
    "qwen3.5-9b-bf16":               "unsloth/Qwen3.5-9B-GGUF",
    "ministral-3-8b-bf16":           "mistralai/Ministral-3-8B-Instruct-2512-GGUF",
    "granite-4.1-8b-bf16":           "unsloth/granite-4.1-8b-GGUF",
    "gemma-4-e4b-it-bf16":           "unsloth/gemma-4-E4B-it-GGUF",
    "qwen2.5-coder-7b-f16":          "unsloth/Qwen2.5-Coder-7B-Instruct-128K-GGUF",
    "gemma-4-e2b-it-bf16":           "unsloth/gemma-4-E2B-it-GGUF",
    "qwen3guard-gen-4b-f16":         "mradermacher/Qwen3Guard-Gen-4B-GGUF",
    "qwen3guard-gen-8b-f16":         "mradermacher/Qwen3Guard-Gen-8B-GGUF",
    "deepseek-coder-6.7b-f16":       "mradermacher/deepseek-coder-6.7b-instruct-GGUF",
    "nemotron-3-nano-4b-q4":         "nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF",
    "lfm-2.5-1.2b-f16":              "LiquidAI/LFM2.5-1.2B-Thinking-GGUF",
    "ministral-3-8b-reasoning-bf16": "unsloth/Ministral-3-8B-Reasoning-2512-GGUF",
    "ministral-3-3b-reasoning-bf16": "unsloth/Ministral-3-3B-Reasoning-2512-GGUF",
}


def patch_catalog(report: dict) -> int:
    """Patch CuratedRepo entries in hf_models.py with bench data."""
    src = HF_MODELS.read_text(encoding="utf-8")
    edits = 0
    for preset, info in report.items():
        repo_id = _PRESET_TO_REPO.get(preset)
        if repo_id is None or not info.get("started"):
            continue
        agg = info.get("agg") or {}
        mem = info.get("memory") or {}
        f1 = float(agg.get("f1") or 0.0)
        recall = float(agg.get("recall") or 0.0)
        peak_vram = int(mem.get("peak_vram_mb") or 0)
        elapsed = float(agg.get("elapsed_total_s") or 0.0)

        repo_pat = re.compile(
            r'CuratedRepo\(\s*\n\s*repo_id="' + re.escape(repo_id) + r'"',
            re.MULTILINE,
        )
        m = repo_pat.search(src)
        if not m:
            print(f"[skip] {repo_id}: not found in CuratedRepo list")
            continue
        # Find the closing paren of this CuratedRepo(...) constructor.
        start = m.start()
        depth = 0
        i = start
        while i < len(src):
            c = src[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        else:
            continue
        block = src[start:end + 1]
        # Remove any existing benchmark_* fields (idempotent re-apply).
        for fld in (
            "benchmark_f1",
            "benchmark_recall",
            "benchmark_peak_vram_mb",
            "benchmark_total_seconds",
            "benchmark_notes",
        ):
            block = re.sub(
                rf",?\s*{fld}=[^,)]+",
                "",
                block,
            )
        # Insert benchmark_* fields just before the trailing ).
        injected = (
            f",\n        benchmark_f1={f1:.3f},"
            f"\n        benchmark_recall={recall:.3f},"
            f"\n        benchmark_peak_vram_mb={peak_vram},"
            f"\n        benchmark_total_seconds={elapsed:.1f},"
            f'\n        benchmark_notes=""'
        )
        # ``block`` ends in ')'; insert before that.
        new_block = block[:-1].rstrip().rstrip(",") + injected + "\n    )"
        src = src[:start] + new_block + src[end + 1:]
        edits += 1
        print(
            f"[ok] {repo_id}: F1={f1*100:.1f}% recall={recall*100:.1f}% "
            f"vram={peak_vram} MB elapsed={elapsed:.0f}s"
        )

    HF_MODELS.write_text(src, encoding="utf-8")
    return edits


_DESC_RE = re.compile(
    r'^(\s*description:\s*")([^"]+)("\s*)$',
    re.MULTILINE,
)


def patch_profiles_yml(report: dict) -> int:
    src = PROFILES_YML.read_text(encoding="utf-8")
    edits = 0
    for preset, info in report.items():
        if not info.get("started"):
            continue
        mem = info.get("memory") or {}
        peak_vram = mem.get("peak_vram_mb")
        if not peak_vram:
            continue
        gb = peak_vram / 1024.0
        # Find the ``- name: <preset>`` block, then the ``description:`` line
        # that follows within the next ~20 lines.
        name_re = re.compile(rf"^  - name: {re.escape(preset)}\b", re.MULTILINE)
        n = name_re.search(src)
        if not n:
            print(f"[skip-yaml] preset {preset!r} not found")
            continue
        block_start = n.end()
        block = src[block_start:block_start + 800]
        m = _DESC_RE.search(block)
        if not m:
            continue
        old_desc = m.group(2)
        # Replace any "(~X GB VRAM)" trailer with the measured one.
        new_desc = re.sub(
            r"\(~?[\d.]+\s*GB\s*VRAM\)",
            f"(~{gb:.1f} GB VRAM)",
            old_desc,
        )
        if new_desc == old_desc and "VRAM" not in old_desc:
            new_desc = f"{old_desc} (~{gb:.1f} GB VRAM)"
        if new_desc == old_desc:
            continue
        full_old = m.group(0)
        full_new = m.group(1) + new_desc + m.group(3)
        # Substitute only inside the located block to avoid clobbering
        # other presets that share the same description text.
        replaced = block.replace(full_old, full_new, 1)
        src = src[:block_start] + replaced + src[block_start + len(block):]
        edits += 1
        print(f"[yaml-ok] {preset}: {peak_vram} MB peak VRAM injected")

    PROFILES_YML.write_text(src, encoding="utf-8")
    return edits


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <report.json>", file=sys.stderr)
        return 2
    report_path = Path(argv[1])
    if not report_path.exists():
        print(f"[fatal] report not found: {report_path}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    n_cat = patch_catalog(report)
    n_yml = patch_profiles_yml(report)
    print(f"\ncatalog edits: {n_cat}, profile YAML edits: {n_yml}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
