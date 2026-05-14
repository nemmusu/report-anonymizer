"""Cross-platform process group management.

Wraps the OS-specific incantations needed to (a) launch a child process
in its own process group / job (so it can be reliably reaped together
with all its descendants) and (b) terminate the whole tree on demand.

The rest of the codebase imports just three functions from here::

    from anonymize._proc import (
        spawn_new_process_group,
        terminate_process_tree,
        find_pid_listening_on,
    )

so ``server_manager.py`` doesn't need to know about ``os.killpg``,
``CREATE_NEW_PROCESS_GROUP``, ``CTRL_BREAK_EVENT``, ``/proc/net/tcp``,
or any other Linux/Windows-specific quirk: branching POSIX vs ``nt`` is
all done inside this module.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional


def spawn_new_process_group(
    cmd: list[str], **kwargs: Any
) -> subprocess.Popen:
    """Launch ``cmd`` in its own process group / Win32 job.

    On POSIX (Linux/macOS/BSD) the child is started with
    ``start_new_session=True`` so it leads its own session and process
    group, which lets ``os.killpg`` reap the whole tree.

    On Windows (``os.name == "nt"``) the child is started with
    ``CREATE_NEW_PROCESS_GROUP`` so it can later receive a
    ``CTRL_BREAK_EVENT``. ``CREATE_NO_WINDOW`` is also OR'd in so a
    GUI-spawned ``llama-server`` doesn't pop a console window.

    Extra ``**kwargs`` are forwarded to :class:`subprocess.Popen`. If
    the caller already provides ``creationflags`` or
    ``start_new_session``, our flags are merged additively.
    """
    if os.name == "nt":
        creation_flags = int(kwargs.pop("creationflags", 0))
        creation_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = creation_flags
        kwargs.pop("start_new_session", None)
    else:
        kwargs.setdefault("start_new_session", True)
        kwargs.pop("creationflags", None)
    return subprocess.Popen(cmd, **kwargs)


def _snapshot_descendant_pids(root_pid: int) -> list[int]:
    """Return the PIDs of every descendant of ``root_pid`` *right now*.

    Must be called BEFORE the leader is killed: once the leader exits,
    the OS reparents its orphaned descendants to PID 1 (POSIX) or the
    System Idle Process (Windows), at which point ``parent.children()``
    can no longer find them. Capturing the snapshot up-front is what
    lets :func:`terminate_process_tree` reap orphans on Windows where
    we don't have POSIX session/process-group semantics.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    try:
        parent = psutil.Process(root_pid)
    except (psutil.NoSuchProcess, Exception):
        return []
    try:
        return [p.pid for p in parent.children(recursive=True)]
    except Exception:
        return []


def _kill_pids(pids: list[int]) -> None:
    """Best-effort SIGKILL / TerminateProcess of each PID in ``pids``."""
    if not pids:
        return
    try:
        import psutil  # type: ignore
    except Exception:
        return
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, Exception):
            continue


def terminate_process_tree(
    proc: subprocess.Popen, timeout: float = 5.0
) -> None:
    """Stop ``proc`` and reap every descendant.

    Strategy:

    1. Snapshot every descendant PID *before* signalling anything:
       once the leader is gone the OS reparents orphans to PID 1
       (POSIX) or the System Idle Process (Windows) and they fall
       out of ``parent.children()`` walks. Without this snapshot,
       Windows leaks grandchildren on every stop.
    2. Send a soft signal to the leader: ``CTRL_BREAK_EVENT`` on
       Windows (it works because we spawned the leader with
       ``CREATE_NEW_PROCESS_GROUP``), ``SIGTERM`` to the process
       group on POSIX (works because ``spawn_new_process_group``
       used ``start_new_session=True``).
    3. Wait up to ``timeout`` seconds for the leader to exit.
    4. If still alive, hard-kill it (``TerminateProcess`` /
       ``SIGKILL`` to the process group).
    5. Hard-kill every PID in the snapshot from step 1 (some may
       already be dead; psutil handles ``NoSuchProcess`` for us).

    Returns ``None``; success/failure is opaque (best-effort cleanup).
    """
    if proc is None:
        return
    pid = proc.pid

    descendant_snapshot = _snapshot_descendant_pids(pid)

    if os.name == "nt":
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        try:
            if ctrl_break is not None:
                proc.send_signal(ctrl_break)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                proc.kill()
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
    except Exception:
        pass

    _kill_pids(descendant_snapshot)


def _find_pid_listening_psutil(host: str, port: int) -> Optional[int]:
    """Locate a PID bound to ``host:port`` via psutil (cross-platform)."""
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return None
    except Exception:
        return None
    try:
        host_aliases = {host, "0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}
        for c in conns:
            if c.status != psutil.CONN_LISTEN:
                continue
            laddr = getattr(c, "laddr", None)
            if not laddr:
                continue
            lport = getattr(laddr, "port", None) or (
                laddr[1] if isinstance(laddr, tuple) and len(laddr) > 1 else None
            )
            if lport != port:
                continue
            lip = getattr(laddr, "ip", None) or (
                laddr[0] if isinstance(laddr, tuple) and laddr else ""
            )
            if lip in host_aliases or host in ("0.0.0.0", "::") or not lip:
                if c.pid:
                    return int(c.pid)
    except Exception:
        return None
    return None


def _find_pid_listening_proc(host: str, port: int) -> Optional[int]:
    """Linux-only fallback: parse ``/proc/net/tcp{,6}``.

    Useful when psutil is absent (very stripped test env) or has
    insufficient privileges to enumerate sockets but we can still
    read our own ``/proc`` entries.
    """
    if not Path("/proc/net/tcp").exists():
        return None
    try:
        import socket

        try:
            ipv4 = socket.gethostbyname(host)
        except Exception:
            ipv4 = host
    except Exception:
        ipv4 = host
    port_hex = f"{port:04X}"

    def _match_v4(addr: str) -> bool:
        try:
            ip_part, port_part = addr.split(":")
        except ValueError:
            return False
        if port_part.upper() != port_hex:
            return False
        if len(ip_part) != 8:
            return False
        try:
            octs = [int(ip_part[i : i + 2], 16) for i in (6, 4, 2, 0)]
        except ValueError:
            return False
        ip_str = ".".join(str(o) for o in octs)
        return (
            ip_str == ipv4
            or ipv4 in ("0.0.0.0", "127.0.0.1")
            or ip_str in ("0.0.0.0", "127.0.0.1")
        )

    def _match_v6(addr: str) -> bool:
        try:
            _, port_part = addr.split(":")
        except ValueError:
            return False
        return port_part.upper() == port_hex

    listening_inodes: set[str] = set()
    for tcp, matcher in (
        ("/proc/net/tcp", _match_v4),
        ("/proc/net/tcp6", _match_v6),
    ):
        try:
            lines = Path(tcp).read_text(encoding="utf-8").splitlines()[1:]
        except Exception:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 10:
                continue
            local_addr = cols[1]
            state = cols[3]
            if state != "0A":  # TCP_LISTEN
                continue
            if not matcher(local_addr):
                continue
            listening_inodes.add(cols[9])

    if not listening_inodes:
        return None

    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        if not fd_dir.is_dir():
            continue
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inode = target[len("socket:[") : -1]
                    if inode in listening_inodes:
                        return int(pid_dir.name)
        except (PermissionError, FileNotFoundError):
            continue
    return None


def find_pid_listening_on(host: str, port: int) -> Optional[int]:
    """Return the PID of any process bound to ``host:port``, or ``None``.

    Used by :func:`server_manager.ServerManager.stop` when the manager
    has lost track of its child (``self._proc is None``) but the user
    still expects Stop to terminate whatever is currently bound to the
    configured port.

    Cross-platform: tries psutil first (works on POSIX and Windows),
    falls back to ``/proc/net/tcp`` parsing on Linux when psutil is
    unavailable.
    """
    pid = _find_pid_listening_psutil(host, port)
    if pid is not None:
        return pid
    return _find_pid_listening_proc(host, port)


__all__ = [
    "spawn_new_process_group",
    "terminate_process_tree",
    "find_pid_listening_on",
]
