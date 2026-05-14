"""Multi-PDF anonymization stress harness.

For every PDF in the corpus, run the full pipeline N times against
an isolated per-iteration map and output dir. Aggregate detection
rates per value, residual-leak counts per category, and per-run
divergences. Writes a Markdown report under ``$BENCH_ROOT``.

Usage::

    .venv/bin/python bench/run_corpus_x5.py --runs 5 --cycle 1

Configuration via environment variables:

* ``BENCH_ROOT`` (default ``/tmp/anonbench``): where reports / fixtures /
  per-iteration maps land.
* ``BENCH_REAL_CORPUS_ROOT``: if set, the harness adds every PDF under
  the named pentest sub-directories when ``--mode real`` or ``--mode
  full`` is used.  Without it, only the synthetic fixtures are
  exercised, keeps the harness usable on a fresh checkout that has
  no private corpus available.

The harness shells out to ``bin/anonymize-dossier all`` so it always
exercises the same code path the user runs from the GUI (which also
calls ``stage_*`` directly).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import yaml


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "anonymize-dossier"
PYTHON = str(REPO / ".venv" / "bin" / "python")
BENCH_ROOT = Path(os.environ.get("BENCH_ROOT", "/tmp/anonbench"))


def discover_corpus(*, mode: str = "fast") -> list[Path]:
    """Corpus selection.

    ``mode='fast'`` returns only the synthetic fixtures (small,
    fast feedback loop for prompt-tuning cycles).
    ``mode='real'`` adds the user's pentest PDFs.
    ``mode='full'`` is real + the 1.4MB master document.
    """
    seen: set[str] = set()
    out: list[Path] = []

    def _add(p: Path) -> None:
        if not p.exists():
            return
        key = str(p.resolve())
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    fixtures = BENCH_ROOT / "fixtures"
    for f in (
        "synthetic_credentials.pdf",
        "synthetic_multilang.pdf",
        "synthetic_brand.pdf",
        "synthetic_negatives.pdf",
    ):
        _add(fixtures / f)

    # Pentest corpus is opt-in via env var so a fresh checkout
    # without any private PDFs can still exercise ``--mode fast``.
    real_root_env = os.environ.get("BENCH_REAL_CORPUS_ROOT", "").strip()
    if mode in ("real", "full") and real_root_env:
        real_root = Path(real_root_env).expanduser()
        # Pick up every PDF under the configured corpus root.
        # Sub-directory naming is left to the user, so the harness
        # is corpus-agnostic.
        if real_root.is_dir():
            for pdf in sorted(real_root.rglob("*.pdf")):
                _add(pdf)

    return out


def parse_applied(applied_path: Path) -> dict:
    """Return per-category event counts + the set of distinct ``from``
    values caught in this run. Used to compute detection rate.
    """
    if not applied_path.exists():
        return {"events_by_cat": {}, "values": set(), "total_events": 0}
    data = json.loads(applied_path.read_text(encoding="utf-8"))
    events_by_cat: dict[str, int] = defaultdict(int)
    values: set[tuple[str, str]] = set()
    total = 0
    for f in data.get("files") or []:
        for ev in f.get("events") or []:
            cat = ev.get("category") or "other"
            events_by_cat[cat] += 1
            total += 1
            v = ev.get("from") or ""
            if v:
                values.add((cat, v))
    return {
        "events_by_cat": dict(events_by_cat),
        "values": values,
        "total_events": total,
    }


_HIT_ROW = re.compile(r"^\|\s*(?P<file>[^|]+)\|\s*(?P<pat>[^|]+)\|\s*(?P<match>[^|]+)\|")


def parse_verifier_report(report_path: Path) -> list[dict]:
    """Lightweight parse of ``verifier_report.md``. Returns a list of
    {file, pattern, match} dicts, one per residual hit."""
    if not report_path.exists():
        return []
    out: list[dict] = []
    in_table = False
    for line in report_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            continue
        # skip header / separator rows
        if "---" in s:
            in_table = True
            continue
        if not in_table:
            continue
        m = _HIT_ROW.match(line)
        if not m:
            continue
        out.append(
            {
                "file": m.group("file").strip(),
                "pattern": m.group("pat").strip(),
                "match": m.group("match").strip(),
            }
        )
    return out


def parse_pending(pending_path: Path) -> int:
    if not pending_path.exists():
        return 0
    try:
        d = yaml.safe_load(pending_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return 0
    cands = d.get("candidates") or []
    return len(cands)


def grade_against_ground_truth(
    pdf_name: str, run_summary: dict
) -> dict:
    """For synthetic fixtures we have explicit ground truth. Compare
    each run's caught values against ``GROUND_TRUTH[pdf_name]`` to
    yield per-category miss / false-positive / wrong-category lists.
    For non-fixture PDFs returns an empty grading dict (the harness
    falls back to "any-run-stable" detection-rate analysis).
    """
    try:
        from bench.synthetic_fixtures import GROUND_TRUTH, NEGATIVES
    except Exception:
        return {}
    gt = GROUND_TRUTH.get(pdf_name)
    if gt is None:
        return {}
    caught: set[tuple[str, str]] = set()
    for cat, vals in (run_summary.get("values_by_cat") or {}).items():
        for v in vals:
            caught.add((cat, v))
    misses_by_cat: dict[str, list[str]] = {}
    wrong_cat: list[dict] = []
    caught_values = {v for _, v in caught}
    for cat_expected, vals in gt.items():
        for v in vals:
            if v not in caught_values:
                misses_by_cat.setdefault(cat_expected, []).append(v)
                continue
            actual_cat = next((c for c, vv in caught if vv == v), None)
            if actual_cat and actual_cat != cat_expected:
                wrong_cat.append(
                    {"value": v, "expected": cat_expected, "got": actual_cat}
                )
    # False-positive check: any caught value that is in NEGATIVES.
    false_positives = [
        {"category": c, "value": v}
        for c, v in sorted(caught)
        if v in NEGATIVES
    ]
    return {
        "ground_truth_total": sum(len(vs) for vs in gt.values()),
        "caught_count": sum(
            1 for vs in gt.values() for v in vs if v in caught_values
        ),
        "misses_by_cat": misses_by_cat,
        "wrong_category": wrong_cat,
        "false_positives_on_negatives": false_positives,
    }


def run_one(pdf: Path, out_dir: Path, *, llm_url: str, timeout_s: int) -> dict:
    """Invoke ``anonymize-dossier all`` on a single PDF and return a
    summary dict.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    map_path = out_dir / "substitution_map.yml"
    pending = out_dir / "needs_review.yml"
    auto_t0 = out_dir / "auto_promoted_t0.yml"
    auto_t1 = out_dir / "auto_promoted_t1.yml"
    applied = out_dir / "applied_substitutions.json"
    report = out_dir / "verifier_report.md"
    decisions = out_dir / "decisions_history.jsonl"

    cmd = [
        PYTHON,
        str(CLI),
        "all",
        str(pdf),
        "-o",
        str(out_dir),
        "--map",
        str(map_path),
        "--pending",
        str(pending),
        "--auto-t0",
        str(auto_t0),
        "--auto-t1",
        str(auto_t1),
        "--applied",
        str(applied),
        "--report",
        str(report),
        "--decisions",
        str(decisions),
        "--llm-url",
        llm_url,
        "--force-rescan",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(REPO),
            env={"PYTHONPATH": str(REPO), **__import__("os").environ},
        )
        rc = proc.returncode
        stderr = proc.stderr[-2000:]
        stdout = proc.stdout[-2000:]
    except subprocess.TimeoutExpired:
        rc = -9
        stderr = "[TIMEOUT]"
        stdout = ""
    elapsed = time.time() - t0

    applied_summary = parse_applied(applied)
    hits = parse_verifier_report(report)
    pending_count = parse_pending(pending)

    summary = {
        "pdf": pdf.name,
        "out": str(out_dir),
        "rc": rc,
        "elapsed_s": round(elapsed, 1),
        "events_by_cat": applied_summary["events_by_cat"],
        "values_by_cat": {
            c: sorted({v for cc, v in applied_summary["values"] if cc == c})
            for c in {cc for cc, _ in applied_summary["values"]}
        },
        "total_events": applied_summary["total_events"],
        "residual_hits": hits,
        "residual_count": len(hits),
        "pending_count": pending_count,
        "stderr_tail": stderr,
        "stdout_tail": stdout,
    }
    grading = grade_against_ground_truth(pdf.name, summary)
    if grading:
        summary["grading"] = grading
    return summary


def aggregate(per_pdf_runs: dict[str, list[dict]]) -> dict:
    """Compute per-PDF detection rate + cross-run residuals + (when
    we have ground truth) miss / false-positive aggregation."""
    summary: dict[str, dict] = {}
    for pdf, runs in per_pdf_runs.items():
        all_values: dict[tuple[str, str], int] = defaultdict(int)
        residuals: dict[str, int] = defaultdict(int)
        cat_totals = defaultdict(list)
        elapsed = []
        gt_misses: dict[tuple[str, str], int] = defaultdict(int)
        gt_wrong_cat: dict[tuple[str, str, str], int] = defaultdict(int)
        gt_total = 0
        gt_caught_runs: list[int] = []
        false_positives: dict[tuple[str, str], int] = defaultdict(int)
        for r in runs:
            elapsed.append(r["elapsed_s"])
            for cat, vals in r["values_by_cat"].items():
                for v in vals:
                    all_values[(cat, v)] += 1
                cat_totals[cat].append(len(vals))
            for h in r["residual_hits"]:
                key = f"{h['file']}::{h['pattern']}::{h['match']}"
                residuals[key] += 1
            g = r.get("grading") or {}
            if g:
                gt_total = max(gt_total, g.get("ground_truth_total", 0))
                gt_caught_runs.append(g.get("caught_count", 0))
                for cat, vs in (g.get("misses_by_cat") or {}).items():
                    for v in vs:
                        gt_misses[(cat, v)] += 1
                for w in g.get("wrong_category") or []:
                    gt_wrong_cat[(w["value"], w["expected"], w["got"])] += 1
                for fp in g.get("false_positives_on_negatives") or []:
                    false_positives[(fp["category"], fp["value"])] += 1
        flaky = [
            {"category": c, "value": v, "runs_caught": n, "runs_total": len(runs)}
            for (c, v), n in sorted(all_values.items())
            if n < len(runs)
        ]
        always_caught = [
            {"category": c, "value": v}
            for (c, v), n in sorted(all_values.items())
            if n == len(runs)
        ]
        summary[pdf] = {
            "runs": len(runs),
            "elapsed_s": elapsed,
            "always_caught_count": len(always_caught),
            "flaky_misses_count": len(flaky),
            "flaky_misses": flaky,
            "cat_event_counts": {c: l for c, l in cat_totals.items()},
            "residuals": residuals,
            "ground_truth_total": gt_total,
            "ground_truth_caught_per_run": gt_caught_runs,
            "ground_truth_misses": [
                {"category": c, "value": v, "missed_runs": n,
                 "runs_total": len(runs)}
                for (c, v), n in sorted(gt_misses.items(), key=lambda kv: -kv[1])
            ],
            "wrong_category": [
                {"value": v, "expected": exp, "got": got, "runs": n}
                for (v, exp, got), n in sorted(gt_wrong_cat.items(), key=lambda kv: -kv[1])
            ],
            "false_positives_on_negatives": [
                {"category": c, "value": v, "runs": n}
                for (c, v), n in sorted(false_positives.items(), key=lambda kv: -kv[1])
            ],
        }
    return summary


def render_report(corpus: list[Path], per_pdf_runs, agg, *, cycle: int) -> str:
    out: list[str] = []
    out.append(f"# Cycle {cycle}, anonymization stress test\n")
    out.append(
        f"Corpus: {len(corpus)} PDFs, runs/PDF: "
        f"{len(next(iter(per_pdf_runs.values()), []))}\n"
    )
    out.append("\n## Per-PDF summary\n")
    out.append(
        "| PDF | runs | elapsed (avg s) | always-caught | flaky | "
        "residuals | gt-coverage (avg) |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for pdf, info in agg.items():
        elapsed = info["elapsed_s"]
        avg = round(sum(elapsed) / max(len(elapsed), 1), 1)
        residuals = sum(info["residuals"].values())
        gt = info.get("ground_truth_total") or 0
        per_run = info.get("ground_truth_caught_per_run") or []
        gt_avg = (
            f"{round(sum(per_run) / max(len(per_run), 1), 1)}/{gt}"
            if gt else "n/a"
        )
        out.append(
            f"| {pdf} | {info['runs']} | {avg} | "
            f"{info['always_caught_count']} | {info['flaky_misses_count']} | "
            f"{residuals} | {gt_avg} |"
        )
    out.append("\n## Ground-truth misses (synthetic fixtures)\n")
    any_gt = False
    for pdf, info in agg.items():
        misses = info.get("ground_truth_misses") or []
        wrong = info.get("wrong_category") or []
        fps = info.get("false_positives_on_negatives") or []
        if not (misses or wrong or fps):
            continue
        any_gt = True
        out.append(f"### {pdf}\n")
        if misses:
            out.append("**Missed leaks (expected category | value | missed runs):**\n")
            out.append("| category | value | missed runs / total |")
            out.append("|---|---|---|")
            for m in misses:
                out.append(
                    f"| {m['category']} | `{m['value']}` | "
                    f"{m['missed_runs']} / {m['runs_total']} |"
                )
            out.append("")
        if wrong:
            out.append("**Wrong category (value | expected | got | runs):**\n")
            out.append("| value | expected | got | runs |")
            out.append("|---|---|---|---|")
            for w in wrong:
                out.append(
                    f"| `{w['value']}` | {w['expected']} | {w['got']} | "
                    f"{w['runs']} |"
                )
            out.append("")
        if fps:
            out.append("**False positives on negatives (these are NOT leaks):**\n")
            out.append("| category | value | runs flagged |")
            out.append("|---|---|---|")
            for fp in fps:
                out.append(f"| {fp['category']} | `{fp['value']}` | {fp['runs']} |")
            out.append("")
    if not any_gt:
        out.append("(none, synthetic fixtures fully covered, no false positives)\n")
    out.append("\n## Flaky misses (caught only in some runs)\n")
    any_flaky = False
    for pdf, info in agg.items():
        if not info["flaky_misses"]:
            continue
        any_flaky = True
        out.append(f"### {pdf}\n")
        out.append("| category | value | runs caught / total |")
        out.append("|---|---|---|")
        for m in info["flaky_misses"]:
            out.append(
                f"| {m['category']} | `{m['value']}` | "
                f"{m['runs_caught']} / {m['runs_total']} |"
            )
        out.append("")
    if not any_flaky:
        out.append("(none, every detected value was caught in every run)\n")
    out.append("\n## Verifier residuals (post-pipeline leaks)\n")
    any_residual = False
    for pdf, info in agg.items():
        if not info["residuals"]:
            continue
        any_residual = True
        out.append(f"### {pdf}\n")
        out.append("| pattern::match | times across runs |")
        out.append("|---|---|")
        for k, n in sorted(info["residuals"].items(), key=lambda kv: -kv[1]):
            out.append(f"| `{k}` | {n} |")
        out.append("")
    if not any_residual:
        out.append("(none, verifier reported zero residuals on every run)\n")
    out.append("\n## Per-run RC / errors\n")
    for pdf, runs in per_pdf_runs.items():
        for r in runs:
            if r["rc"] != 0:
                out.append(
                    f"- `{pdf}` iter {r['out'].rsplit('/', 1)[-1]}: "
                    f"rc={r['rc']}, stderr_tail=…{r['stderr_tail'][-300:]}"
                )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--cycle", type=int, default=1)
    ap.add_argument("--mode", default="fast", choices=["fast", "real", "full"])
    ap.add_argument("--llm-url", default="http://localhost:8080/v1")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--corpus-filter", default="", help="substring filter on PDF name")
    ap.add_argument("--out-root", default=str(BENCH_ROOT))
    args = ap.parse_args(argv)

    out_root = Path(args.out_root) / f"cycle{args.cycle}"
    out_root.mkdir(parents=True, exist_ok=True)

    corpus = discover_corpus(mode=args.mode)
    if args.corpus_filter:
        corpus = [p for p in corpus if args.corpus_filter in p.name]
    print(f"corpus = {len(corpus)} PDFs:")
    for p in corpus:
        print(f"  - {p}")

    per_pdf_runs: dict[str, list[dict]] = {}
    for pdf in corpus:
        per_pdf_runs[pdf.name] = []
        for n in range(1, args.runs + 1):
            iter_dir = out_root / pdf.stem / f"iter{n:02d}"
            print(
                f"\n[cycle{args.cycle}] {pdf.name} iter {n}/{args.runs} "
                f"-> {iter_dir}",
                flush=True,
            )
            r = run_one(
                pdf, iter_dir, llm_url=args.llm_url, timeout_s=args.timeout
            )
            per_pdf_runs[pdf.name].append(r)
            print(
                f"  rc={r['rc']} elapsed={r['elapsed_s']}s "
                f"events={r['total_events']} residuals={r['residual_count']} "
                f"pending={r['pending_count']}"
            )
            (iter_dir / "_run_summary.json").write_text(
                json.dumps(
                    {**r, "values_by_cat": {c: list(v) for c, v in r["values_by_cat"].items()}},
                    indent=2,
                ),
                encoding="utf-8",
            )

    agg = aggregate(per_pdf_runs)
    report = render_report(corpus, per_pdf_runs, agg, cycle=args.cycle)
    report_path = out_root / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n[cycle{args.cycle}] report -> {report_path}")
    print(f"corpus = {len(corpus)}, runs = {args.runs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
