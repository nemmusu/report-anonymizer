"""Structured JSONL logger for ``<output>/.anon/logs/<date>.log``.

Every event is a single JSON line so it can be tailed and grep'd. Used by
the pipeline to record stage durations, LLM calls, file events and errors.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class StructuredLogger:
    """Append-only JSONL logger with thread-safe writes.

    Use :meth:`stage` as a context manager to bracket a stage with
    ``stage_start``/``stage_end`` events that include duration_ms.
    """

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def file(self) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"{date}.log"

    def emit(self, event: str, **fields: Any) -> None:
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        for k, v in fields.items():
            try:
                json.dumps(v)
                rec[k] = v
            except Exception:
                rec[k] = repr(v)
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    class _StageCtx:
        def __init__(self, parent: "StructuredLogger", name: str, **fields: Any) -> None:
            self.parent = parent
            self.name = name
            self.fields = fields
            self.t0 = 0.0

        def __enter__(self) -> "StructuredLogger._StageCtx":
            self.t0 = time.monotonic()
            self.parent.emit("stage_start", stage=self.name, **self.fields)
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            duration_ms = int((time.monotonic() - self.t0) * 1000)
            if exc:
                self.parent.emit(
                    "stage_end",
                    stage=self.name,
                    ok=False,
                    error=repr(exc),
                    duration_ms=duration_ms,
                    **self.fields,
                )
            else:
                self.parent.emit(
                    "stage_end",
                    stage=self.name,
                    ok=True,
                    duration_ms=duration_ms,
                    **self.fields,
                )

    def stage(self, name: str, **fields: Any) -> "StructuredLogger._StageCtx":
        return StructuredLogger._StageCtx(self, name, **fields)


__all__ = ["StructuredLogger"]
