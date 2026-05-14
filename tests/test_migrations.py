from __future__ import annotations

from pathlib import Path

import yaml

from anonymize import migrations as mig


def test_migrate_unknown_kind_returns_data_unchanged() -> None:
    data = {"version": 1, "x": 1}
    out, changed = mig.migrate("unknown_kind", data)
    assert out == data and changed in (True, False)


def test_safe_load_yaml_uses_backup(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yml"
    p.write_text(yaml.safe_dump({"version": 1, "x": 1}), encoding="utf-8")
    out = mig.safe_load_yaml(p, kind="unknown_kind")
    assert isinstance(out, dict)
