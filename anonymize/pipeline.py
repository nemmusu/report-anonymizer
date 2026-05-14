"""High-level pipeline orchestration used by both the CLI and the GUI.

Each stage is a small function that takes a :class:`Project` (plus a few
config inputs) and produces a structured result. Long stages accept a
``progress`` callback that emits ``(done, total, current_label)`` and an
optional ``stop_event`` for cooperative cancellation.

Stages:

1. :func:`stage_scan_and_rules` (no LLM)
2. :func:`stage_detect_and_critic` (LLM, parallel)
3. :func:`stage_promote` (no LLM, idempotent)
4. :func:`stage_apply` (no LLM, atomic writes)
5. :func:`stage_build` (no LLM, optional)
6. :func:`stage_verify` (no LLM)

After every successful stage we update ``<output>/.anon/state.json`` so the
GUI can offer "Resume from <stage>" on the next launch.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

from .applier import ApplyReport, apply, write_apply_report
from .builder import BuildReport, build_dossier, build_single_md
from .candidates import Candidate
from .critic import CriticConfig, run_critic
from .decisions_log import DecisionsLog
from .detector import DetectorConfig, load_safe_terms, run_detector
from .format_adapters import get_adapter, set_export_template, set_pdf_strategy
from .image_inventory import (
    FileInventory,
    ImageInventory,
    InventoryImage,
    ImageLocation,
    compute_image_id,
    load_decisions,
    load_inventory,
    save_inventory,
    write_thumbnail,
)
from .llm_client import LLMClient
from .project import Project
from .rules_pass import run_rules_pass
from .scanner import ScanResult, scan_files, scan_path
from .sub_map import SubstitutionMap
from .triage import TriageConfig, TriageResult, read_candidates_yaml, triage, write_candidates_yaml
from .verifier import VerifierReport, verify, write_verifier_report


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MULTIPASS_PROMPTS_DIR = PROMPTS_DIR / "detector_multipass"


def _resolve_detector_prompt_paths(project: "Project") -> list[Path]:
    """Pick the detector prompts based on ``project.detector_mode``.

    Order of precedence:
    1. ``ANONYMIZE_DETECTOR_PROMPTS`` env var (``os.pathsep``-separated list
       of paths: ``:`` on POSIX, ``;`` on Windows -- matching the same
       convention as ``PATH`` so Windows drive letters like ``C:\\foo`` are
       not mis-split). Reserved for batch A/B testing and CI fixtures,
       takes priority over the project setting so callers can override
       without rewriting the YAML.
    2. ``project.detector_mode == "multipass"``: the 11 per-category
       prompts under ``prompts/detector_multipass/``.
    3. Default (``"single"``): the monolithic
       ``prompts/system_detector.txt``.

    Missing prompt files are reported clearly so a partially-installed
    package never silently degrades to fewer passes.
    """
    env_override = os.environ.get("ANONYMIZE_DETECTOR_PROMPTS", "").strip()
    if env_override:
        return [Path(p.strip()) for p in env_override.split(os.pathsep) if p.strip()]

    mode = getattr(project, "detector_mode", "single") or "single"
    if mode == "multipass":
        from .project import MULTIPASS_PROMPT_FILES

        paths = [MULTIPASS_PROMPTS_DIR / name for name in MULTIPASS_PROMPT_FILES]
        missing = [p for p in paths if not p.exists()]
        if missing:
            joined = ", ".join(str(p.name) for p in missing)
            raise FileNotFoundError(
                "multipass detector prompts not found: "
                f"{joined} (looked under {MULTIPASS_PROMPTS_DIR})"
            )
        return paths

    return [PROMPTS_DIR / "system_detector.txt"]


@dataclass
class StageResult:
    ok: bool = True
    message: str = ""
    extras: dict = field(default_factory=dict)
    cancelled: bool = False


def _merge_preserving_user_edits(
    new_cands: list[Candidate], old_path: Path
) -> list[Candidate]:
    """Return ``new_cands`` augmented with user-edited / user-decided
    rows from the YAML at ``old_path``.

    Why this exists: the GUI lets the operator hand-edit ``value`` and
    ``suggested_placeholder``, and approve/skip pending rows. Those
    mutations live in the YAML files (``auto_promoted_t{0,1}.yml``,
    ``needs_review.yml``). When the user later clicks Run again the
    scan/detect stages would otherwise blow those edits away by
    overwriting the file with fresh detector output.

    Merge policy:

    * Match by ``(value, category)``. New candidates whose key already
      exists in the old YAML inherit the old's user-mutable fields
      (placeholder, decision, ``user_edited`` flag) so edits stick.
      The detector-supplied confidence / rationale do refresh.
    * Old rows whose key is **not** present in the new run are kept if
      they're flagged ``user_edited`` or have a non-default
      ``decision``, losing them would surprise the operator (e.g.
      they renamed ``value`` to something the detector can no longer
      re-find).
    * Brand-new rows from the detector are appended verbatim.
    """
    if not old_path.exists():
        return new_cands
    try:
        old = read_candidates_yaml(old_path)
    except Exception:
        return new_cands
    # Match key prefers ``original_value`` (the detector's name for
    # this row) so that a user-renamed row still matches its
    # detector counterpart on the next scan.
    def _match_key(c: Candidate) -> tuple[str, str]:
        return (c.original_value or c.value, c.category)

    by_key: dict[tuple[str, str], Candidate] = {_match_key(c): c for c in old}

    seen: set[tuple[str, str]] = set()
    merged: list[Candidate] = []
    for c in new_cands:
        key = _match_key(c)
        old_c = by_key.get(key)
        if old_c is not None:
            # Refresh detector-provided fields, preserve user state.
            # ``value`` and ``suggested_placeholder`` are user-mutable;
            # if the operator hand-edited them ``user_edited`` is True
            # and we keep the operator's version verbatim. Otherwise
            # we adopt the latest detector output (handles the case
            # where the detector updated its own placeholder logic
            # between runs).
            if not getattr(old_c, "user_edited", False):
                old_c.value = c.value
                old_c.suggested_placeholder = (
                    c.suggested_placeholder or old_c.suggested_placeholder
                )
                old_c.original_value = ""
            old_c.confidence = c.confidence
            old_c.critic_confidence = c.critic_confidence
            old_c.critic_is_real_leak = (
                c.critic_is_real_leak or old_c.critic_is_real_leak
            )
            old_c.critic_category_correct = (
                c.critic_category_correct or old_c.critic_category_correct
            )
            old_c.critic_placeholder_safe = (
                c.critic_placeholder_safe or old_c.critic_placeholder_safe
            )
            old_c.critic_note = c.critic_note or old_c.critic_note
            old_c.rationale = c.rationale or old_c.rationale
            old_c.count = max(old_c.count, c.count)
            old_c.examples = c.examples or old_c.examples
            merged.append(old_c)
        else:
            merged.append(c)
        seen.add(key)

    # Keep old rows the user edited or decided on, even if the new
    # scan no longer detects them, losing them would silently undo
    # the operator's work.
    for key, old_c in by_key.items():
        if key in seen:
            continue
        if (
            getattr(old_c, "user_edited", False)
            or getattr(old_c, "decision", "pending") != "pending"
        ):
            merged.append(old_c)
    return merged


def _state_path(project: Project) -> Path:
    return project.state_path()


def load_state(project: Project) -> dict:
    p = _state_path(project)
    if not p.exists():
        return {"stage_done": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"stage_done": []}


def save_state(project: Project, *, stage: str, extras: Optional[dict] = None) -> None:
    p = _state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(project)
    sd = list(state.get("stage_done") or [])
    if stage not in sd:
        sd.append(stage)
    state["stage_done"] = sd
    state["last_stage"] = stage
    state["last_at"] = datetime.now(timezone.utc).isoformat()
    if extras:
        state.setdefault("extras", {})[stage] = extras
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mark_paused(project: Project, *, why: str) -> None:
    """Record a ``paused`` marker so the GUI can recover after a crash/quit."""
    p = _state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(project)
    state["paused"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "why": why,
    }
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def clear_pause_marker(project: Project) -> None:
    """Drop the ``paused`` marker from ``.anon/state.json`` (if present)."""
    p = _state_path(project)
    if not p.exists():
        return
    state = load_state(project)
    if "paused" not in state:
        return
    state.pop("paused", None)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def reset_run_state(project: Project) -> dict:
    """Delete per-run state files inside ``project.output_dir``.

    Removes ``auto_promoted_t0.yml``, ``auto_promoted_t1.yml``,
    ``needs_review.yml``, ``applied_substitutions.json``,
    ``decisions_history.jsonl``, ``verifier_report.md`` and the
    ``.anon/state.json`` checkpoint. The global ``substitution_map.yml``
    is left untouched.

    Returns a small report dict with the names of the files actually
    removed so the GUI can show feedback.
    """
    candidates = [
        project.auto_t0_path,
        project.auto_t1_path,
        project.pending_path,
        project.applied_path,
        project.decisions_path,
        project.verifier_report_path,
        _state_path(project),
    ]
    removed: list[str] = []
    for c in candidates:
        try:
            if c.exists():
                c.unlink(missing_ok=True)
                removed.append(c.name)
        except Exception:
            pass
    return {"removed": removed}


def write_run_manifest(project: Project, *, profile: Optional[dict] = None) -> None:
    p = project.manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "tool": "document-anonymizer-production",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": project.to_dict(),
        "profile": profile or {},
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _scan(project: Project) -> ScanResult:
    set_pdf_strategy(project.pdf_strategy)
    set_export_template(getattr(project, "export_template_id", None))
    if project.mode == "single":
        return scan_path(
            project.input_paths[0],
            pdf_strategy=project.pdf_strategy,
            follow_symlinks=project.follow_symlinks,
            max_depth=project.max_depth,
            max_file_size_mb=project.max_file_size_mb,
            exclude_paths=[Path(p) for p in project.exclude_paths],
            respect_gitignore=project.respect_gitignore,
            extra_ignore_patterns=project.exclude_patterns,
        )
    if project.mode == "multi":
        return scan_files(
            project.input_paths,
            pdf_strategy=project.pdf_strategy,
            max_file_size_mb=project.max_file_size_mb,
        )
    return scan_path(
        project.input_paths[0],
        pdf_strategy=project.pdf_strategy,
        follow_symlinks=project.follow_symlinks,
        max_depth=project.max_depth,
        max_file_size_mb=project.max_file_size_mb,
        exclude_paths=[Path(p) for p in project.exclude_paths],
        respect_gitignore=project.respect_gitignore,
        extra_ignore_patterns=project.exclude_patterns,
    )


# ---- Stage 1 -----------------------------------------------------------------


def stage_scan_and_rules(
    project: Project,
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    force_rescan: Optional[bool] = None,
) -> tuple[ScanResult, list[Candidate], StageResult]:
    scan = _scan(project)
    smap = SubstitutionMap.load(project.map_path)
    decisions = DecisionsLog.load(project.decisions_path)
    safe_terms = load_safe_terms(project.safe_terms_path)
    if progress:
        progress(0, max(1, len(scan.files)), "rules pass")
    if stop_event is not None and stop_event.is_set():
        return scan, [], StageResult(ok=False, cancelled=True, message="cancelled")
    fresh = force_rescan if force_rescan is not None else project.force_rescan
    map_keys_filter: set[str] = set() if fresh else smap.keys()
    cands, _occ = run_rules_pass(
        scan,
        patterns_path=project.patterns_path,
        decisions=decisions,
        existing_map_keys=map_keys_filter,
        safe_values=safe_terms,
    )
    cands = _merge_preserving_user_edits(cands, project.auto_t0_path)
    write_candidates_yaml(project.auto_t0_path, cands)
    # Build the image inventory in the same scan pass: reading once,
    # iterating once. The inventory is independent of the LLM stages,
    # so any re-scan keeps it fresh without the cost of detect/critic.
    image_count = 0
    try:
        image_count = scan_images(project, scan)
    except Exception:
        # Image inventory is best-effort, never blocks the text flow.
        pass
    if progress:
        progress(len(scan.files), max(1, len(scan.files)), "done")
    save_state(
        project,
        stage="rules",
        extras={"candidates": len(cands), "images": image_count},
    )
    return (
        scan,
        cands,
        StageResult(
            ok=True,
            message=f"T0: {len(cands)} candidates",
            extras={"candidates_count": len(cands), "images_count": image_count},
        ),
    )


def scan_images(project: Project, scan: ScanResult) -> int:
    """Walk the scanned input files and rebuild ``image_inventory.yml``.

    Returns the total image-occurrence count (one per ``InventoryImage``
    entry, multiple occurrences of the same logo across pages count
    multiple). Adapter dispatch happens via :func:`get_adapter`, so a
    new format that grows image support inherits the inventory step
    automatically by overriding ``inventory_images`` on its adapter.

    Per-format thumbnails land under ``project.image_thumbs_dir`` and
    are reused across re-scans (cache key is the ``image_id``).
    """
    files: list[FileInventory] = []
    total = 0
    for src in scan.files:
        path = Path(src.path)
        try:
            adapter = get_adapter(path)
        except Exception:
            continue
        try:
            raws = adapter.inventory_images(path)
        except Exception:
            raws = []
        if not raws:
            continue
        try:
            file_sha = _file_sha256(path)
        except Exception:
            file_sha = None
        images: list[InventoryImage] = []
        for raw in raws:
            image_id = compute_image_id(raw.raw_bytes)
            thumb_path: Optional[str] = None
            try:
                tdst = project.image_thumbs_dir / (
                    image_id.split(":", 1)[1] + ".jpg"
                )
                written = write_thumbnail(raw.raw_bytes, raw.fmt, tdst)
                if written is not None:
                    try:
                        thumb_path = str(written.relative_to(project.output_dir))
                    except ValueError:
                        thumb_path = str(written)
            except Exception:
                thumb_path = None
            location = ImageLocation.from_dict(raw.location or {})
            images.append(
                InventoryImage(
                    image_id=image_id,
                    format=raw.fmt,
                    width=int(raw.width or 0),
                    height=int(raw.height or 0),
                    location=location,
                    thumbnail=thumb_path,
                    warnings=list(raw.warnings or []),
                )
            )
            total += 1
        try:
            file_rel = str(path.relative_to(project.output_dir))
        except ValueError:
            file_rel = str(path)
        files.append(FileInventory(file=file_rel, file_sha256=file_sha, images=images))
    inv = ImageInventory(files=files)
    save_inventory(project.image_inventory_path, inv)
    return total


def _file_sha256(path: Path) -> str:
    import hashlib as _hl
    h = _hl.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- Stage 2 -----------------------------------------------------------------


def stage_detect_and_critic(
    project: Project,
    scan: ScanResult,
    t0_candidates: list[Candidate],
    *,
    llm: Optional[LLMClient] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    force_rescan: Optional[bool] = None,
) -> tuple[TriageResult, StageResult]:
    smap = SubstitutionMap.load(project.map_path)
    decisions = DecisionsLog.load(project.decisions_path)
    if llm is None:
        llm = LLMClient(
            base_url=project.llm_url,
            model=project.llm_model,
            max_workers=project.concurrency,
        )
    if not llm.health(refresh=True):
        return (
            TriageResult(auto_t0=t0_candidates, auto_t1=[], needs_review=[]),
            StageResult(
                ok=False,
                message=(
                    "LLM server not reachable on "
                    f"{project.llm_url}; only Tier-0 candidates available."
                ),
            ),
        )

    detector_prompt_paths = _resolve_detector_prompt_paths(project)
    det_cfg = DetectorConfig(
        system_prompt_path=detector_prompt_paths[0],
        user_template_path=PROMPTS_DIR / "detect_user.txt.j2",
        safe_terms_path=project.safe_terms_path,
        parallel=project.concurrency,
        chunk_strategy=getattr(project, "chunk_strategy", "structured"),
    )
    cri_cfg = CriticConfig(
        system_prompt_path=PROMPTS_DIR / "system_critic.txt",
        user_template_path=PROMPTS_DIR / "critic_user.txt.j2",
        safe_terms_path=project.safe_terms_path,
        parallel=project.concurrency,
    )
    tier0_values = {c.value for c in t0_candidates}

    if progress:
        progress(0, 100, "detector")
    fresh = force_rescan if force_rescan is not None else project.force_rescan
    map_keys_filter: set[str] = set() if fresh else smap.keys()

    total_passes = len(detector_prompt_paths)
    pass_results: list[tuple[list[Candidate], list]] = []
    for pass_idx, ppath in enumerate(detector_prompt_paths):
        det_cfg_pass = DetectorConfig(
            system_prompt_path=ppath,
            user_template_path=det_cfg.user_template_path,
            safe_terms_path=det_cfg.safe_terms_path,
            parallel=det_cfg.parallel,
            chunk_strategy=det_cfg.chunk_strategy,
        )

        def _pass_progress(d: int, t: int, lbl: str, _pass=pass_idx, _total=total_passes) -> None:
            if not progress:
                return
            per_pass = 50.0 / max(1, _total)
            base = _pass * per_pass
            progress(
                int(base + per_pass * d / max(1, t)),
                100,
                f"detect[{_pass + 1}/{_total}] {lbl}",
            )

        cands_i, occ_i = run_detector(
            scan,
            llm=llm,
            config=det_cfg_pass,
            decisions=decisions,
            existing_map_keys=map_keys_filter,
            tier0_values=tier0_values,
            progress=_pass_progress if progress else None,
            stop_event=stop_event,
        )
        pass_results.append((cands_i, occ_i))
        if stop_event is not None and stop_event.is_set():
            break

    # Merge candidates across passes: dedup by value, keep highest
    # confidence and union the example list.
    merged: dict[str, Candidate] = {}
    merged_occ: list = []
    for cands_i, occ_i in pass_results:
        for c in cands_i:
            existing = merged.get(c.value)
            if existing is None:
                merged[c.value] = c
                continue
            existing.count += c.count
            for ex in c.examples:
                if ex not in existing.examples and len(existing.examples) < 5:
                    existing.examples.append(ex)
            if c.confidence > existing.confidence:
                existing.confidence = c.confidence
                existing.category = c.category
                existing.suggested_placeholder = c.suggested_placeholder
                existing.rationale = c.rationale
        merged_occ.extend(occ_i)
    t1_cands = list(merged.values())
    _occ = merged_occ
    if stop_event is not None and stop_event.is_set():
        return (
            TriageResult(auto_t0=t0_candidates, auto_t1=t1_cands, needs_review=[]),
            StageResult(ok=False, cancelled=True, message="cancelled"),
        )
    # Identity placeholders ("AcmeBank → AcmeBank", "JSESSIONID=… →
    # JSESSIONID=…") are no-ops at apply time and the critic always
    # votes ``placeholder_safe: no`` on them, which means they end
    # up in needs_review.yml waiting for a human even though we
    # already know how to derive a valid placeholder for the
    # category. Repair them BEFORE the critic sees them so the
    # critic can vote on a real placeholder and the candidate flows
    # to auto_t1 instead of getting stuck.
    if t1_cands:
        _fill_missing_placeholders(t1_cands, decisions)
    if progress:
        progress(50, 100, "critic")
    if t1_cands:
        run_critic(
            t1_cands,
            llm=llm,
            config=cri_cfg,
            progress=lambda d, t: progress(50 + int(40 * d / max(1, t)), 100, f"critic {d}/{t}")
            if progress
            else None,
            stop_event=stop_event,
        )

    triage_cfg = TriageConfig(t_high=project.t_high, t_low=project.t_low)
    res = triage(
        t0_candidates=t0_candidates, t1_candidates=t1_cands, config=triage_cfg
    )
    merged_t1 = _merge_preserving_user_edits(res.auto_t1, project.auto_t1_path)
    merged_pending = _merge_preserving_user_edits(
        res.needs_review, project.pending_path
    )
    # Mutate the result so the GUI's ``set_candidates(auto_t1=res...)``
    # call also sees the merged rows, otherwise the in-memory state
    # would diverge from the YAML on disk for one tick.
    res.auto_t1 = merged_t1
    res.needs_review = merged_pending
    write_candidates_yaml(project.auto_t1_path, merged_t1)
    write_candidates_yaml(project.pending_path, merged_pending)
    if progress:
        progress(100, 100, "done")
    save_state(
        project,
        stage="detect_critic",
        extras={
            "auto_t1_count": len(res.auto_t1),
            "needs_review_count": len(res.needs_review),
        },
    )
    return res, StageResult(
        ok=True,
        message=(
            f"T1: {len(res.auto_t1)} auto-promoted, "
            f"{len(res.needs_review)} need review"
        ),
        extras={
            "auto_t1_count": len(res.auto_t1),
            "needs_review_count": len(res.needs_review),
        },
    )


# ---- Stage 3: promote --------------------------------------------------------


def _fill_missing_placeholders(
    cands: list[Candidate], decisions: "DecisionsLog"
) -> int:
    """Fill in a sensible placeholder for any candidate whose
    ``suggested_placeholder`` is missing or equal to ``value``.

    This prevents the silent-drop bug where the operator approved a
    candidate in Review (e.g. an LLM-proposed ``ids`` row whose
    ``suggested_placeholder`` echoes the value) and ``merge_candidates``
    skipped it because the placeholder was a no-op. The first pass
    runs ``auto_derive_placeholder`` which picks the right strategy
    (phone_intl / hex_keep_prefix / brand / hostname / ipv4 / generic
    based on category).

    A second, last-resort pass kicks in when the strategy itself
    returned ``None`` or echoed the value back (rare but possible for
    odd categories): we then mint a deterministic opaque placeholder
    ``[REDACTED-<CATEGORY>-<8-hex-hash>]``. The hash is taken over the
    ``category::value`` pair so re-runs against the same input land on
    the exact same placeholder, which keeps the diff stable across
    successive Apply passes and prevents the "0 events" / blank-diff
    surprise the user reported.
    """
    import hashlib

    from .placeholders import auto_derive_placeholder

    fixed = 0
    for c in cands:
        v = (c.value or "").strip()
        p = (c.suggested_placeholder or "").strip()
        if not v:
            continue
        if p and p != v:
            continue
        new_p = auto_derive_placeholder(v, c.category or "other", log=decisions)
        if not new_p or new_p == v:
            cat = (c.category or "other").strip().lower() or "other"
            digest = hashlib.sha256(
                f"{cat}::{v}".encode("utf-8")
            ).hexdigest()[:8]
            new_p = f"[REDACTED-{cat.upper()}-{digest}]"
        if new_p and new_p != v:
            c.suggested_placeholder = new_p
            fixed += 1
    return fixed


def stage_promote(
    project: Project,
    *,
    pending: Optional[list[Candidate]] = None,
) -> StageResult:
    smap = SubstitutionMap.load(project.map_path)
    decisions = DecisionsLog.load(project.decisions_path)

    auto_t0 = read_candidates_yaml(project.auto_t0_path)
    auto_t1 = read_candidates_yaml(project.auto_t1_path)
    if pending is None:
        pending = read_candidates_yaml(project.pending_path)
        # Honour per-row decisions persisted in needs_review.yml: rows
        # the operator explicitly skipped never reach the map. Rows
        # without an explicit decision (legacy or "Approve & continue"
        # path) remain merged for backward compatibility.
        pending = [
            c for c in pending if getattr(c, "decision", "pending") != "skip"
        ]
    # Same guard for auto-promoted lists, a user can demote an auto
    # row to pending and skip it there, but they can also Un-approve
    # an auto row directly which currently routes through demote+skip.
    # If the auto row carries decision=='skip', honour that intent.
    auto_t0 = [c for c in auto_t0 if getattr(c, "decision", "pending") != "skip"]
    auto_t1 = [c for c in auto_t1 if getattr(c, "decision", "pending") != "skip"]

    # Operators approve candidates in Review even when the LLM proposed
    # an empty / identity placeholder. ``merge_candidates`` would drop
    # those silently, auto-derive the placeholder before merging.
    auto_filled = (
        _fill_missing_placeholders(auto_t0, decisions)
        + _fill_missing_placeholders(auto_t1, decisions)
        + _fill_missing_placeholders(pending, decisions)
    )

    added = 0
    added += smap.merge_candidates(auto_t0)
    added += smap.merge_candidates(auto_t1)
    added += smap.merge_candidates(pending)
    smap.save()

    # Prune the auto / pending YAMLs of entries that are now in the
    # map.  Without this they'd accumulate stale rows and the Review
    # tree would render every promoted row twice (once as ✓ in-map,
    # once as ✓ auto / · pending).  Skipped rows are kept so the
    # operator's "no" decision survives the round-trip.
    map_keys = smap.keys()

    def _prune(cands: list[Candidate]) -> list[Candidate]:
        return [
            c
            for c in cands
            if c.value not in map_keys
            or getattr(c, "decision", "pending") == "skip"
        ]

    write_candidates_yaml(project.auto_t0_path, _prune(auto_t0))
    write_candidates_yaml(project.auto_t1_path, _prune(auto_t1))
    write_candidates_yaml(project.pending_path, _prune(pending))

    for c in auto_t0 + auto_t1 + pending:
        decisions.record_decision(
            "promote",
            c.value,
            c.suggested_placeholder,
            c.category,
            meta={"tier": c.tier},
        )

    save_state(
        project, stage="promote", extras={"added": added, "auto_filled": auto_filled}
    )
    msg = f"Map: +{added} new entries"
    if auto_filled:
        msg += f" ({auto_filled} placeholder(s) auto-derived)"
    return StageResult(
        ok=True,
        message=msg,
        extras={
            "added": added,
            "auto_filled": auto_filled,
            "total_keys": len(smap.keys()),
        },
    )


# ---- Stage 4: apply ----------------------------------------------------------


def stage_apply(
    project: Project,
    scan: Optional[ScanResult] = None,
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[ApplyReport, StageResult]:
    # Adapters read the strategy / template at call time, set both
    # explicitly here so a cached ``scan`` (which skips ``_scan`` and
    # therefore the setters there) doesn't fall back to the defaults
    # of a previous, differently-configured project.
    set_pdf_strategy(project.pdf_strategy)
    set_export_template(getattr(project, "export_template_id", None))
    smap = SubstitutionMap.load(project.map_path)
    rules = smap.to_rules(tier="T2_human")
    if scan is None:
        scan = _scan(project)
    report = apply(project, scan, rules, progress=progress, stop_event=stop_event)
    write_apply_report(report, project.applied_path)
    if report.cancelled:
        return report, StageResult(
            ok=False,
            cancelled=True,
            message="apply cancelled",
            extras={
                "files": report.total_files,
                "events": report.total_events,
                "skipped_binary": report.skipped_binary,
            },
        )
    # Image redaction pass. Strict two-stage: text apply must be done
    # FIRST (above) so the textual round-trip is byte-for-byte
    # identical to the pre-image-redaction behaviour. Image apply
    # then walks the freshly-written output, replacing image bytes
    # in place. Failure here NEVER fails the text apply (the text
    # pipeline's correctness is the primary contract).
    image_applied = 0
    image_skipped = 0
    image_warnings: list[str] = []
    try:
        image_applied, image_skipped, image_warnings = stage_apply_images(
            project, scan
        )
    except Exception as e:
        image_warnings.append(f"image_apply_unexpected_failure:{e}")
    save_state(
        project,
        stage="apply",
        extras={
            "files": report.total_files,
            "events": report.total_events,
            "skipped_binary": report.skipped_binary,
            "images_applied": image_applied,
            "images_skipped": image_skipped,
        },
    )
    extras = {
        "files": report.total_files,
        "events": report.total_events,
        "skipped_binary": report.skipped_binary,
        "images_applied": image_applied,
        "images_skipped": image_skipped,
    }
    if image_warnings:
        extras["image_warnings"] = list(image_warnings)
    msg = (
        f"Apply: {report.total_files} files, {report.total_events} events, "
        f"{report.skipped_binary} binary copied as-is"
    )
    if image_applied:
        msg += f", {image_applied} image(s) redacted"
    return report, StageResult(ok=True, message=msg, extras=extras)


def stage_apply_images(
    project: Project,
    scan: ScanResult,
) -> tuple[int, int, list[str]]:
    """Walk the freshly-written output files, apply operator image
    redactions in place. Returns ``(applied, skipped, warnings)``.

    The output mirror tree is determined by the existing ``apply()``
    flow: each ``scan.files[i].path`` (input) maps to a destination
    inside ``project.output_dir``. We rederive the mapping by joining
    ``output_dir`` with the path's name (single-file mode) or with
    its tree-relative path (multi / folder mode); this matches the
    convention :class:`anonymize.applier.apply` already uses.
    """
    decisions = load_decisions(project.image_redactions_path)
    if not decisions.decisions:
        return (0, 0, [])
    inventory = load_inventory(project.image_inventory_path)
    if not inventory.files:
        return (0, 0, [])
    # Build a per-output-file map of {image_id: ImageDecision}. The
    # adapter's apply pass receives this dict per file.
    by_input: dict[str, dict[str, object]] = {}
    for f in inventory.files:
        decisions_for_file: dict[str, object] = {}
        for im in f.images:
            d = decisions.get(im.image_id)
            if d is None:
                continue
            decisions_for_file[im.image_id] = d
        if decisions_for_file:
            by_input[f.file] = decisions_for_file
    if not by_input:
        return (0, 0, [])

    applied = 0
    skipped = 0
    warnings: list[str] = []

    for src in scan.files:
        src_path = Path(src.path)
        # Resolve the source to the inventory key (relative-to-output-dir
        # if possible, else absolute, matching ``scan_images``).
        try:
            src_rel = str(src_path.relative_to(project.output_dir))
        except ValueError:
            src_rel = str(src_path)
        decisions_for_file = by_input.get(src_rel)
        if not decisions_for_file:
            continue
        # Find the freshly-written output mirror. The applier writes
        # to ``project.output_dir / <relative path of src>`` for
        # multi/folder, or to ``output_dir / single_output_filename``
        # for single mode (default: <name>.anonymized.<ext>).
        dst_path = _resolve_output_path(project, src_path, scan)
        if dst_path is None or not dst_path.exists():
            warnings.append(f"output_missing:{src_rel}")
            continue
        try:
            adapter = get_adapter(dst_path)
        except Exception as e:
            warnings.append(f"adapter_resolve_failed:{src_rel}:{e}")
            continue
        try:
            report = adapter.apply_image_redactions(dst_path, decisions_for_file)
        except Exception as e:
            warnings.append(f"image_apply_failed:{src_rel}:{e}")
            continue
        applied += int(getattr(report, "applied", 0))
        skipped += int(getattr(report, "skipped", 0))
        if getattr(report, "warnings", None):
            warnings.extend(f"{src_rel}:{w}" for w in report.warnings)
    return applied, skipped, warnings


def _resolve_output_path(
    project: Project,
    src_path: Path,
    scan: ScanResult,
) -> Optional[Path]:
    """Mirror the applier's source-to-destination path resolution.

    Single-file mode uses ``project.single_output_filename`` if set,
    otherwise ``<stem>.anonymized<suffix>``. Multi / folder mode keeps
    the relative path of ``src_path`` under the project's scan root,
    rooted at ``project.output_dir``.
    """
    out_root = project.output_dir
    if project.mode == "single":
        if project.single_output_filename:
            return out_root / project.single_output_filename
        return out_root / f"{src_path.stem}.anonymized{src_path.suffix}"
    # multi / folder: try every input root in scan_root_paths to find
    # the longest match.
    candidates: list[Path] = []
    if hasattr(scan, "root_paths"):
        candidates = [Path(p) for p in scan.root_paths]
    else:
        candidates = [Path(p) for p in project.input_paths]
    best_rel: Optional[Path] = None
    for root in candidates:
        try:
            rel = src_path.relative_to(root)
        except ValueError:
            continue
        if best_rel is None or len(str(rel)) < len(str(best_rel)):
            best_rel = rel
    if best_rel is None:
        # Fallback: just join the basename.
        return out_root / src_path.name
    return out_root / best_rel


# ---- Stage 5: build ----------------------------------------------------------


def _convert_with_pandoc(
    src: Path,
    target_ext: str,
    *,
    template_id: Optional[str] = None,
) -> Path:
    """Convert ``src`` to ``target_ext`` (``.pdf`` / ``.html`` / ``.md``)
    via pandoc + (for PDFs) WeasyPrint. Returns the path of the new
    artefact. Caller is responsible for catching ``RuntimeError``.

    When ``template_id`` resolves to a known template and the target
    is ``.pdf`` or ``.html``, the conversion goes through
    :mod:`anonymize.templates` so the Import dialog's promise that
    "Also export as PDF/HTML will use the template" actually holds
    in single mode (in folder mode this is already handled by
    :func:`build_dossier`).
    """
    import subprocess
    import shutil as _sh

    pandoc = _sh.which("pandoc")
    if pandoc is None:
        raise RuntimeError("pandoc not found in PATH")
    dst = src.with_suffix(target_ext)

    tmpl = None
    if template_id and target_ext in (".pdf", ".html"):
        try:
            from .templates import get_template
            tmpl = get_template(template_id)
        except Exception:
            tmpl = None

    if tmpl is not None and target_ext == ".pdf":
        # Templated PDF render: pandoc fragment + template wrapper +
        # WeasyPrint. ``with_cover=False`` keeps the cover-page out
        # of the output because at this point the operator has not
        # entered Engagement / Author / Date metadata anywhere; the
        # cover would render with empty fields. Title falls back to
        # the file stem.
        from .templates import (
            TemplateContext,
            render_pdf_with_template,
        )
        md = src.read_text(encoding="utf-8")
        if src.suffix.lower() in (".html", ".htm"):
            # Templates expect markdown; pre-strip to text and let
            # the template's CSS style the result rather than fighting
            # the source's inline styles.
            md = src.read_text(encoding="utf-8")
        render_pdf_with_template(
            md=md,
            template=tmpl,
            ctx=TemplateContext(title=dst.stem),
            dst=dst,
            with_cover=False,
        )
        return dst

    if tmpl is not None and target_ext == ".html":
        from .templates import (
            TemplateContext,
            render_html_with_template,
        )
        md = src.read_text(encoding="utf-8")
        render_html_with_template(
            md=md,
            template=tmpl,
            ctx=TemplateContext(title=dst.stem),
            dst=dst,
        )
        return dst

    if target_ext == ".pdf":
        # Legacy un-templated path: pandoc -> standalone HTML5 ->
        # WeasyPrint, same look the operator gets when no template
        # is selected. Pygments-driven syntax highlighting comes for
        # free with ``--standalone``.
        text = src.read_text(encoding="utf-8")
        proc = subprocess.run(
            [pandoc, "-f", "markdown" if src.suffix.lower() in (".md", ".markdown") else "html",
             "-t", "html5", "--standalone"],
            input=text, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc html: {proc.stderr.strip()[:300]}")
        from .pdf_render import render_html_to_pdf
        try:
            render_html_to_pdf(proc.stdout, dst, base_url=src.parent)
        except Exception as e:
            raise RuntimeError(f"weasyprint: {str(e)[:300]}")
        return dst
    # Generic: pandoc <src> -o <dst>
    proc = subprocess.run(
        [pandoc, str(src), "-o", str(dst)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc {target_ext}: {proc.stderr.strip()[:300]}")
    return dst


_EXTRA_FORMAT_EXT = {"pdf": ".pdf", "html": ".html", "md": ".md"}


def stage_build(
    project: Project,
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[BuildReport, StageResult]:
    if project.mode == "folder":
        report = build_dossier(
            project.output_dir,
            progress=progress,
            stop_event=stop_event,
            template_id=getattr(project, "export_template_id", None),
        )
        save_state(project, stage="build", extras={"pdfs": len(report.artefacts)})
        return report, StageResult(
            ok=True,
            message=f"Build: {len(report.artefacts)} PDFs ({len(report.warnings)} warnings)",
            extras={
                "artefacts": [str(p) for p in report.artefacts],
                "warnings": list(report.warnings),
            },
        )

    if project.mode == "single":
        out = project.output_path_for(_dummy_scan_for_single(project))
        # Build the union of legacy ``also_build_pdf_for_md`` and the
        # new ``extra_export_formats`` list, deduplicated. We skip a
        # target whose extension matches the source (would be a no-op).
        extras = list(getattr(project, "extra_export_formats", []) or [])
        if project.also_build_pdf_for_md and "pdf" not in extras:
            extras.append("pdf")
        artefacts: list[Path] = []
        warnings: list[str] = []
        if not out.exists():
            return BuildReport(warnings=["single-mode output missing"]), StageResult(
                ok=False, message="Build skipped (output missing)"
            )
        tmpl_id = getattr(project, "export_template_id", None)
        for fmt in extras:
            target_ext = _EXTRA_FORMAT_EXT.get(fmt)
            if not target_ext:
                continue
            if target_ext == out.suffix.lower():
                continue  # would clobber the original
            try:
                artefacts.append(
                    _convert_with_pandoc(out, target_ext, template_id=tmpl_id)
                )
            except Exception as e:
                warnings.append(f"{fmt}: {e}")
        save_state(
            project,
            stage="build",
            extras={"artefacts": [str(p) for p in artefacts]},
        )
        if not extras:
            # Single-mode "no extra format" path: Apply has already
            # written the redacted output in-place, there is nothing
            # left for Build to materialise. The previous wording
            # ("Build skipped") tripped the GUI's _looks_skipped
            # heuristic and the Build card rendered grey, which made
            # the operator think the substitutions had not been
            # written. Use neutral language so the card stays green.
            return BuildReport(), StageResult(
                ok=True,
                message=(
                    "Build: output already materialised by Apply "
                    "(no extra format requested)"
                ),
            )
        if not artefacts and warnings:
            return BuildReport(warnings=warnings), StageResult(
                ok=False, message=f"Build failed: {warnings[0]}"
            )
        return BuildReport(artefacts=artefacts, warnings=warnings), StageResult(
            ok=True,
            message=(
                f"Build: {len(artefacts)} extra "
                f"format{'s' if len(artefacts) != 1 else ''} "
                f"({', '.join(p.suffix.lstrip('.') for p in artefacts) or '-'})"
            ),
            extras={
                "artefacts": [str(p) for p in artefacts],
                "warnings": list(warnings),
            },
        )

    return BuildReport(), StageResult(ok=True, message="Build skipped (mode=multi)")


def _dummy_scan_for_single(project: Project):
    from .scanner import ScannedFile
    from .format_adapters import NullAdapter

    src = project.input_paths[0]
    return ScannedFile(
        path=src,
        rel=Path(src.name),
        adapter=NullAdapter(),
        size=src.stat().st_size if src.exists() else 0,
        is_text_like=False,
    )


# ---- Stage 6: verify ---------------------------------------------------------


def stage_verify(
    project: Project,
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[VerifierReport, StageResult]:
    smap = SubstitutionMap.load(project.map_path)
    if project.mode == "single":
        out_root = project.output_path_for(_dummy_scan_for_single(project))
    else:
        out_root = project.output_dir
    report = verify(
        out_root,
        patterns_path=project.patterns_path,
        map_keys=smap.keys(),
        map_entries=smap.entries,
        pdf_strategy=project.pdf_strategy,
    )
    write_verifier_report(report, project.verifier_report_path)
    save_state(
        project,
        stage="verify",
        extras={
            "hits": len(report.hits),
            "files_scanned": report.files_scanned,
            "is_clean": report.is_clean,
        },
    )
    return report, StageResult(
        ok=report.is_clean,
        message=(
            f"Verifier: {len(report.hits)} residual leaks "
            f"in {report.files_scanned} files ({report.pdfs_scanned} PDFs)"
        ),
        extras={
            "hits": len(report.hits),
            "files_scanned": report.files_scanned,
            "pdfs_scanned": report.pdfs_scanned,
            "is_clean": report.is_clean,
        },
    )


# ---- Stage 7: auto-resolve residual leaks -----------------------------------


def stage_auto_resolve_residuals(
    project: Project,
    *,
    max_iterations: int = 2,
    llm: Optional[LLMClient] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[VerifierReport, StageResult]:
    """Two-channel feedback loop after ``stage_verify``.

    Each iteration first tries the **deterministic** channel: derive
    a candidate for every regex/regression hit via
    :func:`anonymize.triage.derive_placeholder_for_hit`. If that
    channel runs dry but ``project.audit_residuals_with_llm`` is
    enabled and an ``llm`` client is provided, run the **LLM audit**
    channel (see :mod:`anonymize.auditor`): it spots typos,
    concatenations and creative variants of values already in the
    map. Both channels merge into ``stage_promote`` /
    ``stage_apply`` exactly the same way.

    The loop stops as soon as no hit has a derivable placeholder
    *and* the LLM audit (if enabled) returns nothing new.
    """
    from .triage import derive_placeholder_for_hit, write_candidates_yaml
    from .verifier import LeakHit

    if project.mode == "single":
        out_root = project.output_path_for(_dummy_scan_for_single(project))
    else:
        out_root = project.output_dir

    history: list[int] = []
    iterations = 0
    final_report: VerifierReport | None = None
    smap = SubstitutionMap.load(project.map_path)
    initial_hits: int = 0

    for iteration in range(max_iterations):
        if stop_event is not None and stop_event.is_set():
            break
        report = verify(
            out_root,
            patterns_path=project.patterns_path,
            map_keys=smap.keys(),
            map_entries=smap.entries,
            pdf_strategy=project.pdf_strategy,
        )
        final_report = report
        if iteration == 0:
            initial_hits = len(report.hits)
        history.append(len(report.hits))
        if not report.hits:
            break

        derived: list[Candidate] = []
        seen_values: set[str] = set()
        for hit in report.hits:
            value = getattr(hit, "match", "") or ""
            pattern = getattr(hit, "pattern", "") or ""
            if not value or value in seen_values:
                continue
            cand = derive_placeholder_for_hit(value, smap, pattern=pattern)
            if cand is None:
                continue
            seen_values.add(value)
            derived.append(cand)

        # Audit candidates the LLM produced this iteration but for
        # which it was NOT fully confident, written to Review only,
        # NOT auto-promoted. The user can approve/reject them and run
        # promote+apply manually.
        review_only: list[Candidate] = []

        # If the deterministic channel ran dry, fall back to the LLM
        # auditor (typos / concatenations / creative variants).
        if not derived and getattr(project, "audit_residuals_with_llm", False) and llm is not None:
            from .auditor import AuditConfig, run_audit
            from .verifier import _extract_text_from

            audit_cfg = AuditConfig(
                system_prompt_path=PROMPTS_DIR / "system_audit.txt",
                user_template_path=PROMPTS_DIR / "audit_user.txt.j2",
                parallel=max(1, project.concurrency),
            )
            audit_chunks: list[str] = []
            if out_root.is_file():
                audit_chunks.append(
                    _extract_text_from(out_root, pdf_strategy=project.pdf_strategy)
                )
            else:
                for p in out_root.rglob("*"):
                    if not p.is_file():
                        continue
                    txt = _extract_text_from(p, pdf_strategy=project.pdf_strategy)
                    if txt:
                        audit_chunks.append(txt)
            full_text = "\n\n".join(t for t in audit_chunks if t)
            if full_text:
                audit_candidates = run_audit(
                    full_text,
                    smap_entries=smap.entries,
                    llm=llm,
                    config=audit_cfg,
                    file_rel=str(out_root),
                    seg_id="audit",
                    progress=progress,
                    stop_event=stop_event,
                )
                for cand in audit_candidates:
                    if cand.value in seen_values:
                        continue
                    seen_values.add(cand.value)
                    if cand.confidence >= max(0.95, project.t_high):
                        # The LLM is sure (typically a direct case
                        # variant of a known map entry); auto-promote.
                        derived.append(cand)
                    else:
                        # Lower-confidence: typo or concatenation the
                        # operator should approve manually.
                        review_only.append(cand)

        # Always persist what the loop saw (auto-promoted + review-
        # only) into needs_review.yml so the operator has a complete
        # audit trail and can approve the review-only ones.
        if derived or review_only:
            try:
                from .triage import read_candidates_yaml as _read

                existing_pending = list(_read(project.pending_path))
            except Exception:
                existing_pending = []
            existing_values = {c.value for c in existing_pending}
            merged = existing_pending + [
                c
                for c in (derived + review_only)
                if c.value not in existing_values
            ]
            try:
                write_candidates_yaml(project.pending_path, merged)
            except Exception:
                pass

        if not derived:
            # Either the LLM auditor produced only low-confidence
            # candidates (now sitting in Review) or nothing at all -
            # stop the loop here.
            break

        promote_res = stage_promote(project, pending=derived)
        smap = SubstitutionMap.load(project.map_path)  # refresh after promote
        # Re-apply; any failure here is treated as a stop condition.
        _, apply_res = stage_apply(
            project, progress=progress, stop_event=stop_event
        )
        iterations += 1
        if apply_res.cancelled:
            break
        if not apply_res.ok:
            break
        if not promote_res.extras.get("added"):
            # ``merge_candidates`` accepted nothing new, every
            # ``derived`` candidate was already in the map. We still
            # gave apply a second chance above (PDF text-fragmentation
            # sometimes shifts after the first apply, exposing matches
            # that ``page.search_for`` could not see in the source),
            # but if the next verify-pass shows no improvement we
            # MUST break, otherwise we'd loop forever re-applying the
            # same map.
            if (
                len(history) >= 2
                and history[-1] == history[-2]
                and history[-1] > 0
            ):
                break

    # One final verify after the last apply to make the report
    # reflect the latest state.
    if iterations > 0:
        final_report = verify(
            out_root,
            patterns_path=project.patterns_path,
            map_keys=smap.keys(),
            map_entries=smap.entries,
            pdf_strategy=project.pdf_strategy,
        )
        history.append(len(final_report.hits))
        write_verifier_report(final_report, project.verifier_report_path)

    if final_report is None:
        final_report = verify(
            out_root,
            patterns_path=project.patterns_path,
            map_keys=smap.keys(),
            map_entries=smap.entries,
            pdf_strategy=project.pdf_strategy,
        )

    save_state(
        project,
        stage="auto_resolve",
        extras={
            "iterations": iterations,
            "history": history,
            "initial_hits": initial_hits,
            "final_hits": len(final_report.hits),
        },
    )
    if iterations == 0:
        message = (
            f"Auto-resolve: nothing to do ({len(final_report.hits)} "
            f"residual leak{'s' if len(final_report.hits) != 1 else ''})"
        )
    else:
        message = (
            f"Auto-resolve: {initial_hits} → {len(final_report.hits)} "
            f"residual leaks in {iterations} iteration"
            f"{'s' if iterations != 1 else ''}"
        )
    return final_report, StageResult(
        ok=final_report.is_clean or len(final_report.hits) <= initial_hits,
        message=message,
        extras={
            "iterations": iterations,
            "history": history,
            "initial_hits": initial_hits,
            "final_hits": len(final_report.hits),
            "is_clean": final_report.is_clean,
        },
    )


__all__ = [
    "PROMPTS_DIR",
    "StageResult",
    "stage_scan_and_rules",
    "stage_detect_and_critic",
    "stage_promote",
    "stage_apply",
    "stage_build",
    "stage_verify",
    "stage_auto_resolve_residuals",
    "load_state",
    "save_state",
    "write_run_manifest",
]
