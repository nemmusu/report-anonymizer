"""Windows-only ``subprocess`` patch that suppresses transient console
windows for every CLI tool spawned by the GUI process tree.

Why this file exists
====================

When the Report Anonymizer GUI is launched via ``ReportAnonymizer.exe``
(a Windows GUI-subsystem binary that wraps ``pythonw.exe``) the parent
process has no console attached. As soon as it ``subprocess.run([...])``
or ``subprocess.Popen([...])`` a *console-subsystem* CLI tool such as
``pandoc.exe``, ``pdftotext.exe``, ``soffice.exe``, ``nvidia-smi.exe``
etc., Windows allocates a fresh console window for the child because no
``creationflags`` were specified. Users see this as a black terminal
window flashing open and closing again, possibly several times per
file processed. The fix is to OR ``CREATE_NO_WINDOW`` into the child's
``creationflags`` so Windows starts the child without ever attaching a
console to it.

We already do this in :func:`anonymize._proc.spawn_new_process_group`
for the llama-server child, but the build pipeline shells out to
several other binaries (pandoc via :mod:`pypandoc` and via
:func:`anonymize.builder._run_killable`, ``pdftotext`` from the
:mod:`anonymize.verifier`, libreoffice from the legacy ``.doc`` and
``.pdf`` rederive adapters, etc). Wrapping every individual call site
would be invasive and easy to forget; monkeypatching
``subprocess.Popen.__init__`` once at GUI startup is a single,
auditable change that covers ALL current and future call sites without
having to touch any third-party library.

Behaviour
=========

* Idempotent: :func:`install` is a no-op after the first successful
  call (so importing this module twice or running the test suite that
  imports it doesn't double-wrap).
* Windows-only: on POSIX :func:`install` returns ``False`` immediately,
  no monkeypatching happens.
* Escape hatch: if the caller already passed ``CREATE_NEW_CONSOLE``,
  ``CREATE_NO_WINDOW`` or ``DETACHED_PROCESS`` we leave their flags
  alone. This is what lets debugging code, or tests that explicitly
  *want* a child console, opt out.
* :func:`uninstall` restores the original ``__init__`` (used by tests).
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Optional


_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0

_ORIGINAL_INIT: Optional[Any] = None
_ORIGINAL_OS_SYSTEM: Optional[Any] = None
_ORIGINAL_OS_POPEN: Optional[Any] = None


def _no_window_flag() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))


def _explicit_console_flags() -> int:
    """Bitset of flags that signal "the caller has already chosen a console
    behaviour, do NOT clobber it"."""
    return (
        int(getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW))
        | int(getattr(subprocess, "CREATE_NEW_CONSOLE", _CREATE_NEW_CONSOLE))
        | int(getattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS))
    )


def is_installed() -> bool:
    """Return ``True`` once :func:`install` has wrapped ``Popen.__init__``."""
    return _ORIGINAL_INIT is not None


def _hide_startupinfo(si: Any) -> Any:
    """Return a ``STARTUPINFO`` that has ``STARTF_USESHOWWINDOW`` +
    ``wShowWindow = SW_HIDE`` set. The previous Popen patch only
    OR'd in ``CREATE_NO_WINDOW``, but Windows 11 24H2+ ConPTY
    behaviour can still flash a transient console for sub-processes
    spawned with stdin/stdout pipes when ``startupinfo`` does not
    explicitly request a hidden window. Belt-and-braces hide it.
    """
    try:
        if si is None:
            si = subprocess.STARTUPINFO()
        si.dwFlags = int(getattr(si, "dwFlags", 0)) | _STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_HIDE
    except Exception:
        return si
    return si


def install() -> bool:
    """Wrap ``subprocess.Popen.__init__`` (and ``os.system`` / ``os.popen``)
    so every child started without an explicit console choice gets
    ``CREATE_NO_WINDOW`` plus a ``STARTUPINFO`` with ``SW_HIDE``.

    Returns ``True`` if the patch was applied this call, ``False`` if it
    was a no-op (already installed, or non-Windows host).
    """
    global _ORIGINAL_INIT, _ORIGINAL_OS_SYSTEM, _ORIGINAL_OS_POPEN
    if _ORIGINAL_INIT is not None:
        return False
    if os.name != "nt":
        return False

    no_window = _no_window_flag()
    explicit_mask = _explicit_console_flags()
    original_init = subprocess.Popen.__init__

    def _patched_init(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        flags = int(kwargs.get("creationflags", 0) or 0)
        if not (flags & explicit_mask):
            kwargs["creationflags"] = flags | no_window
        # Force a hidden STARTUPINFO so console-subsystem children
        # do NOT flash a window even on Windows builds where
        # CREATE_NO_WINDOW alone is not enough (ConPTY behaviour
        # on 24H2+). If the caller already supplied one, mutate it
        # in place; otherwise allocate a fresh hidden one.
        kwargs["startupinfo"] = _hide_startupinfo(kwargs.get("startupinfo"))
        return original_init(self, *args, **kwargs)

    _patched_init.__wrapped__ = original_init  # type: ignore[attr-defined]
    _patched_init.__no_window_patch__ = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = _patched_init  # type: ignore[assignment]
    _ORIGINAL_INIT = original_init

    # ``os.system`` and ``os.popen`` ultimately call out to ``cmd /c
    # ...`` which is itself a console-subsystem binary. The native
    # implementation does NOT go through ``subprocess.Popen``, so our
    # patch above doesn't reach it. Reroute both APIs through the
    # patched Popen so a stray ``os.system(...)`` in a third-party
    # library cannot pop a black window either.
    _ORIGINAL_OS_SYSTEM = os.system
    _ORIGINAL_OS_POPEN = os.popen

    def _patched_os_system(command: str) -> int:  # type: ignore[no-untyped-def]
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return int(proc.wait())
        except Exception:
            # Fall back to the original on any failure so we never
            # regress error handling of the stdlib API.
            return _ORIGINAL_OS_SYSTEM(command)  # type: ignore[misc]

    def _patched_os_popen(command, mode="r", buffering=-1):  # type: ignore[no-untyped-def]
        # Best-effort: route through subprocess.Popen (which is now
        # patched). Only the "r"/"w" text-mode contracts are honoured,
        # which matches what every caller in the wild actually uses.
        try:
            if "w" in mode:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    bufsize=buffering,
                    text=True,
                )
                return proc.stdin  # mimic the file-like return of os.popen
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=buffering,
                text=True,
            )
            return proc.stdout
        except Exception:
            return _ORIGINAL_OS_POPEN(command, mode, buffering)  # type: ignore[misc]

    os.system = _patched_os_system  # type: ignore[assignment]
    os.popen = _patched_os_popen  # type: ignore[assignment]
    return True


def uninstall() -> bool:
    """Restore the unpatched ``subprocess.Popen.__init__``. Idempotent."""
    global _ORIGINAL_INIT, _ORIGINAL_OS_SYSTEM, _ORIGINAL_OS_POPEN
    if _ORIGINAL_INIT is None:
        return False
    subprocess.Popen.__init__ = _ORIGINAL_INIT  # type: ignore[assignment]
    _ORIGINAL_INIT = None
    if _ORIGINAL_OS_SYSTEM is not None:
        os.system = _ORIGINAL_OS_SYSTEM  # type: ignore[assignment]
        _ORIGINAL_OS_SYSTEM = None
    if _ORIGINAL_OS_POPEN is not None:
        os.popen = _ORIGINAL_OS_POPEN  # type: ignore[assignment]
        _ORIGINAL_OS_POPEN = None
    return True


__all__ = [
    "install",
    "uninstall",
    "is_installed",
]
