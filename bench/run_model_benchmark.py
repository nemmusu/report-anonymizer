"""Per-model benchmark harness.

Iterates over a list of server profiles (presets), starts
``llama-server`` headlessly with each one, runs the full
anonymization pipeline on every PDF in the configured corpus
directory, then stops the server and moves on.

Captures per-run elapsed time + total events caught + verifier
residuals + errors. Aggregates into a comparison Markdown report.

Usage::

    .venv/bin/python bench/run_model_benchmark.py
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
from typing import Optional

import yaml


REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "anonymize-dossier"
PYTHON = str(REPO / ".venv" / "bin" / "python")

# Default benchmark corpus (override with --corpus-dir or
# ``BENCH_CORPUS_ROOT``). Falls back to the empty path so a fresh
# checkout fails fast with a clear error rather than crashing on a
# stranger's filesystem layout.
CORPUS_DEFAULT = Path(
    os.environ.get("BENCH_CORPUS_ROOT", "")
).expanduser() if os.environ.get("BENCH_CORPUS_ROOT") else Path("")

# Profiles defined in ``config/server_profiles.yml`` (or user-saved
# ones). Restricted to entries whose model file is already on disk
# under ``MODELS_DIR``.
BUILTIN_PROFILES = (
    "default",
    "ministral-3-8b-reasoning-bf16",
    "qwen3.5-9b-bf16",
    "ministral-3-8b-bf16",
    "qwen2.5-coder-7b-f16",
)

# Ad-hoc profiles list, used to be where we plugged GGUFs that
# weren't part of the curated preset set. After the BF16-only
# cleanup the curated catalog covers everything we benchmark, so
# this stays empty by default.  Add entries here only for one-off
# A/B comparisons; they're synthesized in-memory via
# ``_build_extra_profile``.
# If you maintain a private collection of GGUFs outside MODELS_DIR
# and want the harness to A/B them against the curated presets,
# point ``BENCH_EXTRA_MODEL_ROOT`` at the directory and add entries
# to ``EXTRA_PROFILES``. Empty by default so a fresh checkout has
# nothing to chase.
EXTRA_MODEL_ROOT = Path(
    os.environ.get("BENCH_EXTRA_MODEL_ROOT", "")
).expanduser() if os.environ.get("BENCH_EXTRA_MODEL_ROOT") else Path("")
EXTRA_PROFILES: tuple[dict, ...] = ()


def _build_extra_profile(spec: dict, base: "ServerProfile") -> "ServerProfile":
    """Clone ``base`` (typically the default builtin) and override the
    fields needed to point at one of the EXTRA_PROFILES entries."""
    p = base.clone(name=spec["name"])
    p.model = str(spec["model"])
    p.model_repo = ""
    p.model_filename = ""
    mm_raw = spec.get("mmproj")
    # An empty/blank Path("") would resolve to "." and confuse
    # llama-server. Treat any falsy / empty / non-existent mmproj
    # as "no mmproj at all".
    mm_str = str(mm_raw) if mm_raw else ""
    if mm_str in ("", ".") or not Path(mm_str).exists():
        p.mmproj = ""
    else:
        p.mmproj = mm_str
    p.mmproj_repo = ""
    p.mmproj_filename = ""
    p.ctx_size = int(spec["ctx_size"])
    p.parallel = int(spec["parallel"])
    p.n_gpu_layers = int(spec["n_gpu_layers"])
    p.is_builtin = False
    p.source = "user"
    return p


# All profile names to bench in order, builtins first (smaller),
# then custom in increasing order of cost.
DEFAULT_PROFILES = (
    *BUILTIN_PROFILES,
    *(spec["name"] for spec in EXTRA_PROFILES),
)


_HIT_ROW = re.compile(r"^\|\s*(?P<file>[^|]+)\|\s*(?P<pat>[^|]+)\|\s*(?P<match>[^|]+)\|")


def parse_applied(applied_path: Path) -> dict:
    if not applied_path.exists():
        return {
            "events_by_cat": {},
            "total_events": 0,
            "values": set(),
            "values_by_cat": {},
        }
    data = json.loads(applied_path.read_text(encoding="utf-8"))
    events_by_cat: dict[str, int] = defaultdict(int)
    values: set[str] = set()
    values_by_cat: dict[str, set[str]] = defaultdict(set)
    total = 0
    for f in data.get("files") or []:
        for ev in f.get("events") or []:
            cat = ev.get("category") or "other"
            events_by_cat[cat] += 1
            total += 1
            v = ev.get("from") or ""
            if v:
                values.add(v)
                values_by_cat[cat].add(v)
    return {
        "events_by_cat": dict(events_by_cat),
        "total_events": total,
        "values": values,
        "values_by_cat": {c: vs for c, vs in values_by_cat.items()},
    }


_PHASE_MARKERS = {
    "rules": re.compile(r"\[rules\] +100%"),
    "detect": re.compile(r"\[LLM\] +100%"),
    "apply": re.compile(r"\[apply\] +100%"),
    "build": re.compile(r"\[build\] +100%"),
    "verify": re.compile(r"\[verifier?\] +100%|Verifier: "),
    "auto_resolve": re.compile(r"Auto-resolve: "),
}


def _split_stages_from_stdout(stdout: str) -> dict[str, bool]:
    """Best-effort: just record which phases the CLI reached. The
    CLI doesn't print absolute timestamps in milliseconds, so per-stage
    duration would need a more invasive change. The booleans are still
    useful to spot e.g. profiles that fail before reaching apply."""
    out: dict[str, bool] = {}
    for k, pat in _PHASE_MARKERS.items():
        out[k] = bool(pat.search(stdout))
    return out


def parse_verifier_report(report_path: Path) -> list[dict]:
    if not report_path.exists():
        return []
    out: list[dict] = []
    in_table = False
    for line in report_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            continue
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


def run_pipeline_on(
    pdf: Path,
    out_dir: Path,
    *,
    llm_url: str,
    llm_model: str,
    timeout_s: int,
    chunk_strategy: str = "",
) -> dict:
    """Run ``bin/anonymize-dossier all`` on a single PDF, return per-run
    summary dict."""
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
        "--llm-model",
        llm_model,
        "--force-rescan",
    ]
    if chunk_strategy:
        cmd.extend(["--chunk-strategy", chunk_strategy])
    t0 = time.time()
    stdout_text = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(REPO),
            env={"PYTHONPATH": str(REPO), **os.environ},
        )
        rc = proc.returncode
        stdout_text = proc.stdout or ""
        stderr_tail = (proc.stderr or "")[-1500:]
    except subprocess.TimeoutExpired:
        rc = -9
        stderr_tail = "[TIMEOUT]"
    elapsed = time.time() - t0

    applied_summary = parse_applied(applied)
    hits = parse_verifier_report(report)
    phases = _split_stages_from_stdout(stdout_text)
    return {
        "pdf": pdf.name,
        "rc": rc,
        "elapsed_s": round(elapsed, 2),
        "events_by_cat": applied_summary["events_by_cat"],
        "total_events": applied_summary["total_events"],
        "values": applied_summary["values"],
        "values_by_cat": applied_summary["values_by_cat"],
        "residual_count": len(hits),
        "residuals": hits,
        "phases_completed": phases,
        "stderr_tail": stderr_tail,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus-dir", type=Path, default=CORPUS_DEFAULT,
        help=f"directory of PDFs to benchmark (default: {CORPUS_DEFAULT})",
    )
    ap.add_argument(
        "--profiles", nargs="*", default=list(DEFAULT_PROFILES),
        help="server profile names to benchmark in order",
    )
    ap.add_argument(
        "--out-root", type=Path,
        default=Path("/tmp/anonbench/models"),
    )
    ap.add_argument(
        "--per-pdf-timeout", type=int, default=900,
        help="seconds to wait for one PDF run before killing it",
    )
    ap.add_argument(
        "--start-timeout", type=int, default=900,
        help="seconds to wait for llama-server ready after start",
    )
    ap.add_argument(
        "--runs-per-pdf", type=int, default=2,
        help=(
            "how many times to anonymize the same PDF per profile, to "
            "measure variance from LLM stochasticity. Default 2."
        ),
    )
    ap.add_argument(
        "--chunk-strategy", default="", choices=("", "structured", "flat"),
        help=(
            "Pass a fixed chunker strategy through to the CLI. Empty "
            "(default) uses whatever Project.chunk_strategy resolves to."
        ),
    )
    args = ap.parse_args(argv)

    pdfs = sorted(args.corpus_dir.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs found under {args.corpus_dir}", file=sys.stderr)
        return 2
    args.out_root.mkdir(parents=True, exist_ok=True)

    # Lazy import so the rest of the module is unit-test friendly.
    from anonymize.server_manager import ServerManager
    from anonymize.server_profile import get_profile, load_profiles

    all_profiles = {p.name: p for p in load_profiles()}
    extras_by_name = {spec["name"]: spec for spec in EXTRA_PROFILES}

    # Resolve a base profile we can clone for the synthesized extras.
    base_profile = all_profiles.get("default")

    results: dict[str, dict] = {}

    for prof_name in args.profiles:
        prof = all_profiles.get(prof_name)
        if prof is None and prof_name in extras_by_name and base_profile is not None:
            spec = extras_by_name[prof_name]
            if not Path(spec["model"]).exists():
                print(
                    f"profile '{prof_name}' model not at {spec['model']} "
                    f"- skipping",
                    flush=True,
                )
                continue
            prof = _build_extra_profile(spec, base_profile)
        if prof is None:
            print(f"profile '{prof_name}' not found, skipping", flush=True)
            continue
        if not prof.is_model_present():
            print(
                f"profile '{prof_name}' model not present on disk "
                f"({prof.model_path}), skipping (download via "
                f"GUI Model Manager first)",
                flush=True,
            )
            continue

        prof_out = args.out_root / prof_name
        prof_out.mkdir(parents=True, exist_ok=True)

        print(f"\n==== profile: {prof_name} ====", flush=True)
        print(
            f"     binary  = {prof.binary}\n"
            f"     model   = {prof.model_path}\n"
            f"     mmproj  = {prof.mmproj_path}\n"
            f"     ctx     = {prof.ctx_size}\n"
            f"     parallel= {prof.parallel}\n"
            f"     gpu_lay = {prof.n_gpu_layers}",
            flush=True,
        )

        mgr = ServerManager(prof)
        # Be sure nothing else is on the port.
        try:
            mgr.stop(timeout=5.0)
        except Exception:
            pass

        t_start = time.time()
        try:
            ok = mgr.start(wait_seconds=args.start_timeout)
        except Exception as e:
            ok = False
            print(f"     start exception: {e}", flush=True)
        startup_s = round(time.time() - t_start, 1)
        if not ok:
            print(
                f"     [FAIL] llama-server did not become ready in "
                f"{args.start_timeout}s, skipping",
                flush=True,
            )
            results[prof_name] = {
                "started": False,
                "startup_s": startup_s,
                "runs": [],
            }
            continue
        print(f"     ready in {startup_s}s", flush=True)

        runs: list[dict] = []
        for pdf in pdfs:
            for trial in range(1, args.runs_per_pdf + 1):
                run_dir = prof_out / pdf.stem / f"run{trial}"
                shutil.rmtree(run_dir, ignore_errors=True)
                print(
                    f"     · {pdf.name} run{trial}/{args.runs_per_pdf}",
                    end="  ", flush=True,
                )
                r = run_pipeline_on(
                    pdf,
                    run_dir,
                    llm_url=prof.base_url,
                    llm_model=prof.name,
                    timeout_s=args.per_pdf_timeout,
                    chunk_strategy=args.chunk_strategy,
                )
                r["trial"] = trial
                runs.append(r)
                print(
                    f"rc={r['rc']:3d}  {r['elapsed_s']:6.1f}s  "
                    f"events={r['total_events']:3d}  "
                    f"residuals={r['residual_count']}",
                    flush=True,
                )

        try:
            mgr.stop(timeout=10.0)
        except Exception as e:
            print(f"     stop exception (ignored): {e}", flush=True)

        results[prof_name] = {
            "started": True,
            "startup_s": startup_s,
            "runs": runs,
        }

    # ---- aggregate report --------------------------------------------------
    out_path = args.out_root / "report.md"
    _write_report(args, pdfs, results, out_path)
    print(f"\nreport -> {out_path}", flush=True)
    return 0


def _stat(values: list[float]) -> dict:
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "stdev": 0.0, "n": 0}
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return {
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "mean": round(mean, 2),
        "stdev": round(var ** 0.5, 2),
        "n": n,
    }


def _write_report(args, pdfs, results: dict[str, dict], out_path: Path) -> None:
    md: list[str] = []
    md.append("# Per-model benchmark, full report\n")
    md.append(
        f"Corpus: `{args.corpus_dir}` · {len(pdfs)} PDF(s) · "
        f"{args.runs_per_pdf} run(s)/PDF · "
        f"{len(args.profiles)} profile(s) attempted.\n"
    )
    md.append(
        "Each profile is started fresh (llama-server is killed and "
        "respawned), then every PDF is processed via "
        "``anonymize-dossier all --force-rescan`` with isolated "
        "per-PDF state. The same prompt files + pipeline code path "
        "are used across profiles, so differences in catch-rate / "
        "speed come from the LLM only.\n"
    )

    # ---- per-profile aggregate ----
    md.append("\n## 1. Profile leaderboard\n")
    md.append(
        "| profile | started | load (s) | runs OK | total events "
        "| dist. values | residuals | elapsed total (s) | "
        "elapsed mean / PDF (s) | elapsed stdev (s) | events/sec |"
    )
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    profile_dist_values: dict[str, set[str]] = {}
    profile_runs: dict[str, list[dict]] = {}
    for prof_name in args.profiles:
        r = results.get(prof_name)
        if r is None:
            md.append(f"| {prof_name} | skipped |, |, |, |, |, |, |, |, |, |")
            continue
        if not r["started"]:
            md.append(
                f"| {prof_name} | ❌ | {r['startup_s']} | 0 |, |, |, |, |, |, |, |"
            )
            continue
        runs = r["runs"]
        profile_runs[prof_name] = runs
        ok_runs = [x for x in runs if x["rc"] == 0]
        total_events = sum(x["total_events"] for x in runs)
        total_residuals = sum(x["residual_count"] for x in runs)
        elapsed_list = [x["elapsed_s"] for x in runs]
        elapsed_total = sum(elapsed_list)
        s = _stat(elapsed_list)
        dist_values: set[str] = set()
        for x in runs:
            dist_values |= x.get("values", set())
        profile_dist_values[prof_name] = dist_values
        ev_per_sec = (
            round(total_events / elapsed_total, 2) if elapsed_total else 0.0
        )
        md.append(
            f"| {prof_name} | ✅ | {r['startup_s']} | {len(ok_runs)}/{len(runs)} "
            f"| {total_events} | {len(dist_values)} | {total_residuals} "
            f"| {round(elapsed_total, 1)} | {s['mean']} | {s['stdev']} "
            f"| {ev_per_sec} |"
        )

    # ---- coverage union ----
    union_per_pdf: dict[str, set[str]] = defaultdict(set)
    for prof_name, runs in profile_runs.items():
        for x in runs:
            union_per_pdf[x["pdf"]] |= x.get("values", set())
    union_total = set()
    for vs in union_per_pdf.values():
        union_total |= vs

    md.append("\n## 2. Coverage vs. union\n")
    md.append(
        "The *union* is the set of distinct values caught by **any** "
        "profile on each PDF, a proxy for ground truth. A profile's "
        "coverage is what % of that union it caught.\n"
    )
    md.append(
        f"Across the corpus the union has **{len(union_total)} "
        f"distinct values** (all profiles combined).\n"
    )
    md.append("| profile | distinct caught | coverage vs union |")
    md.append("|---|---|---|")
    for prof_name in args.profiles:
        if prof_name not in profile_runs:
            continue
        caught = len(profile_dist_values.get(prof_name, set()))
        cov = (
            round(100.0 * caught / max(len(union_total), 1), 1)
            if union_total else 0.0
        )
        md.append(f"| {prof_name} | {caught} | {cov}% |")

    # ---- per-category coverage ----
    md.append("\n## 3. Per-category coverage (distinct values)\n")
    cats_seen: set[str] = set()
    per_profile_cat_values: dict[str, dict[str, set[str]]] = {}
    for prof_name, runs in profile_runs.items():
        per_cat: dict[str, set[str]] = defaultdict(set)
        for x in runs:
            for c, vs in x.get("values_by_cat", {}).items():
                per_cat[c] |= vs
                cats_seen.add(c)
        per_profile_cat_values[prof_name] = per_cat
    cats = sorted(cats_seen)
    if cats:
        header = "| profile | " + " | ".join(cats) + " |"
        sep = "|---|" + "|".join("---" for _ in cats) + "|"
        md.append(header)
        md.append(sep)
        for prof_name in args.profiles:
            if prof_name not in per_profile_cat_values:
                continue
            row = [prof_name]
            for c in cats:
                row.append(str(len(per_profile_cat_values[prof_name].get(c, set()))))
            md.append("| " + " | ".join(row) + " |")

    # ---- per-PDF detail (mean / range across runs) ----
    md.append("\n## 4. Per-PDF detail (variance across runs)\n")
    for prof_name in args.profiles:
        runs = profile_runs.get(prof_name)
        if not runs:
            continue
        md.append(f"\n### {prof_name}\n")
        md.append(
            "| PDF | runs | rc | elapsed (s) min/mean/max | events min/mean/max | residuals total |"
        )
        md.append("|---|---|---|---|---|---|")
        per_pdf: dict[str, list[dict]] = defaultdict(list)
        for x in runs:
            per_pdf[x["pdf"]].append(x)
        for pdf_name, xs in per_pdf.items():
            elapsed = [x["elapsed_s"] for x in xs]
            events = [x["total_events"] for x in xs]
            residuals = sum(x["residual_count"] for x in xs)
            rcs = [str(x["rc"]) for x in xs]
            es = _stat(elapsed)
            ev = _stat([float(v) for v in events])
            md.append(
                f"| {pdf_name} | {len(xs)} | {','.join(rcs)} "
                f"| {es['min']}/{es['mean']}/{es['max']} "
                f"| {int(ev['min'])}/{int(ev['mean'])}/{int(ev['max'])} "
                f"| {residuals} |"
            )

    # ---- reliability ----
    md.append("\n## 5. Reliability\n")
    md.append(
        "| profile | rc=0 runs | rc≠0 runs | timeouts | runs with residuals |"
    )
    md.append("|---|---|---|---|---|")
    for prof_name in args.profiles:
        runs = profile_runs.get(prof_name)
        if not runs:
            continue
        ok = sum(1 for x in runs if x["rc"] == 0)
        bad = sum(1 for x in runs if x["rc"] != 0)
        timeouts = sum(1 for x in runs if x["rc"] == -9)
        with_residuals = sum(1 for x in runs if x["residual_count"] > 0)
        md.append(
            f"| {prof_name} | {ok} | {bad} | {timeouts} | {with_residuals} |"
        )

    # ---- errors ----
    md.append("\n## 6. Errors\n")
    any_err = False
    for prof_name in args.profiles:
        runs = profile_runs.get(prof_name)
        if not runs:
            continue
        for x in runs:
            if x["rc"] != 0:
                any_err = True
                md.append(
                    f"- **{prof_name}** / {x['pdf']} (run{x.get('trial')}): "
                    f"rc={x['rc']}\n  ```\n  "
                    f"{x.get('stderr_tail','')[-500:].strip()}\n  ```"
                )
    if not any_err:
        md.append("(none)")

    out_path.write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
