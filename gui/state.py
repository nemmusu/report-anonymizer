"""Centralized AppState: single source of truth for the GUI."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from anonymize.applier import ApplyReport
from anonymize.builder import BuildReport
from anonymize.candidates import Candidate
from anonymize.hardware import (
    HardwareReport,
    _read_installer_sentinel,
    report as hw_report,
)
from anonymize.hf_models import MODELS_DIR
from anonymize.project import Project
from anonymize.scanner import ScanResult
from anonymize.server_manager import ServerManager
from anonymize.server_profile import (
    ServerProfile,
    get_default_profile,
    get_profile,
    load_profiles,
    save_user_deployment_override,
)
from anonymize.sub_map import SubstitutionMap
from anonymize.verifier import VerifierReport


def _autocorrect_stale_deployment(p: ServerProfile) -> ServerProfile:
    """Flip ``deployment_mode`` back to ``local_binary`` when the
    persisted choice is ``docker`` but Docker is not installed AND
    the Windows installer has staged a local llama-server binary.

    The wizard that ships with the source tree (no installer) used to
    suggest Docker on machines with no llama-server in PATH. If the
    operator clicked OK there, the choice landed in the user-scope
    ``server.yml`` for the active preset (``default``) — and stayed
    there even after they installed the EXE that bundles a CUDA /
    Vulkan / CPU llama-server. Every Start then floods the log with
    ``[docker] stop failed: [WinError 2]`` because Docker is missing.

    The fix runs at AppState construction time so the corrected
    deployment_mode is visible everywhere downstream (chooser dialog,
    Start button, etc.) without the user having to click "Configure
    deployment" first.
    """
    if p.deployment_mode != "docker":
        return p
    if shutil.which("docker") is not None:
        return p
    sentinel = _read_installer_sentinel()
    if not isinstance(sentinel, dict):
        return p
    variant = sentinel.get("variant")
    if not isinstance(variant, str) or variant not in ("cpu", "cuda", "vulkan"):
        return p
    try:
        save_user_deployment_override(p.name, deployment_mode="local_binary")
    except Exception:
        pass
    p.deployment_mode = "local_binary"
    return p


def _initial_profile() -> ServerProfile:
    """Pick the default profile - honouring the user preference."""
    p = get_default_profile()
    if p is None:
        # last resort: synthesize a CPU-friendly profile
        return ServerProfile(
            name="default",
            description="auto-generated",
            model=str(MODELS_DIR / "Qwen3.5-9B-UD-Q6_K_XL.gguf"),
        )
    return _autocorrect_stale_deployment(p)


class AppState(QObject):
    project_changed = Signal(object)
    scan_changed = Signal(object)
    map_changed = Signal(object)
    candidates_changed = Signal()
    apply_report_changed = Signal(object)
    build_report_changed = Signal(object)
    verifier_changed = Signal(object)
    server_status_changed = Signal(bool, str)
    # Emitted whenever the "starting in progress" flag flips. Lets the
    # status-bar widget update its label/LED without polling for it.
    server_starting_changed = Signal(bool)
    log_message = Signal(str)
    busy_changed = Signal(bool, str)
    hardware_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.project: Optional[Project] = None
        self.scan: Optional[ScanResult] = None
        self.smap: Optional[SubstitutionMap] = None
        self.auto_t0: list[Candidate] = []
        self.auto_t1: list[Candidate] = []
        self.pending: list[Candidate] = []
        self.apply_report: Optional[ApplyReport] = None
        self.build_report: Optional[BuildReport] = None
        self.verifier_report: Optional[VerifierReport] = None
        self.profile: ServerProfile = _initial_profile()
        self.server: ServerManager = ServerManager(self.profile)
        self.hardware: Optional[HardwareReport] = None
        self._busy: bool = False
        # Last known llama-server health (refreshed by ServerPanel's
        # poll loop). Cached so PipelineView's Run-gate can read it
        # cheaply on every sync without firing a network call.
        self._server_online: bool = False
        # ``True`` while a Start request is in flight: the worker is
        # spawning llama-server but the health endpoint hasn't come
        # up yet. The ServerPanel/StatusWidget poll loops watch this
        # and render "starting…" instead of flickering between
        # "offline → online" while the binary boots.
        self._server_starting: bool = False

    def set_project(self, p: Optional[Project]) -> None:
        self.project = p
        if p is not None:
            try:
                self.smap = SubstitutionMap.load(p.map_path)
                self.map_changed.emit(self.smap)
            except Exception as e:
                self.log_message.emit(f"map load failed: {e}")
        self.project_changed.emit(p)

    def set_scan(self, s: Optional[ScanResult]) -> None:
        self.scan = s
        self.scan_changed.emit(s)

    def set_candidates(
        self,
        *,
        auto_t0: Optional[list[Candidate]] = None,
        auto_t1: Optional[list[Candidate]] = None,
        pending: Optional[list[Candidate]] = None,
    ) -> None:
        if auto_t0 is not None:
            self.auto_t0 = list(auto_t0)
        if auto_t1 is not None:
            self.auto_t1 = list(auto_t1)
        if pending is not None:
            self.pending = list(pending)
        self.candidates_changed.emit()

    def set_apply_report(self, r: Optional[ApplyReport]) -> None:
        self.apply_report = r
        self.apply_report_changed.emit(r)

    def set_build_report(self, r: Optional[BuildReport]) -> None:
        self.build_report = r
        self.build_report_changed.emit(r)

    def set_verifier_report(self, r: Optional[VerifierReport]) -> None:
        self.verifier_report = r
        self.verifier_changed.emit(r)

    def set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        self.busy_changed.emit(busy, label)

    def set_profile(self, prof: ServerProfile) -> None:
        try:
            self.server.stop()
        except Exception:
            pass
        self.profile = prof
        self.server = ServerManager(prof)

    def detect_hardware(self) -> HardwareReport:
        self.hardware = hw_report()
        self.hardware_changed.emit(self.hardware)
        return self.hardware

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def server_online(self) -> bool:
        """Cached llama-server health (refreshed by ServerPanel's poll)."""
        return self._server_online

    def set_server_online(self, online: bool) -> None:
        if online == self._server_online:
            return
        self._server_online = online
        # Reuse the existing signal so any listener (PipelineView's
        # Run-gate, status-bar widgets) stays in sync.
        self.server_status_changed.emit(online, "online" if online else "offline")

    @property
    def server_starting(self) -> bool:
        return self._server_starting

    def set_server_starting(self, starting: bool) -> None:
        if starting == self._server_starting:
            return
        self._server_starting = starting
        self.server_starting_changed.emit(starting)

    # ---- in_map / leak helpers -----------------------------------------
    # These thin helpers expose the "what will end up in the
    # anonymisation map" lens over the existing candidate buckets.
    # Approved pending candidates and every auto-promoted (T0/T1)
    # candidate are considered ``in_map`` (they'll be merged at the
    # next promote); skipped / undecided pending rows are considered
    # ``out``. The substitution map itself is the source of truth
    # for already-promoted entries.

    def iter_unreviewed_pending(self) -> list[Candidate]:
        """Pending candidates the operator never opened a decision on.

        Returns every row in ``self.pending`` whose ``decision`` is one
        of ``None`` / ``""`` / ``"pending"`` — i.e. the operator has
        neither approved nor skipped it. Used by the pre-promote gate
        so a Run-all that skipped the Review tab entirely surfaces a
        confirmation dialog instead of silently auto-approving every
        pending row via ``stage_promote`` (which merges everything
        that isn't explicitly ``"skip"``).

        Unlike :meth:`iter_unhandled_leaks` we deliberately do **not**
        prune rows already in the substitution map or in the auto
        T0/T1 buckets: even when those rows would land in the map by
        default, the operator has not actively reviewed them, and the
        confirmation copy is built around that reading. Filtering them
        out used to miss the obvious case where the user wanted to be
        asked the very first time the queue ran through promote.
        """
        out: list[Candidate] = []
        for c in (self.pending or []):
            if not c.value:
                continue
            decision = (c.decision or "").strip().lower()
            if decision in ("", "pending"):
                out.append(c)
        return out

    def iter_unhandled_leaks(self) -> list[Candidate]:
        """Pending candidates that will NOT be substituted by Build.

        A "leak" here = a pending Candidate the user hasn't approved
        (decision != ``approve``) whose value isn't already covered
        either by the substitution map or by the auto-promoted T0/T1
        buckets. The build-time warning dialog uses the length of this
        list to decide whether to prompt the operator before
        committing.
        """
        smap_keys: set[str] = set()
        if self.smap is not None:
            try:
                smap_keys = set(self.smap.keys())
            except Exception:
                smap_keys = set()
        auto_vals: set[str] = {
            c.value for c in (self.auto_t0 or []) if c.value
        } | {
            c.value for c in (self.auto_t1 or []) if c.value
        }
        out: list[Candidate] = []
        for c in (self.pending or []):
            if not c.value:
                continue
            if c.value in smap_keys or c.value in auto_vals:
                continue
            if (c.decision or "pending") == "approve":
                continue
            out.append(c)
        return out

    def set_included(self, value: str, included: bool) -> bool:
        """Toggle the ``in_map`` flag on the pending candidate
        identified by ``value``.

        ``included=True`` → ``decision=approve`` (will be merged into
        the substitution map at the next promote). ``included=False``
        → ``decision=pending`` (stays visible, not in the map).
        Returns ``True`` when a matching pending Candidate was found
        and updated, ``False`` otherwise. Does not touch the
        substitution map or the auto-promoted buckets.
        """
        target = "approve" if included else "pending"
        changed = False
        for c in self.pending or []:
            if c.value == value:
                if (c.decision or "pending") != target:
                    c.decision = target
                    changed = True
        if changed:
            self.candidates_changed.emit()
        return changed

    def shutdown(self) -> None:
        """Best-effort cleanup of background resources at app exit.

        Currently this just makes sure the spawned ``llama-server`` PID is
        terminated. Safe to call multiple times.

        Short timeout: ``MainWindow.closeEvent`` already stopped the
        server with a generous deadline; this is a belt-and-suspenders
        pass that runs only if something is still alive, so a 1 s
        budget is plenty — anything longer just makes the X-button
        feel sluggish.
        """
        try:
            if self.server is not None and self.server.is_running():
                self.server.stop(timeout=1.0)
        except Exception:
            pass


__all__ = ["AppState"]
