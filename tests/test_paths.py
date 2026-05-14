"""Cross-platform tests for :mod:`anonymize._paths`.

Validates the three OS branches (Windows / macOS / Linux-XDG) by
monkeypatching the OS predicate helpers (``_is_windows`` /
``_is_macos``) and the relevant environment variables. We avoid
monkeypatching ``os.name`` directly because that breaks
``pathlib.Path`` itself on the host OS (it tries to instantiate the
wrong concrete subclass).

All branches are mocked so the suite stays fully cross-platform
(passes on the Linux CI runner and on the developer's Windows box
without ``skipif`` markers).
"""

from __future__ import annotations

from pathlib import Path

import anonymize._paths as paths


# ---- branch helpers --------------------------------------------------------


def _force_windows(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "Roaming"
    localappdata = tmp_path / "Local"
    appdata.mkdir(parents=True)
    localappdata.mkdir(parents=True)
    monkeypatch.setattr(paths, "_is_windows", lambda: True)
    monkeypatch.setattr(paths, "_is_macos", lambda: False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    return appdata, localappdata


def _force_macos(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(paths, "_is_windows", lambda: False)
    monkeypatch.setattr(paths, "_is_macos", lambda: True)
    monkeypatch.setattr(paths, "_macos_root", lambda: tmp_path / "Library" / "AS")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path / "Library" / "AS"


def _force_linux(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    xdg_cfg = tmp_path / ".config"
    xdg_data = tmp_path / ".local" / "share"
    monkeypatch.setattr(paths, "_is_windows", lambda: False)
    monkeypatch.setattr(paths, "_is_macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return xdg_cfg, xdg_data


# ---- Windows branch --------------------------------------------------------


def test_user_config_dir_windows(monkeypatch, tmp_path):
    appdata, _ = _force_windows(monkeypatch, tmp_path)
    assert paths.user_config_dir() == appdata / "report-anonymizer"


def test_user_data_dir_windows(monkeypatch, tmp_path):
    _, localappdata = _force_windows(monkeypatch, tmp_path)
    assert paths.user_data_dir() == localappdata / "report-anonymizer"


def test_models_dir_windows(monkeypatch, tmp_path):
    _, localappdata = _force_windows(monkeypatch, tmp_path)
    assert (
        paths.models_dir() == localappdata / "report-anonymizer" / "models"
    )


def test_user_config_dir_windows_falls_back_when_appdata_missing(
    monkeypatch, tmp_path
):
    """When ``%APPDATA%`` is unset, fall back to ``~/AppData/Roaming``."""
    monkeypatch.setattr(paths, "_is_windows", lambda: True)
    monkeypatch.setattr(paths, "_is_macos", lambda: False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.user_config_dir() == (
        tmp_path / "AppData" / "Roaming" / "report-anonymizer"
    )


# ---- macOS branch ----------------------------------------------------------


def test_user_config_dir_macos(monkeypatch, tmp_path):
    base = _force_macos(monkeypatch, tmp_path)
    assert paths.user_config_dir() == base / "report-anonymizer"


def test_user_data_dir_macos(monkeypatch, tmp_path):
    base = _force_macos(monkeypatch, tmp_path)
    assert paths.user_data_dir() == base / "report-anonymizer"


def test_models_dir_macos(monkeypatch, tmp_path):
    base = _force_macos(monkeypatch, tmp_path)
    assert paths.models_dir() == base / "report-anonymizer" / "models"


# ---- Linux / XDG branch ---------------------------------------------------


def test_user_config_dir_linux_xdg(monkeypatch, tmp_path):
    xdg_cfg, _ = _force_linux(monkeypatch, tmp_path)
    assert paths.user_config_dir() == xdg_cfg / "report-anonymizer"


def test_user_data_dir_linux_xdg(monkeypatch, tmp_path):
    _, xdg_data = _force_linux(monkeypatch, tmp_path)
    assert paths.user_data_dir() == xdg_data / "report-anonymizer"


def test_models_dir_linux_xdg(monkeypatch, tmp_path):
    _, xdg_data = _force_linux(monkeypatch, tmp_path)
    assert paths.models_dir() == xdg_data / "report-anonymizer" / "models"


def test_linux_default_when_xdg_unset(monkeypatch, tmp_path):
    """Default to ``~/.config`` and ``~/.local/share`` when XDG vars are unset."""
    monkeypatch.setattr(paths, "_is_windows", lambda: False)
    monkeypatch.setattr(paths, "_is_macos", lambda: False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.user_config_dir() == tmp_path / ".config" / "report-anonymizer"
    assert paths.user_data_dir() == tmp_path / ".local" / "share" / "report-anonymizer"


# ---- Migration shim --------------------------------------------------------


def test_migration_one_shot_copies_legacy_config(monkeypatch, tmp_path):
    _force_linux(monkeypatch, tmp_path)
    legacy = tmp_path / ".config" / "document-anonymizer"
    legacy.mkdir(parents=True)
    (legacy / "server.yml").write_text("version: 1\n", encoding="utf-8")
    (legacy / "preferences.yml").write_text(
        "default_profile: foo\n", encoding="utf-8"
    )

    cfg = paths.user_config_dir()

    assert (cfg / "server.yml").read_text(encoding="utf-8") == "version: 1\n"
    assert (cfg / "preferences.yml").read_text(encoding="utf-8").startswith(
        "default_profile:"
    )
    assert (cfg / ".migrated_v1").exists()


def test_migration_idempotent_on_second_call(monkeypatch, tmp_path):
    """Sentinel must guard against re-migration overwriting fresh user edits."""
    _force_linux(monkeypatch, tmp_path)
    legacy = tmp_path / ".config" / "document-anonymizer"
    legacy.mkdir(parents=True)
    (legacy / "server.yml").write_text("legacy\n", encoding="utf-8")

    cfg = paths.user_config_dir()
    assert (cfg / "server.yml").read_text(encoding="utf-8") == "legacy\n"

    (cfg / "server.yml").write_text("user-edit\n", encoding="utf-8")
    paths.user_config_dir()
    assert (cfg / "server.yml").read_text(encoding="utf-8") == "user-edit\n"


def test_migration_skipped_when_target_non_empty(monkeypatch, tmp_path):
    """If the new dir already has files, migration must not overwrite them."""
    xdg_cfg, _ = _force_linux(monkeypatch, tmp_path)

    new = xdg_cfg / "report-anonymizer"
    new.mkdir(parents=True)
    (new / "server.yml").write_text("fresh-install\n", encoding="utf-8")

    legacy = tmp_path / ".config" / "document-anonymizer"
    legacy.mkdir(parents=True)
    (legacy / "server.yml").write_text("legacy\n", encoding="utf-8")

    cfg = paths.user_config_dir()
    assert (cfg / "server.yml").read_text(encoding="utf-8") == "fresh-install\n"


def test_migration_no_legacy_dir_is_noop(monkeypatch, tmp_path):
    _force_linux(monkeypatch, tmp_path)
    cfg = paths.user_config_dir()
    # No legacy dir means the migration shim does nothing visible: in
    # particular it must not create the sentinel (otherwise a future
    # legacy dir appearing later would be ignored).
    assert not (cfg / ".migrated_v1").exists()
