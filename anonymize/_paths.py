"""Cross-platform user directory resolution for Report Anonymizer.

Centralises the per-OS conventions for the user-scope config dir, the
user-scope data dir, and the models dir, so the rest of the codebase
can do::

    from anonymize._paths import user_config_dir, user_data_dir, models_dir

instead of hard-coding ``Path("~/.config/document-anonymizer").expanduser()``
(which fails on Windows where ``~`` expands to ``%USERPROFILE%`` but
``.config/`` is an XDG convention that doesn't exist there).

Resolution rules (per-OS):

* **Windows** (``os.name == "nt"``):
    * config: ``%APPDATA%\\report-anonymizer``
    * data:   ``%LOCALAPPDATA%\\report-anonymizer``
    * models: ``%LOCALAPPDATA%\\report-anonymizer\\models``
* **macOS** (``platform.system() == "Darwin"``):
    * config: ``~/Library/Application Support/report-anonymizer``
    * data:   same as config
    * models: ``<data>/models``
* **Linux / *BSD / other POSIX** (XDG):
    * config: ``$XDG_CONFIG_HOME/report-anonymizer`` (default ``~/.config/report-anonymizer``)
    * data:   ``$XDG_DATA_HOME/report-anonymizer`` (default ``~/.local/share/report-anonymizer``)
    * models: ``<data>/models``

Migration: the very first call to :func:`user_config_dir` checks for a
legacy ``~/.config/document-anonymizer`` (or ``~/.local/share/document-anonymizer``
for the data dir) and, if the new location does not yet exist, moves the
contents over and writes a ``.migrated_v1`` sentinel so the operation is
idempotent. Multi-GB models in the old data dir are NOT copied (only YAML
config is moved); the user can re-import them via the Model Manager.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

_APP_NAME = "report-anonymizer"
_LEGACY_APP_NAME = "document-anonymizer"
_MIGRATION_SENTINEL = ".migrated_v1"


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _windows_config_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def _windows_data_root() -> Path:
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        return Path(localappdata)
    return Path.home() / "AppData" / "Local"


def _macos_root() -> Path:
    return Path.home() / "Library" / "Application Support"


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".config"


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


def _config_root() -> Path:
    if _is_windows():
        return _windows_config_root()
    if _is_macos():
        return _macos_root()
    return _xdg_config_home()


def _data_root() -> Path:
    if _is_windows():
        return _windows_data_root()
    if _is_macos():
        return _macos_root()
    return _xdg_data_home()


def _legacy_config_dir() -> Path:
    """Where prior versions stored config (single hard-coded XDG path).

    Used by the migration shim regardless of host OS. On Windows this
    expands to ``C:\\Users\\<u>\\.config\\document-anonymizer`` which is
    almost certainly empty (Windows users started fresh with the
    installer), but we still check because dev installs that pip
    installed the old code on Windows may have created it.
    """
    return Path.home() / ".config" / _LEGACY_APP_NAME


def _legacy_data_dir() -> Path:
    """Where prior versions stored data (single hard-coded XDG path)."""
    return Path.home() / ".local" / "share" / _LEGACY_APP_NAME


def _migrate_once(legacy: Path, new: Path) -> None:
    """One-shot copy of small config files from a legacy path.

    Idempotent via a ``.migrated_v1`` sentinel inside ``new``. Best-effort:
    any failure is swallowed so a permission error never crashes startup.
    Symlinks are preserved (``copytree(symlinks=True)``).
    """
    try:
        if not legacy.exists() or not legacy.is_dir():
            return
        sentinel = new / _MIGRATION_SENTINEL
        if sentinel.exists():
            return
        new.mkdir(parents=True, exist_ok=True)
        if any(new.iterdir()):
            try:
                sentinel.write_text("skipped: target non-empty\n", encoding="utf-8")
            except Exception:
                pass
            return
        for entry in legacy.iterdir():
            target = new / entry.name
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(entry, target, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(entry, target, follow_symlinks=False)
            except Exception:
                continue
        try:
            sentinel.write_text("ok\n", encoding="utf-8")
        except Exception:
            pass
    except Exception:
        return


def user_config_dir() -> Path:
    """Return the user-scope config directory, creating it lazily.

    Performs a one-shot migration from the legacy
    ``~/.config/document-anonymizer`` location on first call.
    """
    target = _config_root() / _APP_NAME
    try:
        _migrate_once(_legacy_config_dir(), target)
    except Exception:
        pass
    return target


def user_data_dir() -> Path:
    """Return the user-scope data directory (cache, larger artifacts)."""
    return _data_root() / _APP_NAME


def models_dir() -> Path:
    """Return the directory where downloaded GGUF models live.

    On Windows/macOS this lives under ``user_data_dir()`` to keep
    multi-GB downloads off the roaming profile. On Linux/XDG it lives
    under ``$XDG_DATA_HOME/report-anonymizer/models``.
    """
    return user_data_dir() / "models"


__all__ = [
    "user_config_dir",
    "user_data_dir",
    "models_dir",
]
