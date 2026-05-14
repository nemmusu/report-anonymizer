"""Tests for the Windows-only ``CREATE_NO_WINDOW`` monkeypatch.

The patch wraps :class:`subprocess.Popen.__init__` so every CLI tool
spawned by the GUI process tree gets ``CREATE_NO_WINDOW`` ORed into
its ``creationflags`` automatically. We verify:

* idempotency (install twice == install once)
* a real ``cmd.exe`` ``exit 0`` child still works after the patch
* the ``creationflags`` kwarg actually carries ``CREATE_NO_WINDOW``
  after the patch (introspected via a sentinel ``Popen`` subclass)
* the escape hatch: explicit ``CREATE_NEW_CONSOLE`` is NOT clobbered

All of the assertions are valid only on Windows; the tests gracefully
skip on POSIX. The patch installation itself is also exercised on
POSIX (it must be a no-op there, never a crash).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from gui import _win_subprocess_patch as patch


_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010
_DETACHED_PROCESS = 0x00000008


@pytest.fixture(autouse=True)
def _restore_subprocess_popen():
    """Make sure no test leaks a patched ``Popen.__init__`` to the
    next test (the patch is process-global)."""
    yield
    patch.uninstall()


def test_install_is_idempotent_on_windows():
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    assert patch.install() is True
    assert patch.is_installed() is True
    assert patch.install() is False, "second install should be a no-op"
    assert patch.is_installed() is True


def test_install_is_noop_on_posix():
    if os.name == "nt":
        pytest.skip("non-Windows behaviour")
    assert patch.install() is False
    assert patch.is_installed() is False


def test_uninstall_restores_unpatched_init():
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    pristine = subprocess.Popen.__init__
    assert patch.install() is True
    patched = subprocess.Popen.__init__
    assert patched is not pristine
    assert patch.uninstall() is True
    assert subprocess.Popen.__init__ is pristine
    assert patch.is_installed() is False


def test_real_cmd_exit_zero_still_works_after_patch():
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")
    patch.install()
    rc = subprocess.run(
        ["cmd.exe", "/c", "exit", "0"],
        capture_output=True,
    ).returncode
    assert rc == 0


def test_patch_or_s_create_no_window_into_default_flags():
    """A ``subprocess.Popen`` call with no ``creationflags`` must pick
    up ``CREATE_NO_WINDOW`` after :func:`install`. We introspect the
    actual flags by wrapping the original ``__init__`` with a sentinel
    that records the kwargs the patched layer forwarded."""
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")

    captured: dict[str, int] = {}
    real_init = subprocess.Popen.__init__

    def _spy_init(self, *args, **kwargs):
        # The patched layer rewrote kwargs before calling the real
        # init -- this spy is the real init from the OS perspective.
        captured["creationflags"] = int(kwargs.get("creationflags", 0) or 0)
        return real_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _spy_init  # type: ignore[assignment]
    try:
        # NOTE: install() must run with our spy already in place so
        # the patched __init__ wraps the spy (not the OEM init).
        # Otherwise the patched code would forward to the OEM init
        # and the spy would never run. install() captures the
        # currently-installed __init__ as ``original_init``.
        assert patch.install() is True
        try:
            subprocess.run(
                ["cmd.exe", "/c", "exit", "0"], capture_output=True
            )
        finally:
            patch.uninstall()
    finally:
        subprocess.Popen.__init__ = real_init  # type: ignore[assignment]

    assert captured.get("creationflags", 0) & _CREATE_NO_WINDOW, (
        "after install() every Popen with no explicit creationflags "
        "should have CREATE_NO_WINDOW ORed in"
    )


def test_patch_does_not_clobber_explicit_create_new_console():
    """If the caller explicitly passes ``CREATE_NEW_CONSOLE`` (i.e.
    "I really want a console for this child"), the patch must not
    override that choice -- it's the documented escape hatch."""
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")

    captured: dict[str, int] = {}
    real_init = subprocess.Popen.__init__

    def _spy_init(self, *args, **kwargs):
        captured["creationflags"] = int(kwargs.get("creationflags", 0) or 0)
        return real_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _spy_init  # type: ignore[assignment]
    try:
        assert patch.install() is True
        try:
            subprocess.run(
                ["cmd.exe", "/c", "exit", "0"],
                capture_output=True,
                creationflags=_CREATE_NEW_CONSOLE,
            )
        finally:
            patch.uninstall()
    finally:
        subprocess.Popen.__init__ = real_init  # type: ignore[assignment]

    flags = captured.get("creationflags", 0)
    assert flags & _CREATE_NEW_CONSOLE, "user-supplied flag must survive"
    assert not (flags & _CREATE_NO_WINDOW), (
        "CREATE_NO_WINDOW must NOT be added when the caller explicitly "
        "asked for CREATE_NEW_CONSOLE"
    )


def test_patch_does_not_clobber_existing_create_no_window():
    """When the caller has already set ``CREATE_NO_WINDOW`` (e.g.
    via :func:`anonymize._proc.spawn_new_process_group`), the patch
    must be idempotent -- no double-OR, no flag clobber."""
    if os.name != "nt":
        pytest.skip("Windows-only behaviour")

    captured: dict[str, int] = {}
    real_init = subprocess.Popen.__init__

    def _spy_init(self, *args, **kwargs):
        captured["creationflags"] = int(kwargs.get("creationflags", 0) or 0)
        return real_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _spy_init  # type: ignore[assignment]
    try:
        patch.install()
        try:
            subprocess.run(
                ["cmd.exe", "/c", "exit", "0"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        finally:
            patch.uninstall()
    finally:
        subprocess.Popen.__init__ = real_init  # type: ignore[assignment]

    flags = captured.get("creationflags", 0)
    assert flags == _CREATE_NO_WINDOW, (
        "CREATE_NO_WINDOW already set: patch should leave the flags untouched"
    )
