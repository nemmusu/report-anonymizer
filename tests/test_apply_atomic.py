"""Atomic write tests for the applier."""
from __future__ import annotations

import json
from pathlib import Path

from anonymize.applier import apply, write_apply_report
from anonymize.format_adapters.base import SubstitutionRule
from anonymize.project import Project
from anonymize.scanner import scan_path


def test_apply_writes_anonymized_output(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("hello Acme world", encoding="utf-8")
    out = tmp_path / "out"
    proj = Project.for_folder(src, out)
    scan = scan_path(src)
    rules = [SubstitutionRule(from_="Acme", to="Vendor-A", category="brand")]
    rep = apply(proj, scan, rules)
    assert (out / "a.md").exists()
    text = (out / "a.md").read_text(encoding="utf-8")
    assert "Vendor-A" in text and "Acme" not in text
    assert rep.total_events >= 1


def test_apply_report_atomic_write(tmp_path: Path) -> None:
    p = tmp_path / "applied.json"
    from anonymize.applier import ApplyReport
    rep = ApplyReport(project={}, total_files=0)
    write_apply_report(rep, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["total_files"] == 0


def test_apply_cancellable_via_stop_event(tmp_path: Path) -> None:
    import threading
    src = tmp_path / "src"
    src.mkdir()
    for i in range(20):
        (src / f"f{i}.md").write_text(f"hello {i}", encoding="utf-8")
    out = tmp_path / "out"
    proj = Project.for_folder(src, out)
    scan = scan_path(src)
    ev = threading.Event()
    ev.set()  # request stop immediately
    rep = apply(proj, scan, [], stop_event=ev)
    assert rep.cancelled
