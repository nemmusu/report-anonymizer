"""Tests for server-process lifecycle: spawn -> stop -> descendant cleanup.

We use a tiny synthetic ``llama-server`` shim (a sleep loop) as the binary so
the tests stay self-contained and don't require llama.cpp to be installed.
The shim is a ``.bat`` (Windows) or ``.sh`` (POSIX) wrapper that exec()s a
shared Python helper (``tests/_fake_llama_server.py``) so the same
behaviour runs on either platform without depending on bash being on PATH.
"""
from __future__ import annotations

import os
import shutil
import signal  # noqa: F401  -- imported for potential future use; harmless
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from anonymize import server_manager as smm
from anonymize.server_profile import ServerProfile


_FAKE_HELPER = Path(__file__).parent / "_fake_llama_server.py"


def _make_fake_binary(tmp_path: Path) -> Path:
    """Materialise a cross-platform fake ``llama-server`` wrapper.

    The wrapper invokes the shared Python helper through ``sys.executable``
    so we don't depend on bash being installed (Windows) or on the helper
    file having an exec bit (POSIX umask).
    """
    if os.name == "nt":
        wrapper = tmp_path / "fake-llama-server.bat"
        # %* forwards every argument server_manager passes (--model,
        # --host, --port, -c, -ngl, ...). The helper happily ignores
        # everything past --children.
        wrapper.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{_FAKE_HELPER}" --children 1 %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = tmp_path / "fake-llama-server"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{_FAKE_HELPER}" --children 1 "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return wrapper


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    """Return True iff ``pid`` is no longer running within ``timeout``.

    Uses ``psutil.pid_exists`` instead of ``os.kill(pid, 0)`` because the
    latter is not portable: on Windows ``signal=0`` is not a valid signal
    and on hosts without PROCESS_QUERY_INFORMATION the call fails for
    unrelated reasons. ``psutil`` papers over both with a single API.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return False


def test_stop_kills_main_process(tmp_path, monkeypatch):
    fake = _make_fake_binary(tmp_path)
    fake_model = tmp_path / "model.gguf"
    fake_model.write_text("not a real gguf", encoding="utf-8")

    prof = ServerProfile(
        name="test",
        binary=str(fake),
        model=str(fake_model),
        host="127.0.0.1",
        port=18181,
        ctx_size=1024,
        n_gpu_layers=0,
        parallel=1,
        flash_attn=False,
        mmap=False,
        no_warmup=True,
        cache_prompt=False,
        no_webui=True,
    )
    mgr = smm.ServerManager(prof)
    monkeypatch.setattr(mgr, "_probe_health", lambda *, timeout=1.0: False)

    mgr.start(wait_seconds=2.0)
    assert mgr.is_running()
    pid = mgr._proc.pid

    mgr.stop(timeout=3.0)
    assert _wait_dead(pid, timeout=3.0), "main pid still alive after stop()"


def test_stop_kills_descendants(tmp_path, monkeypatch):
    fake = _make_fake_binary(tmp_path)
    fake_model = tmp_path / "model.gguf"
    fake_model.write_text("not a real gguf", encoding="utf-8")

    prof = ServerProfile(
        name="test_desc",
        binary=str(fake),
        model=str(fake_model),
        host="127.0.0.1",
        port=18182,
        ctx_size=1024,
        n_gpu_layers=0,
        parallel=1,
        flash_attn=False,
        mmap=False,
        no_warmup=True,
        cache_prompt=False,
        no_webui=True,
    )
    mgr = smm.ServerManager(prof)
    monkeypatch.setattr(mgr, "_probe_health", lambda *, timeout=1.0: False)

    mgr.start(wait_seconds=2.0)
    assert mgr.is_running()
    pid = mgr._proc.pid

    # Give the wrapper time to spawn the helper + grandchild.
    time.sleep(0.8)

    # Cross-platform descendant discovery via psutil (was /proc-only
    # before, which silently passed on Windows). Walk the whole tree
    # below the manager pid; we expect at least one descendant (the
    # grandchild started by _fake_llama_server.py).
    parent = psutil.Process(pid)
    descendants = parent.children(recursive=True)
    assert descendants, (
        f"expected at least one descendant of pid={pid}, got none"
    )
    descendant_pids = [d.pid for d in descendants]

    mgr.stop(timeout=3.0)
    assert _wait_dead(pid, timeout=3.0), "main pid still alive after stop()"
    # Every descendant we saw before stop() must also be reaped.
    for cpid in descendant_pids:
        assert _wait_dead(cpid, timeout=3.0), (
            f"descendant pid={cpid} survived stop() of main pid={pid}"
        )


def test_atexit_cleanup_terminates_running_managers(tmp_path, monkeypatch):
    fake = _make_fake_binary(tmp_path)
    fake_model = tmp_path / "model.gguf"
    fake_model.write_text("not a real gguf", encoding="utf-8")

    prof = ServerProfile(
        name="test_atexit",
        binary=str(fake),
        model=str(fake_model),
        host="127.0.0.1",
        port=18183,
        ctx_size=1024,
        n_gpu_layers=0,
        parallel=1,
        flash_attn=False,
        mmap=False,
        no_warmup=True,
        cache_prompt=False,
        no_webui=True,
    )
    mgr = smm.ServerManager(prof)
    monkeypatch.setattr(mgr, "_probe_health", lambda *, timeout=1.0: False)
    mgr.start(wait_seconds=2.0)
    pid = mgr._proc.pid
    assert mgr.is_running()

    smm._atexit_cleanup()
    assert _wait_dead(pid, timeout=3.0)
