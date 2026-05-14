"""Cross-platform tests for :mod:`anonymize._proc`.

* :func:`spawn_new_process_group` is validated by mocking
  :class:`subprocess.Popen` so we can inspect the kwargs the wrapper
  forwarded (``creationflags`` on ``nt``, ``start_new_session`` on
  POSIX).
* :func:`terminate_process_tree` is end-to-end-tested by spawning a
  real ``python -c "import time; time.sleep(60)"`` child and asserting
  it is gone after the call returns. This works on both POSIX and
  Windows without a network or any external binary dependency.

We deliberately do not rely on ``pytest.mark.skipif(sys.platform != ...)``;
the spawn-flag tests use ``monkeypatch.setattr(_proc, "os", ...)`` plus
a Popen mock so both branches run on every CI runner.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types

import pytest

from anonymize import _proc


# ---- spawn_new_process_group: branch validation via Popen mock ------------


class _DummyPopen:
    """Records the kwargs passed to ``subprocess.Popen``."""

    instances: list["_DummyPopen"] = []

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 12345
        type(self).instances.append(self)


@pytest.fixture(autouse=True)
def _reset_dummy():
    _DummyPopen.instances.clear()
    yield
    _DummyPopen.instances.clear()


def test_spawn_new_process_group_nt_sets_creation_flags(monkeypatch):
    monkeypatch.setattr(_proc, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        _proc.subprocess, "Popen", _DummyPopen, raising=True
    )
    expected_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    expected_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # On Linux CI runners ``subprocess`` lacks the Win-only creation
    # flag constants, so ``_proc.spawn_new_process_group`` would OR in
    # 0 (the ``getattr`` default) and the flag-bit assertion below would
    # fail. Plant them on the ``subprocess`` module the unit under test
    # actually sees so the nt branch can be validated cross-platform.
    monkeypatch.setattr(
        _proc.subprocess, "CREATE_NEW_PROCESS_GROUP", expected_flag, raising=False
    )
    monkeypatch.setattr(
        _proc.subprocess, "CREATE_NO_WINDOW", expected_no_window, raising=False
    )

    proc = _proc.spawn_new_process_group(["x"])

    assert isinstance(proc, _DummyPopen)
    flags = proc.kwargs["creationflags"]
    assert flags & expected_flag, "CREATE_NEW_PROCESS_GROUP must be OR'd in"
    if expected_no_window:
        assert flags & expected_no_window, "CREATE_NO_WINDOW should be OR'd in"
    assert "start_new_session" not in proc.kwargs


def test_spawn_new_process_group_nt_merges_existing_flags(monkeypatch):
    monkeypatch.setattr(_proc, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(_proc.subprocess, "Popen", _DummyPopen, raising=True)
    expected_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    expected_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(
        _proc.subprocess, "CREATE_NEW_PROCESS_GROUP", expected_flag, raising=False
    )
    monkeypatch.setattr(
        _proc.subprocess, "CREATE_NO_WINDOW", expected_no_window, raising=False
    )

    proc = _proc.spawn_new_process_group(["x"], creationflags=0x4)

    flags = proc.kwargs["creationflags"]
    assert flags & 0x4, "user-supplied flags should be preserved"
    assert flags & expected_flag


def test_spawn_new_process_group_posix_sets_start_new_session(monkeypatch):
    monkeypatch.setattr(_proc, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(_proc.subprocess, "Popen", _DummyPopen, raising=True)

    proc = _proc.spawn_new_process_group(["x"])

    assert proc.kwargs.get("start_new_session") is True
    assert "creationflags" not in proc.kwargs


def test_spawn_new_process_group_posix_explicit_kw_wins(monkeypatch):
    """Caller-supplied ``start_new_session`` must NOT be overwritten."""
    monkeypatch.setattr(_proc, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(_proc.subprocess, "Popen", _DummyPopen, raising=True)

    proc = _proc.spawn_new_process_group(["x"], start_new_session=False)

    assert proc.kwargs["start_new_session"] is False


# ---- terminate_process_tree: end-to-end on a real sleeper -----------------


def _spawn_sleeper() -> subprocess.Popen:
    """Spawn a long-sleeping Python child using the real spawn helper."""
    return _proc.spawn_new_process_group(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import psutil  # type: ignore
        except Exception:
            return False
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def test_terminate_process_tree_kills_sleeper():
    proc = _spawn_sleeper()
    pid = proc.pid
    assert _is_pid_alive(pid), "child must be alive before terminate"
    _proc.terminate_process_tree(proc, timeout=5.0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            break
        time.sleep(0.1)
    assert not _is_pid_alive(pid), "child must be reaped after terminate"


def test_terminate_process_tree_handles_already_dead(monkeypatch):
    """Calling on a process that is already gone must not raise."""
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5.0)
    _proc.terminate_process_tree(proc, timeout=2.0)


# ---- find_pid_listening_on -----------------------------------------------


def test_find_pid_listening_on_returns_none_on_unbound_port():
    """A very high, unlikely-to-be-used port should yield ``None``."""
    assert _proc.find_pid_listening_on("127.0.0.1", 59137) is None


def test_find_pid_listening_on_psutil_path(monkeypatch):
    """Mock psutil.net_connections to fake a listening socket."""
    fake_pid = 98765

    class _FakeAddr(tuple):
        @property
        def ip(self):
            return self[0]

        @property
        def port(self):
            return self[1]

    fake_conn = types.SimpleNamespace(
        status="LISTEN",
        laddr=_FakeAddr(("127.0.0.1", 18765)),
        pid=fake_pid,
    )

    fake_psutil = types.SimpleNamespace(
        CONN_LISTEN="LISTEN",
        AccessDenied=PermissionError,
        net_connections=lambda kind="inet": [fake_conn],
    )

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert _proc.find_pid_listening_on("127.0.0.1", 18765) == fake_pid


def test_find_pid_listening_on_psutil_returns_none_when_module_missing(
    monkeypatch,
):
    """When psutil import fails AND /proc is absent, return ``None``.

    On Linux runners we still might find ``/proc/net/tcp`` and walk it
    successfully (yielding ``None`` for an unbound port). On Windows
    ``/proc`` does not exist and the function gracefully returns
    ``None``.
    """
    monkeypatch.setitem(sys.modules, "psutil", None)
    # 0 is a sentinel "all interfaces"; nothing should be listening.
    assert _proc.find_pid_listening_on("127.0.0.1", 59138) is None
