"""Tiny user-scope settings store backed by a YAML file.

Lives at ``~/.config/document-anonymizer/app_settings.yml`` so it
shares a directory with the existing per-user state (``server.yml``,
``hf.token``, ``downloads.yml``). Intentionally minimal: load + save
+ a couple of typed accessors. Use this for small UX prefs (e.g.
``autostart_server``); long-lived structured config still belongs in
the per-domain YAMLs (``server.yml``, ``profiles.yml``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ._paths import user_config_dir as _user_config_dir


_CONFIG_DIR = _user_config_dir()
_SETTINGS_PATH = _CONFIG_DIR / "app_settings.yml"


def _load_raw() -> dict[str, Any]:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(data: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_suffix(".yml.tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    os.replace(tmp, _SETTINGS_PATH)


def get_bool(key: str, default: bool = False) -> bool:
    raw = _load_raw().get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def set_bool(key: str, value: bool) -> None:
    data = _load_raw()
    data[key] = bool(value)
    _save_raw(data)


def get_str(key: str, default: str = "") -> str:
    """Read a string preference; falls back to ``default`` if missing
    or unparseable."""
    raw = _load_raw().get(key, default)
    if isinstance(raw, str):
        return raw
    if raw is None:
        return default
    try:
        return str(raw)
    except Exception:
        return default


def set_str(key: str, value: str) -> None:
    data = _load_raw()
    data[key] = str(value)
    _save_raw(data)


__all__ = ["get_bool", "set_bool", "get_str", "set_str"]
