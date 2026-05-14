"""Persistent download queue (survives app restarts).

Backed by ``~/.config/document-anonymizer/downloads.yml``. The GUI worker
picks up pending jobs and dispatches them to ``hf_models.download_model``.
On crash/restart, jobs in state ``running`` or ``paused`` are re-queued as
``pending``.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

from .hf_models import MODELS_DIR, repo_models_dir
from .server_profile import CONFIG_DIR


QUEUE_PATH = CONFIG_DIR / "downloads.yml"


@dataclass
class DownloadJob:
    repo_id: str
    filename: str
    dst: str = ""
    status: str = "pending"  # pending | running | paused | done | error | cancelled
    progress_bytes: int = 0
    total_bytes: int = 0
    requested_by_preset: Optional[str] = None
    error: str = ""

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return int(100 * self.progress_bytes / self.total_bytes)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DownloadQueue:
    jobs: list[DownloadJob] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls) -> "DownloadQueue":
        q = cls()
        if not QUEUE_PATH.exists():
            return q
        try:
            data = yaml.safe_load(QUEUE_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return q
        for raw in data.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            try:
                job = DownloadJob(**{k: v for k, v in raw.items() if k in DownloadJob.__dataclass_fields__})
                # Recover from interrupted runs
                if job.status in ("running", "paused"):
                    job.status = "pending"
                q.jobs.append(job)
            except Exception:
                continue
        return q

    def save(self) -> None:
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "jobs": [j.to_dict() for j in self.jobs]}
            QUEUE_PATH.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    def find(self, repo_id: str, filename: str) -> Optional[DownloadJob]:
        with self._lock:
            for j in self.jobs:
                if j.repo_id == repo_id and j.filename == filename:
                    return j
        return None

    def enqueue(
        self,
        repo_id: str,
        filename: str,
        *,
        dst: Optional[Path] = None,
        requested_by_preset: Optional[str] = None,
    ) -> DownloadJob:
        existing = self.find(repo_id, filename)
        if existing:
            if existing.status in ("error", "cancelled"):
                existing.status = "pending"
                existing.error = ""
            self.save()
            return existing
        job = DownloadJob(
            repo_id=repo_id,
            filename=filename,
            # Per-repo destination so two repos with a same-named file
            # (notably ``mmproj-BF16.gguf``) never clobber each other.
            dst=str(dst or (repo_models_dir(repo_id) / filename)),
            requested_by_preset=requested_by_preset,
        )
        with self._lock:
            self.jobs.append(job)
        self.save()
        return job

    def update(self, job: DownloadJob) -> None:
        with self._lock:
            for i, j in enumerate(self.jobs):
                if j.repo_id == job.repo_id and j.filename == job.filename:
                    self.jobs[i] = job
                    break
        self.save()

    def remove(self, repo_id: str, filename: str) -> bool:
        with self._lock:
            n = len(self.jobs)
            self.jobs = [j for j in self.jobs if not (j.repo_id == repo_id and j.filename == filename)]
            removed = len(self.jobs) != n
        if removed:
            self.save()
        return removed

    def pending(self) -> list[DownloadJob]:
        with self._lock:
            return [j for j in self.jobs if j.status == "pending"]

    def total_pending_bytes(self) -> int:
        return sum(max(0, j.total_bytes - j.progress_bytes) for j in self.pending())


__all__ = ["DownloadJob", "DownloadQueue", "QUEUE_PATH"]
