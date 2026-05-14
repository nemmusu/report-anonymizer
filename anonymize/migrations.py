"""Schema migrations for YAML/JSON config files.

Each persisted artefact carries an integer ``version`` field. When loading,
we apply ``v_n -> v_{n+1}`` migrations in order. A ``.bak`` of the source is
written before the in-place upgrade so the user can roll back.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import yaml


CURRENT_VERSIONS = {
    "substitution_map": 1,
    "server_profiles": 1,
    "decisions": 1,
    "leak_patterns": 1,
}

MigrationFn = Callable[[dict], dict]
_REGISTRY: dict[str, dict[int, MigrationFn]] = {
    "substitution_map": {},
    "server_profiles": {},
    "decisions": {},
    "leak_patterns": {},
}


def register(kind: str, from_version: int) -> Callable[[MigrationFn], MigrationFn]:
    def deco(fn: MigrationFn) -> MigrationFn:
        _REGISTRY.setdefault(kind, {})[from_version] = fn
        return fn

    return deco


def migrate(kind: str, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply migrations in order until current version. Returns ``(data, changed)``."""
    target = CURRENT_VERSIONS.get(kind, 1)
    current = int(data.get("version") or 1)
    changed = current != target
    while current < target:
        fn = _REGISTRY.get(kind, {}).get(current)
        if fn is None:
            data["version"] = current + 1
        else:
            data = fn(data)
            data["version"] = current + 1
        current += 1
    return data, changed


def safe_load_yaml(path: Path, *, kind: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": CURRENT_VERSIONS.get(kind, 1)}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"version": CURRENT_VERSIONS.get(kind, 1)}
    if not isinstance(data, dict):
        return {"version": CURRENT_VERSIONS.get(kind, 1)}
    new_data, changed = migrate(kind, data)
    if changed:
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            path.write_text(
                yaml.safe_dump(new_data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except Exception:
            pass
    return new_data


__all__ = ["CURRENT_VERSIONS", "register", "migrate", "safe_load_yaml"]
