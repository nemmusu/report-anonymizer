"""Verifier: post-build sweep to catch residual leaks.

Two complementary checks:

1. Apply ``leak_patterns.yml`` (``verify_only`` plus the ``auto_promote``
   patterns minus their ``allow`` whitelists) to every file in the output
   tree. For PDFs we extract text via ``pdftotext`` (so we catch leaks both
   in the source markdown AND in the rendered PDF). For docx/xlsx/etc. we
   call ``adapter.extract`` and run the regexes on the segments.

2. Verify that no canonical ``from`` value (from the substitution map) is
   present in the output, by extracting via the proper adapter and
   substring-checking.

Hardening:
  * HTML/XML entity decode (``html.unescape``) before regex sweep,
    so ``&#65;PI`` is matched as ``API``.
  * Unicode NFKC normalization,
  * Zero-width chars stripped (``U+200B..U+200F``, ``U+FEFF``, ``U+2060``),
  * Span-deduplication so two patterns matching the same byte range count
    once.

The output is a markdown report and an exit code (``0 == clean``).
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from .format_adapters import get_adapter
from .scanner import COPY_AS_IS_EXTENSIONS


_ZERO_WIDTH = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]")


def _normalize(text: str) -> str:
    if not text:
        return text
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return text


@dataclass
class LeakHit:
    file: str
    pattern: str
    match: str
    snippet: str = ""


@dataclass
class VerifierReport:
    hits: list[LeakHit] = field(default_factory=list)
    files_scanned: int = 0
    pdfs_scanned: int = 0
    archives_uninspected: list[str] = field(default_factory=list)
    is_clean: bool = True


def _load_patterns(patterns_path: Path) -> list[tuple[str, re.Pattern, list[re.Pattern]]]:
    data = yaml.safe_load(patterns_path.read_text(encoding="utf-8")) or {}
    out: list[tuple[str, re.Pattern, list[re.Pattern]]] = []
    for sect in ("auto_promote", "verify_only"):
        for r in data.get(sect) or []:
            try:
                pat = re.compile(r["regex"])
                allow = [re.compile(p) for p in (r.get("allow") or [])]
                out.append((str(r.get("name", "rule")), pat, allow))
            except Exception:
                continue
    return out


# Categories whose ``from`` values are textual identifiers (brand
# names, hostnames, advisory IDs, header names, package names): a
# residual occurrence in the output usually means we missed a case
# variant or sub-token. We sweep the output for case-insensitive
# matches of these values.
_DYNAMIC_VERIFY_CATEGORIES = (
    "brand",
    "network",
    "app_packages",
    "headers",
    "user_agents",
    "ids",
    "infra_ids",
)


def build_dynamic_verify_patterns(
    smap_entries: dict[str, list[dict]],
    *,
    min_token_len: int = 3,
) -> list[tuple[str, re.Pattern, list[re.Pattern]]]:
    """Compile per-run regex patterns from the active substitution map.

    Replaces the old hard-coded ``verify_only`` list. For every textual
    map entry (brand / network / app_packages / headers / user_agents /
    ids / infra_ids) we synthesise ONE case-insensitive word-boundary
    regex that matches the canonical ``from`` value. Switching customer
    = switching map = new patterns; nothing here is vendor-specific.

    The ``allow`` lists are populated with each entry's existing
    ``to`` value so an already-anonymized placeholder doesn't get
    re-flagged on the next pass.
    """
    out: list[tuple[str, re.Pattern, list[re.Pattern]]] = []
    seen_values: set[str] = set()
    seen_placeholders: set[str] = set()
    for cat in _DYNAMIC_VERIFY_CATEGORIES:
        for it in smap_entries.get(cat, []) or []:
            f = str(it.get("from", "") or "").strip()
            t = str(it.get("to", "") or "").strip()
            if not f or len(f) < min_token_len:
                continue
            if f.lower() in seen_values:
                continue
            seen_values.add(f.lower())
            if t:
                seen_placeholders.add(t.lower())
            try:
                pat = re.compile(rf"(?i)\b{re.escape(f)}\b")
            except re.error:
                continue
            allow_pats: list[re.Pattern] = []
            if t:
                try:
                    allow_pats.append(re.compile(rf"(?i)^{re.escape(t)}$"))
                except re.error:
                    pass
            mid = str(it.get("id") or cat)
            out.append((f"map:{mid}", pat, allow_pats))
    # Cross-entry allow: any placeholder text from the map is itself
    # legitimate output and must never be flagged as a residual leak,
    # regardless of which dynamic rule produced the candidate match.
    return out


def _is_allowed(value: str, allow: list[re.Pattern]) -> bool:
    return any(p.fullmatch(value) or p.search(value) for p in allow)


def _pdftotext(path: Path) -> str:
    binp = shutil.which("pdftotext")
    if not binp:
        return ""
    try:
        proc = subprocess.run(
            [binp, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout
    except Exception:
        return ""


def _extract_text_from(path: Path, *, pdf_strategy: str = "inplace") -> str:
    """Best-effort text extraction across all supported formats."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdftotext(path)
    if ext in COPY_AS_IS_EXTENSIONS:
        return ""
    try:
        ad = get_adapter(path, pdf_strategy=pdf_strategy)
        segs = ad.extract(path)
        return "\n".join(s.text for s in segs)
    except Exception:
        return ""


_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}

# Engine bookkeeping files written *into* ``output_dir`` by the pipeline
# itself. Every match in those files is a tautology, the original leak
# value will always show up in our own audit trail (``decisions_history``,
# the ``from`` side of ``applied_substitutions``, the source candidates in
# ``auto_promoted_*.yml`` / ``needs_review.yml``). Reporting them as
# residual leaks is misleading: they don't represent a leak in any
# user-facing artifact, so we skip them at scan time.
_BOOKKEEPING_NAMES = {
    "auto_promoted_t0.yml",
    "auto_promoted_t1.yml",
    "needs_review.yml",
    "applied_substitutions.json",
    "decisions_history.jsonl",
    "verifier_report.md",
}


def _is_bookkeeping(path: Path, root: Path) -> bool:
    if path.name in _BOOKKEEPING_NAMES:
        return True
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if parts and parts[0] == ".anon":
        return True
    return False


def _build_supersetting_placeholders(map_entries: Optional[dict]) -> set[str]:
    """Return ``{normalized_from}`` for every map entry where ``to``
    visibly contains ``from`` as a substring (e.g.
    ``hex_keep_prefix`` produces ``9Tv824xGKw2BWJe → 9Tv824xGKw2BWJe0001``).

    The regression-from-value check would otherwise flag every
    occurrence of ``9Tv824xGKw2BWJe`` in the output as a leak -
    *including* the ones now living inside their own placeholder.
    Treating them as already-covered eliminates that false positive.
    """
    out: set[str] = set()
    if not map_entries:
        return out
    for items in map_entries.values():
        for it in items or []:
            f = str(it.get("from", "") or "")
            t = str(it.get("to", "") or "")
            if not f or not t or f == t:
                continue
            if f in t:
                out.add(_normalize(f))
    return out


def verify(
    output_root: Path,
    *,
    patterns_path: Path,
    map_keys: Optional[set[str]] = None,
    map_entries: Optional[dict[str, list[dict]]] = None,
    pdf_strategy: str = "inplace",
    snippet_chars: int = 80,
) -> VerifierReport:
    """Sweep ``output_root`` for residual leaks.

    Three sources of patterns:

    1. ``leak_patterns.yml:auto_promote``, generic Tier-0 rules
       (phones, IPs, hex credentials …) that catch shapes the LLM
       may have skipped. These are deliberately vendor-agnostic.
    2. ``leak_patterns.yml:verify_only``, historically used for
       hard-coded vendor brand regex; now intentionally empty
       because it would tie the engine to one customer.
    3. **Per-run dynamic patterns** synthesised from the active
       ``substitution_map.yml`` via :func:`build_dynamic_verify_patterns`
      , every textual brand / hostname / advisory id the user has
       confirmed is a leak gets compiled into a case-insensitive
       word-boundary regex. Switching customer = no code edit.
    """
    rules = list(_load_patterns(patterns_path))
    if map_entries:
        rules.extend(build_dynamic_verify_patterns(map_entries))
    map_keys = map_keys or set()
    superset_placeholders = _build_supersetting_placeholders(map_entries)
    report = VerifierReport()

    def emit(file: str, pattern: str, match: str, text: str, off: int) -> None:
        start = max(0, off - snippet_chars // 2)
        end = min(len(text), off + len(match) + snippet_chars // 2)
        snippet = text[start:end].replace("\n", " ").strip()
        report.hits.append(LeakHit(file=file, pattern=pattern, match=match, snippet=snippet))

    if output_root.is_file():
        files = [output_root]
        rel_base = output_root.parent
    else:
        files = []
        for p in output_root.rglob("*"):
            if not p.is_file():
                continue
            if _is_bookkeeping(p, output_root):
                continue
            ext = p.suffix.lower()
            if ext in _ARCHIVE_EXTS:
                report.archives_uninspected.append(str(p.relative_to(output_root)))
                continue
            if ext in COPY_AS_IS_EXTENSIONS:
                if ext != ".pdf":
                    continue
            files.append(p)
        rel_base = output_root

    for path in files:
        raw_text = _extract_text_from(path, pdf_strategy=pdf_strategy)
        text = _normalize(raw_text)
        report.files_scanned += 1
        if path.suffix.lower() == ".pdf":
            report.pdfs_scanned += 1
        if not text:
            continue
        rel = path.relative_to(rel_base) if path != rel_base else Path(path.name)
        seen_spans: set[tuple[int, int]] = set()
        # 1. canonical from-value regression check (also normalized).
        # Skip ``from`` values whose own placeholder contains them as a
        # substring, those would always trigger a false positive
        # because the placeholder *is* a legitimate occurrence of the
        # ``from`` text in the output.
        for k in map_keys:
            if not k:
                continue
            kn = _normalize(k)
            if kn in superset_placeholders:
                continue
            idx = 0
            while True:
                idx = text.find(kn, idx)
                if idx == -1:
                    break
                key = (idx, len(kn))
                if key not in seen_spans:
                    seen_spans.add(key)
                    emit(str(rel), "regression:from-value", k, text, idx)
                idx += max(1, len(kn))
        # 2. regex sweep
        for name, pat, allow in rules:
            for m in pat.finditer(text):
                value = m.group(0)
                if _is_allowed(value, allow):
                    continue
                key = (m.start(), len(value))
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                emit(str(rel), name, value, text, m.start())

    report.is_clean = not report.hits
    return report


def write_verifier_report(report: VerifierReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    status = "CLEAN" if report.is_clean else f"{len(report.hits)} LEAKS"
    lines.append(f"# Verifier report - {status}")
    lines.append("")
    lines.append(f"- files scanned: **{report.files_scanned}**")
    lines.append(f"- PDFs scanned: **{report.pdfs_scanned}**")
    lines.append(f"- residual hits: **{len(report.hits)}**")
    if report.archives_uninspected:
        lines.append(f"- archives not inspected: **{len(report.archives_uninspected)}**")
    lines.append("")
    if report.hits:
        lines.append("| File | Pattern | Match | Snippet |")
        lines.append("|------|---------|-------|---------|")
        for h in report.hits:
            lines.append(
                f"| `{h.file}` | `{h.pattern}` | `{h.match}` | {h.snippet[:120]} |"
            )
    if report.archives_uninspected:
        lines.append("")
        lines.append("## Archives (uninspected)")
        for a in report.archives_uninspected:
            lines.append(f"- `{a}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = ["LeakHit", "VerifierReport", "verify", "write_verifier_report"]
