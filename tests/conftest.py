"""pytest fixtures shared across the test suite."""
from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# Run Qt offscreen by default (CI / headless devs)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")

# ---------------------------------------------------------------------------
# Qt WebEngine teardown on Linux CI is the recurring source of
# ``Fatal Python error: Segmentation fault`` during
# ``pytest_runtest_teardown`` (the crashing thread has ``<no Python
# frame>``, the loaded extension modules include ``PySide6.QtWebEngineCore``
# / ``QtWebEngineWidgets``, and Qt's ``QWebEngineProfile`` destructor on
# the offscreen platform deadlocks on the Chromium sandbox helper).
# Tame it with the same flags the Qt + Chromium docs recommend for
# headless / containerised runs:
#   * sandbox off -- the runner already has no setuid binary available
#     for the SUID sandbox helper, so the namespace-sandbox path is
#     the one that crashes during shutdown.
#   * GPU disabled + software rasteriser -- offscreen has no GL
#     context, the GPU process thread is the one that segfaults.
# These have to be set BEFORE QtWebEngine is imported, which happens
# transitively when ``gui.app`` is loaded; conftest is the right
# place. Local developers can override with their own env vars.
# ---------------------------------------------------------------------------
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-software-rasterizer "
    "--disable-dev-shm-usage --in-process-gpu",
)


# ---------------------------------------------------------------------------
# Globally short-circuit ServerManager._probe_health for the WHOLE session.
#
# A test that instantiates a MainWindow (e.g. test_build_*.py) creates a
# ServerPanel whose 1500ms QTimer calls _poll -> health -> _probe_health,
# which fires a real requests.get against the configured llama-server URL.
# On Windows the 1s timeout passed to requests.get is not always honored
# at the socket layer (TCP SYN retransmits stretch a single probe to 21+
# seconds), so a stray timer that survives test teardown can hang the
# next test that calls qapp.processEvents() for tens of seconds.
#
# Doing this as a function-scoped monkeypatch is not enough: pytest's
# monkeypatch reverts BETWEEN tests, and a Qt event loop drain between
# tests can fire the leaked timer with the original (real) method back
# in place. We therefore overwrite the class method exactly once at
# import time -- no fixture, no revert, no race.
# ---------------------------------------------------------------------------
try:
    from anonymize import server_manager as _smm

    def _stub_probe_health(self, *, timeout: float = 1.0) -> bool:
        return False

    _smm.ServerManager._probe_health = _stub_probe_health  # type: ignore[assignment]
except Exception:
    pass


def pytest_sessionfinish(session, exitstatus):  # type: ignore[no-untyped-def]
    """Shield the parent shell from a Qt 6 + PySide6 access violation
    that happens AFTER the last test passes, during the interpreter's
    own unload of the Qt event dispatcher. Concretely:

    * On Windows (pythonw.exe spawned by the Inno launcher) the atexit
      chain returned ``0xC0000005`` reliably.
    * On Linux CI (ubuntu-latest, PySide6 with QtWebEngine loaded) the
      teardown ends with ``"Release of profile requested but
      WebEnginePage still not deleted. Expect troubles !"`` followed by
      ``Segmentation fault (core dumped)`` and the shell sees exit 139.
      The pytest run itself was green; only the Qt destructor chain
      crashed during interpreter shutdown.

    When every collected test has reported success but the interpreter
    is about to crash on teardown, flush stdout and force the process to
    exit cleanly with the pytest exit status, bypassing Python's atexit
    (and thus the Qt teardown) entirely.

    Only fires when the run was logically green and nothing else (xfail
    strict, plugin error, KeyboardInterrupt, etc.) wants a non-zero
    status. Any genuine failure is preserved.
    """
    if exitstatus != 0:
        return
    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return
    except Exception:
        return
    try:
        import sys

        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(terminal, "stats", {}) if terminal else {}
        n_passed = len(stats.get("passed", []))
        n_skipped = len(stats.get("skipped", []))
        n_failed = len(stats.get("failed", []))
        n_error = len(stats.get("error", []))
        summary = (
            f"\n=== {n_passed} passed"
            + (f", {n_skipped} skipped" if n_skipped else "")
            + (f", {n_failed} failed" if n_failed else "")
            + (f", {n_error} error" if n_error else "")
            + " (Qt teardown bypassed) ===\n"
        )
        sys.stdout.write(summary)
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


@pytest.fixture
def tmp_dossier(tmp_path: Path) -> Path:
    """Create a small synthetic dossier with both clean and leaky text."""
    root = tmp_path / "dossier"
    (root / "sub").mkdir(parents=True)
    (root / "README.md").write_text(
        "# Acme Inc. Vulnerability Report\n\nContact: support@acme.example.com\n"
        "Server IP: 10.20.30.40\n",
        encoding="utf-8",
    )
    (root / "sub" / "advisory.md").write_text(
        "## Advisory\n\nPhone: +39 351 123 4567\nAPI key: abcdef1234567890\n",
        encoding="utf-8",
    )
    (root / "ignored.bin").write_bytes(b"\x00\x01\x02\x03 ignored")
    (root / "code.py").write_text(
        "API_HOST = 'internal.acme.example.com'\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    p = tmp_path / "anon-out"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class _Reply:
    parsed: dict | None
    raw: str = ""


class MockLLMClient:
    """Drop-in replacement for ``anonymize.llm_client.LLMClient`` for tests."""

    def __init__(self, *_, **__) -> None:
        self.replies: list[_Reply] = []
        self.health_ok = True

    def _health_url(self) -> str:
        return "http://mock/health"

    def health(self, refresh: bool = False) -> bool:  # noqa: D401
        return self.health_ok

    def chat(self, *_args, stop_event=None, **_kwargs):
        if not self.replies:
            return ({"candidates": []}, '{"candidates":[]}')
        r = self.replies.pop(0)
        return (r.parsed, r.raw or "")

    def chat_many(self, jobs, *, max_workers=None, stop_event=None):
        out = []
        for job in jobs:
            if stop_event is not None and stop_event.is_set():
                out.append((job.tag, None, ""))
                continue
            parsed, raw = self.chat(job.system, job.user)
            out.append((job.tag, parsed, raw))
        return out

    def vote(self, *_args, **_kwargs):
        return ({"candidates": []}, 1.0, [])

    def queue(self, parsed: dict | None, raw: str = "") -> None:
        self.replies.append(_Reply(parsed=parsed, raw=raw))


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def stop_event() -> threading.Event:
    return threading.Event()


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch, tmp_path):
    """Redirect user/global config dirs into a tmp path so tests don't pollute."""
    fake = tmp_path / "user-cfg"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake / ".config"))
    yield


# Note: see the import-time patch above for the rationale behind
# stubbing ``ServerManager._probe_health`` at module load instead of
# via a per-test fixture. ``tests/test_server_lifecycle.py`` overrides
# the stub with its own per-test ``monkeypatch.setattr`` (which always
# wins over a module-level assignment) when it needs the real probe.


@pytest.fixture
def synthetic_apply_report(tmp_output_dir: Path) -> Path:
    """Pre-baked applied_substitutions.json for diff-view tests."""
    import json

    p = tmp_output_dir / "applied_substitutions.json"
    payload = {
        "project": {},
        "total_files": 1,
        "total_events": 2,
        "skipped_binary": 0,
        "warnings_count": 0,
        "files": [
            {
                "file": "README.md",
                "events": [
                    {
                        "rule_id": "brand:0001",
                        "from": "Acme",
                        "to": "Vendor-A",
                        "tier": "T2_human",
                        "category": "brand",
                        "src_offset": 2,
                        "dst_offset": 2,
                    }
                ],
                "warnings": [],
                "is_lossy": False,
            }
        ],
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p
