"""Precision / recall benchmark against a hand-curated ground truth.

Loads ``$BENCH_ROOT/groundtruth/groundtruth.yml`` (built by reading
each PDF in the corpus three times) and, for every server profile,
runs the full pipeline on each PDF, parses
``applied_substitutions.json``, and compares the **set of distinct
``from`` values caught** against the ground-truth set per PDF.

Per (profile × PDF) we compute:
  * ``TP``, caught values that are in the ground truth.
  * ``FP``, caught values not in the ground truth.
  * ``FN``, ground-truth values that were never caught.
  * ``precision = TP / (TP + FP)``
  * ``recall    = TP / (TP + FN)``
  * ``F1       = 2 * P * R / (P + R)``

Ground-truth values are matched against caught ``from`` values
**case-insensitively** so the same brand in different cases collapses
to a single identity (avoids penalising a profile for case-folding
correctly).

Configuration via environment variables:

* ``BENCH_ROOT`` (default ``/tmp/anonbench``): root for ground truth
  and per-profile output.
* ``BENCH_CORPUS_ROOT`` (required): directory containing the PDFs
  named in the ground-truth file.

Run: ``python bench/run_precision_benchmark.py --profiles default``
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "anonymize-dossier"
PYTHON = str(REPO / ".venv" / "bin" / "python")


def _gpu_mem_used_mb() -> int:
    """Read total used VRAM across all GPUs from ``nvidia-smi``.

    Returns ``-1`` when the tool is unavailable (no NVIDIA GPU, or
    driver missing). Used by the benchmark to report the peak VRAM
    occupied by llama-server while serving requests.
    """
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=4,
        )
    except Exception:
        return -1
    used = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            used += int(line)
        except ValueError:
            pass
    return used


def _proc_rss_mb(pid: int) -> int:
    """Resident-set size (MB) of ``pid`` via /proc, or ``-1`` if the
    process has already exited."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) // 1024  # kB → MB
    except Exception:
        pass
    return -1


def _peak_memory_sample(server_pid: int | None) -> tuple[int, int]:
    """Return ``(vram_used_mb, rss_mb)`` snapshot of the running
    llama-server process. ``-1`` for either when unavailable.
    """
    return (
        _gpu_mem_used_mb(),
        _proc_rss_mb(server_pid) if server_pid else -1,
    )

BENCH_ROOT = Path(os.environ.get("BENCH_ROOT", "/tmp/anonbench"))
GT_PATH = BENCH_ROOT / "groundtruth" / "groundtruth.yml"


def _resolve_corpus_root() -> Path:
    """Corpus directory holding the PDFs referenced by the ground
    truth. Required to be set via ``BENCH_CORPUS_ROOT`` so we never
    embed a path tied to one developer's filesystem."""
    raw = os.environ.get("BENCH_CORPUS_ROOT", "").strip()
    if not raw:
        raise SystemExit(
            "BENCH_CORPUS_ROOT is not set, point it at the directory "
            "containing the PDFs named in groundtruth.yml.\n"
            "Example: BENCH_CORPUS_ROOT=~/datasets/pentest-pdfs"
        )
    return Path(raw).expanduser()


CORPUS = _resolve_corpus_root() if os.environ.get("BENCH_CORPUS_ROOT") else None  # type: ignore[assignment]


def load_ground_truth(path: Path) -> dict[str, set[str]]:
    """Return ``{pdf_name: {value, …}}`` flattened across categories,
    lower-cased."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for pdf_name, cats in (data.get("pdfs") or {}).items():
        bag: set[str] = set()
        for cat, values in (cats or {}).items():
            for v in values or []:
                if v:
                    bag.add(str(v).lower())
        out[pdf_name] = bag
    return out


def run_one(
    pdf: Path,
    out_dir: Path,
    *,
    llm_url: str,
    llm_model: str,
    timeout_s: int,
    chunk_strategy: str = "",
) -> tuple[int, set[str], list[dict]]:
    """Run the pipeline on a PDF; return (rc, caught_from_values_lower,
    raw_events)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    map_path = out_dir / "substitution_map.yml"
    pending = out_dir / "needs_review.yml"
    auto_t0 = out_dir / "auto_promoted_t0.yml"
    auto_t1 = out_dir / "auto_promoted_t1.yml"
    applied = out_dir / "applied_substitutions.json"
    report = out_dir / "verifier_report.md"
    decisions = out_dir / "decisions_history.jsonl"

    cmd = [
        PYTHON, str(CLI), "all", str(pdf),
        "-o", str(out_dir),
        "--map", str(map_path),
        "--pending", str(pending),
        "--auto-t0", str(auto_t0),
        "--auto-t1", str(auto_t1),
        "--applied", str(applied),
        "--report", str(report),
        "--decisions", str(decisions),
        "--llm-url", llm_url,
        "--llm-model", llm_model,
        "--force-rescan",
    ]
    if chunk_strategy:
        cmd.extend(["--chunk-strategy", chunk_strategy])
    rc = -1
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            cwd=str(REPO),
            env={"PYTHONPATH": str(REPO), **os.environ},
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -9

    caught: set[str] = set()
    events: list[dict] = []
    if applied.exists():
        try:
            data = json.loads(applied.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for f in data.get("files") or []:
            for ev in f.get("events") or []:
                v = (ev.get("from") or "").strip()
                if v:
                    caught.add(v.lower())
                    events.append({
                        "from": v,
                        "to": ev.get("to") or "",
                        "category": ev.get("category") or "",
                    })
    return rc, caught, events


def score(gt: set[str], caught: set[str]) -> dict:
    tp = len(gt & caught)
    fp = len(caught - gt)
    fn = len(gt - caught)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) else 0.0
    )
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "missed": sorted(gt - caught),
        "extra": sorted(caught - gt),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--profiles", nargs="+", required=True,
        help="server profile names to benchmark in order",
    )
    ap.add_argument(
        "--out-root", type=Path,
        default=BENCH_ROOT / "precision",
    )
    ap.add_argument(
        "--per-pdf-timeout", type=int, default=900,
    )
    ap.add_argument(
        "--start-timeout", type=int, default=900,
    )
    ap.add_argument(
        "--chunk-strategy", default="structured",
        choices=("structured", "flat"),
    )
    args = ap.parse_args(argv)

    args.out_root.mkdir(parents=True, exist_ok=True)
    corpus = _resolve_corpus_root()
    gt = load_ground_truth(GT_PATH)
    pdfs = [corpus / name for name in gt.keys() if (corpus / name).exists()]
    if not pdfs:
        print(f"[fatal] no GT PDFs found under {corpus}", file=sys.stderr)
        return 2
    print(f"corpus: {len(pdfs)} PDF(s); profiles: {args.profiles}")

    from anonymize.server_manager import ServerManager
    from anonymize.server_profile import load_profiles

    all_profiles = {p.name: p for p in load_profiles()}

    # Allow ad-hoc profiles from run_model_benchmark.py too.
    try:
        from bench.run_model_benchmark import EXTRA_PROFILES, _build_extra_profile
        extras_by_name = {spec["name"]: spec for spec in EXTRA_PROFILES}
        base_profile = all_profiles.get("default")
    except Exception:
        extras_by_name = {}
        base_profile = None

    results: dict[str, dict] = {}
    for prof_name in args.profiles:
        prof = all_profiles.get(prof_name)
        if prof is None and prof_name in extras_by_name and base_profile:
            spec = extras_by_name[prof_name]
            if Path(spec["model"]).exists():
                prof = _build_extra_profile(spec, base_profile)
        if prof is None or not prof.is_model_present():
            print(
                f"[skip] profile '{prof_name}' has no resolvable model"
            )
            continue

        prof_out = args.out_root / prof_name
        prof_out.mkdir(parents=True, exist_ok=True)

        print(f"\n==== profile: {prof_name} ====")
        print(f"  model = {prof.model_path}")

        mgr = ServerManager(prof)
        try:
            mgr.stop(timeout=5.0)
        except Exception:
            pass
        t0 = time.time()
        try:
            ok = mgr.start(wait_seconds=args.start_timeout)
        except Exception as e:
            print(f"  start exception: {e}")
            ok = False
        startup_s = round(time.time() - t0, 1)
        if not ok:
            print(f"  [FAIL] not ready in {args.start_timeout}s, skip")
            results[prof_name] = {
                "started": False, "startup_s": startup_s, "per_pdf": {},
            }
            continue
        print(f"  ready in {startup_s}s")

        # Take a baseline VRAM/RSS snapshot RIGHT after the model has
        # loaded but before any request runs.  Subsequent samples
        # during the per-PDF loop give us the peak occupied during
        # actual inference.
        server_pid = getattr(mgr, "pid", None) or getattr(mgr, "_pid", None)
        idle_vram_mb, idle_rss_mb = _peak_memory_sample(server_pid)
        peak_vram_mb = idle_vram_mb
        peak_rss_mb = idle_rss_mb

        per_pdf: dict[str, dict] = {}
        for pdf in pdfs:
            run_dir = prof_out / pdf.stem
            shutil.rmtree(run_dir, ignore_errors=True)
            print(f"  · {pdf.name}", end="  ", flush=True)
            t1 = time.time()
            rc, caught, events = run_one(
                pdf, run_dir,
                llm_url=prof.base_url, llm_model=prof.name,
                timeout_s=args.per_pdf_timeout,
                chunk_strategy=args.chunk_strategy,
            )
            elapsed = round(time.time() - t1, 1)
            # Sample VRAM/RSS just after the per-PDF run completed -
            # llama.cpp doesn't free between requests so this is a
            # reliable proxy for the peak.
            v_now, r_now = _peak_memory_sample(server_pid)
            if v_now > peak_vram_mb:
                peak_vram_mb = v_now
            if r_now > peak_rss_mb:
                peak_rss_mb = r_now
            s = score(gt[pdf.name], caught)
            per_pdf[pdf.name] = {
                "rc": rc, "elapsed_s": elapsed,
                **s,
                "caught_count": len(caught),
                "gt_size": len(gt[pdf.name]),
                "events_total": len(events),
            }
            print(
                f"rc={rc:3d}  {elapsed:6.1f}s  caught={len(caught):3d}  "
                f"gt={len(gt[pdf.name]):2d}  P={s['precision']:.2f}  "
                f"R={s['recall']:.2f}  F1={s['f1']:.2f}",
                flush=True,
            )

        try:
            mgr.stop(timeout=10.0)
        except Exception:
            pass

        # Aggregate across PDFs.
        all_tp = sum(x["tp"] for x in per_pdf.values())
        all_fp = sum(x["fp"] for x in per_pdf.values())
        all_fn = sum(x["fn"] for x in per_pdf.values())
        agg = {
            "precision": (
                round(all_tp / (all_tp + all_fp), 3)
                if (all_tp + all_fp) else 0.0
            ),
            "recall": (
                round(all_tp / (all_tp + all_fn), 3)
                if (all_tp + all_fn) else 0.0
            ),
            "tp": all_tp, "fp": all_fp, "fn": all_fn,
            "elapsed_total_s": round(
                sum(x["elapsed_s"] for x in per_pdf.values()), 1
            ),
        }
        agg["f1"] = (
            round(
                2 * agg["precision"] * agg["recall"] /
                (agg["precision"] + agg["recall"]),
                3,
            ) if (agg["precision"] + agg["recall"]) else 0.0
        )
        results[prof_name] = {
            "started": True,
            "startup_s": startup_s,
            "per_pdf": per_pdf,
            "agg": agg,
            "memory": {
                "idle_vram_mb": idle_vram_mb,
                "peak_vram_mb": peak_vram_mb,
                "idle_rss_mb": idle_rss_mb,
                "peak_rss_mb": peak_rss_mb,
            },
        }
        print(
            f"  memory: idle VRAM {idle_vram_mb} MB, peak VRAM "
            f"{peak_vram_mb} MB · idle RSS {idle_rss_mb} MB, "
            f"peak RSS {peak_rss_mb} MB"
        )

    out_path = args.out_root / "report.md"
    _write_report(args, gt, results, out_path)
    # JSON sidecar so downstream tooling can parse the numbers
    # without scraping markdown.
    json_path = args.out_root / "report.json"
    json_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\nreport -> {out_path}")
    print(f"json   -> {json_path}")
    return 0


def _write_report(args, gt, results, out_path: Path) -> None:
    md: list[str] = []
    md.append("# Precision / Recall benchmark, small models\n")
    md.append(
        f"Corpus: {len(gt)} PDFs from `{CORPUS}`. Ground truth "
        f"manually curated in {GT_PATH} (3 cross-checks).\n"
    )
    md.append(
        f"Chunk strategy: ``{args.chunk_strategy}``. Each profile "
        f"is started fresh; every PDF is processed via "
        f"``anonymize-dossier all --force-rescan``. The set of "
        f"distinct ``from`` values applied (case-insensitive) is "
        f"compared against the GT set per PDF.\n"
    )

    md.append("\n## 1. Aggregate leaderboard\n")
    md.append(
        "| profile | started | load (s) | TP | FP | FN | precision | "
        "recall | F1 | total elapsed (s) | peak VRAM (MB) | peak RSS (MB) |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for prof_name in args.profiles:
        r = results.get(prof_name)
        if r is None:
            md.append(f"| {prof_name} |, |, |, |, |, |, |, |, |, |, |, |")
            continue
        if not r["started"]:
            md.append(
                f"| {prof_name} | ❌ | {r['startup_s']} |, |, |, | "
                f"- |, |, |, |, |, |"
            )
            continue
        a = r["agg"]
        mem = r.get("memory") or {}
        vram = mem.get("peak_vram_mb")
        rss = mem.get("peak_rss_mb")
        vram_s = f"{vram}" if isinstance(vram, int) and vram >= 0 else "-"
        rss_s = f"{rss}" if isinstance(rss, int) and rss >= 0 else "-"
        md.append(
            f"| {prof_name} | ✅ | {r['startup_s']} | "
            f"{a['tp']} | {a['fp']} | {a['fn']} | "
            f"{a['precision']:.2%} | {a['recall']:.2%} | "
            f"{a['f1']:.2%} | {a['elapsed_total_s']} | "
            f"{vram_s} | {rss_s} |"
        )

    md.append("\n## 2. Per-PDF breakdown\n")
    for prof_name in args.profiles:
        r = results.get(prof_name)
        if r is None or not r["started"]:
            continue
        md.append(f"\n### {prof_name}\n")
        md.append(
            "| PDF | gt | caught | TP | FP | FN | precision | recall "
            "| F1 | elapsed (s) |"
        )
        md.append(
            "|---|---|---|---|---|---|---|---|---|---|"
        )
        for pdf_name, x in r["per_pdf"].items():
            md.append(
                f"| {pdf_name} | {x['gt_size']} | {x['caught_count']} "
                f"| {x['tp']} | {x['fp']} | {x['fn']} | "
                f"{x['precision']:.2%} | {x['recall']:.2%} | "
                f"{x['f1']:.2%} | {x['elapsed_s']} |"
            )

    md.append("\n## 3. Misses (FN, ground-truth values never caught)\n")
    for prof_name in args.profiles:
        r = results.get(prof_name)
        if r is None or not r["started"]:
            continue
        for pdf_name, x in r["per_pdf"].items():
            if not x["missed"]:
                continue
            md.append(
                f"- **{prof_name}** / {pdf_name}: "
                + ", ".join(f"`{m}`" for m in x["missed"])
            )

    md.append("\n## 4. Extras (FP, caught values not in GT)\n")
    md.append(
        "These can be over-detection (the LLM flagged something we "
        "decided is generic) OR genuine leaks our GT under-specified. "
        "Eyeballing recommended.\n"
    )
    for prof_name in args.profiles:
        r = results.get(prof_name)
        if r is None or not r["started"]:
            continue
        for pdf_name, x in r["per_pdf"].items():
            if not x["extra"]:
                continue
            md.append(
                f"- **{prof_name}** / {pdf_name}: "
                + ", ".join(f"`{e}`" for e in x["extra"][:25])
                + (
                    f" *(+{len(x['extra'])-25} more)*"
                    if len(x["extra"]) > 25 else ""
                )
            )

    out_path.write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
