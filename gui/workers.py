"""QThread workers driving the engine pipeline.

All long-running stages are now cooperatively cancellable: the worker owns a
``threading.Event`` that gets propagated to engine stages via the ``stop_event``
keyword. Use :meth:`request_stop` from the GUI to ask the worker to stop ASAP.
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from anonymize.candidates import Candidate
from anonymize.llm_client import LLMClient
from anonymize.pipeline import (
    StageResult,
    stage_apply,
    stage_auto_resolve_residuals,
    stage_build,
    stage_detect_and_critic,
    stage_promote,
    stage_scan_and_rules,
    stage_verify,
)
from anonymize.project import Project


class _Signals(QObject):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished = Signal(bool, str, dict)
    cancelled = Signal()
    result = Signal(object)


def _safe_progress(sig):
    def cb(done: int, total: int, lbl: str) -> None:
        try:
            sig.emit(int(done), int(total), str(lbl))
        except Exception:
            pass
    return cb


class _StoppableThread(QThread):
    def __init__(self, project: Project, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.project = project
        self.signals = _Signals()
        self._stop_event = threading.Event()
        # Subclasses that drive an LLM stash the client here so
        # ``request_stop`` can also kick the server-side slot.
        self._llm: Optional[LLMClient] = None

    def request_stop(self) -> None:
        self._stop_event.set()
        # Cooperative cancel only fires between HTTP calls, when a
        # chat POST is in flight the worker is blocked in ``recv()``
        # until llama-server finishes generating. Hit ``/slots/<id>
        # ?action=erase`` so the GPU stops *now* and the pending POST
        # returns instantly. Best-effort: non-llama backends ignore it.
        llm = self._llm
        if llm is not None:
            try:
                llm.abort_in_flight()
            except Exception:
                pass

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event


class ScanWorker(_StoppableThread):
    """Runs scan_and_rules + detect_and_critic with cooperative cancel."""

    def __init__(self, project: Project, parent: Optional[QObject] = None) -> None:
        super().__init__(project, parent)

    def run(self) -> None:
        try:
            fresh_label = " [fresh re-scan]" if self.project.force_rescan else ""
            self.signals.log.emit(
                f"scan: starting on {self.project.input_paths[0]}{fresh_label}"
            )
            scan, t0_cands, r0 = stage_scan_and_rules(
                self.project,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
                force_rescan=self.project.force_rescan,
            )
            if r0.cancelled:
                self.signals.cancelled.emit()
                self.signals.finished.emit(False, "cancelled", {})
                return
            self.signals.log.emit(f"scan: {r0.message}")
            self._llm = LLMClient(
                base_url=self.project.llm_url,
                model=self.project.llm_model,
                max_workers=self.project.concurrency,
            )
            triage_res, r1 = stage_detect_and_critic(
                self.project,
                scan,
                t0_cands,
                llm=self._llm,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
                force_rescan=self.project.force_rescan,
            )
            if r1.cancelled:
                self.signals.cancelled.emit()
                self.signals.finished.emit(False, "cancelled", {})
                return
            self.signals.log.emit(f"scan: {r1.message}")
            self.signals.result.emit({"scan": scan, "triage": triage_res})
            self.signals.finished.emit(
                r1.ok, r1.message, {**(r0.extras or {}), **(r1.extras or {})}
            )
        except Exception as e:
            self.signals.log.emit(f"scan worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


class PromoteWorker(QThread):
    def __init__(
        self,
        project: Project,
        approved_pending: Optional[list[Candidate]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        # ``None`` = :func:`stage_promote` reads ``needs_review.yml`` from
        # disk (Run-all / Approve & continue path). A non-``None`` list is
        # the explicit subset approved in the Review view (may be empty).
        self.approved_pending = approved_pending
        self.signals = _Signals()

    def run(self) -> None:
        try:
            r = stage_promote(self.project, pending=self.approved_pending)
            self.signals.log.emit(f"promote: {r.message}")
            self.signals.finished.emit(r.ok, r.message, r.extras)
        except Exception as e:
            self.signals.log.emit(f"promote worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


class ApplyWorker(_StoppableThread):
    def run(self) -> None:
        try:
            report, r = stage_apply(
                self.project,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
            )
            if r.cancelled:
                self.signals.cancelled.emit()
                self.signals.finished.emit(False, "cancelled", {})
                return
            self.signals.log.emit(f"apply: {r.message}")
            self.signals.result.emit(report)
            self.signals.finished.emit(r.ok, r.message, r.extras)
        except Exception as e:
            self.signals.log.emit(f"apply worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


class BuildWorker(_StoppableThread):
    def run(self) -> None:
        try:
            report, r = stage_build(
                self.project,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
            )
            if getattr(report, "cancelled", False):
                self.signals.cancelled.emit()
                self.signals.finished.emit(False, "cancelled", {})
                return
            self.signals.log.emit(f"build: {r.message}")
            self.signals.result.emit(report)
            self.signals.finished.emit(r.ok, r.message, r.extras)
        except Exception as e:
            self.signals.log.emit(f"build worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


class VerifyWorker(_StoppableThread):
    def run(self) -> None:
        try:
            report, r = stage_verify(
                self.project,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
            )
            self.signals.log.emit(f"verify: {r.message}")
            self.signals.result.emit(report)
            self.signals.finished.emit(r.ok, r.message, r.extras)
        except Exception as e:
            self.signals.log.emit(f"verify worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


class AutoResolveWorker(_StoppableThread):
    """Run the verifier-feedback auto-loop after verify.

    The deterministic channel (regex + map regression) usually
    completes in sub-second. When the project enables LLM audit and
    the deterministic channel runs dry, the worker also calls the
    LLM-driven auditor (typos / concatenations / creative variants
    grounded in the existing map). The worker forwards the final
    ``VerifierReport`` via the ``result`` signal so the verifier
    view can refresh.
    """

    def run(self) -> None:
        try:
            audit_llm: Optional[LLMClient] = None
            if getattr(self.project, "audit_residuals_with_llm", False):
                try:
                    audit_llm = LLMClient(
                        base_url=self.project.llm_url,
                        model=self.project.llm_model,
                        max_workers=self.project.concurrency,
                    )
                    if not audit_llm.health(refresh=True):
                        audit_llm = None
                except Exception:
                    audit_llm = None
            # Expose to ``request_stop`` so an in-flight audit POST can
            # be aborted via ``/slots/<id>?action=erase`` on llama-server.
            self._llm = audit_llm
            report, r = stage_auto_resolve_residuals(
                self.project,
                llm=audit_llm,
                progress=_safe_progress(self.signals.progress),
                stop_event=self._stop_event,
            )
            self.signals.log.emit(f"auto-resolve: {r.message}")
            self.signals.result.emit(report)
            self.signals.finished.emit(r.ok, r.message, r.extras)
        except Exception as e:
            self.signals.log.emit(f"auto-resolve worker error: {e}")
            self.signals.finished.emit(False, str(e), {})


__all__ = [
    "ScanWorker",
    "PromoteWorker",
    "ApplyWorker",
    "BuildWorker",
    "VerifyWorker",
    "AutoResolveWorker",
]
