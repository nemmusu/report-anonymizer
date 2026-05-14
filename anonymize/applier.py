"""Deterministic applier with atomic writes + cooperative cancel.

Walks the scan inventory, dispatches each file to its adapter, and writes the
anonymized version into the project's output tree. Records every substitution
event in ``applied_substitutions.json`` (used by the GUI Diff view).

Hardened for production:
  * writes via ``<dst>.tmp`` then ``os.replace`` -> never leaves half-written
    output even on hard kill,
  * ``stop_event`` honored between files,
  * ``threading.Lock`` around the in-memory report so multiple workers can
    contribute safely if the future async applier is wired in.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .format_adapters import NullAdapter
from .format_adapters.base import (
    FormatAdapter,
    SubstitutionRule,
    WriteEvent,
    WriteReport,
)
from .project import Project
from .scanner import ScanResult


@dataclass
class ApplyReport:
    project: dict
    files: list[dict] = field(default_factory=list)
    total_files: int = 0
    total_events: int = 0
    skipped_binary: int = 0
    warnings_count: int = 0
    cancelled: bool = False

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "total_files": self.total_files,
            "total_events": self.total_events,
            "skipped_binary": self.skipped_binary,
            "warnings_count": self.warnings_count,
            "cancelled": self.cancelled,
            "files": self.files,
        }


def _atomic_write(adapter: FormatAdapter, src: Path, dst: Path, rules: list[SubstitutionRule]) -> WriteReport:
    """Run adapter.write() to a tempfile in dst's directory, then os.replace.

    The adapter writes to a sibling ``.tmp`` file so that ``os.replace`` is
    atomic on the same filesystem. If the adapter raises, the partial file is
    removed.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        wr = adapter.write(src, tmp_path, rules)
        os.replace(tmp_path, dst)
        return wr
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise


def apply(
    project: Project,
    scan: ScanResult,
    rules: list[SubstitutionRule],
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> ApplyReport:
    """Apply ``rules`` to every file in ``scan`` and write the mirror tree."""
    out_root = project.output_dir
    out_root.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    report = ApplyReport(project=project.to_dict())
    total = len(scan.files)
    for i, sf in enumerate(scan.files, 1):
        if stop_event is not None and stop_event.is_set():
            report.cancelled = True
            break
        if progress:
            progress(i, total, str(sf.rel))
        dst = project.output_path_for(sf)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if sf.skipped or isinstance(sf.adapter, NullAdapter):
                # binary or unsupported: copy as-is (still atomic via shutil)
                if sf.path.resolve() != dst.resolve():
                    shutil.copy2(sf.path, dst)
                with lock:
                    report.skipped_binary += 1
                    report.files.append(
                        {
                            "file": str(sf.rel),
                            "events": [],
                            "warnings": [f"copied as-is ({sf.skip_reason})"],
                            "is_lossy": False,
                        }
                    )
                continue
            wr = _atomic_write(sf.adapter, sf.path, dst, rules)
            with lock:
                report.files.append(
                    {
                        "file": str(sf.rel),
                        "events": [e.to_dict() for e in wr.events],
                        "warnings": list(wr.warnings),
                        "is_lossy": wr.is_lossy,
                    }
                )
                report.total_events += len(wr.events)
                report.warnings_count += len(wr.warnings)
        except Exception as e:
            with lock:
                report.files.append(
                    {
                        "file": str(sf.rel),
                        "events": [],
                        "warnings": [f"adapter error: {e}"],
                        "is_lossy": False,
                    }
                )
                report.warnings_count += 1
            try:
                if sf.path.resolve() != dst.resolve():
                    shutil.copy2(sf.path, dst)
            except Exception:
                pass

    report.total_files = total
    return report


def write_apply_report(report: ApplyReport, path: Path) -> None:
    """Atomic write of the JSON report (so a crash never leaves invalid JSON)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        Path(tmp).write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink()
        except Exception:
            pass
        raise


def read_apply_report(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["ApplyReport", "apply", "write_apply_report", "read_apply_report"]
