"""Regression tests for single-file Project output normalisation.

History: when the user picked a destination that *looks like a file*
(e.g. ``foo.anonymized.pdf``) for a single-file run, ``Project.output_dir``
ended up pointing at that file. The applier then ``mkdir``-ed the path,
turned the destination into a directory, and the atomic
``<dst>.tmp -> <dst>`` rename failed with "Is a directory".

These tests pin the contract that, no matter what shape the user gave
``dst_dir``, ``output_dir`` is *always* an actual directory and the desired
output basename is carried separately in ``single_output_filename``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize.project import Project


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def _src(tmp_path: Path) -> Path:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4\n%dummy\n")
    return p


def test_single_dst_none_uses_sibling_directory(tmp_path):
    src = _src(tmp_path)
    proj = Project.for_single_file(src)
    assert proj.mode == "single"
    assert proj.output_dir == src.parent
    assert proj.single_output_filename == "report.anonymized.pdf"


def test_single_dst_with_file_suffix_is_split(tmp_path):
    src = _src(tmp_path)
    target = tmp_path / "out_dir" / "report.anonymized.pdf"
    proj = Project.for_single_file(src, target)
    assert proj.output_dir == target.parent.resolve()
    assert proj.single_output_filename == "report.anonymized.pdf"


def test_single_dst_with_directory_path_keeps_directory(tmp_path):
    src = _src(tmp_path)
    target = tmp_path / "out_dir"
    target.mkdir()
    proj = Project.for_single_file(src, target)
    assert proj.output_dir == target.resolve()
    assert proj.single_output_filename == "report.anonymized.pdf"


def test_single_dst_with_extensionless_path_creates_directory(tmp_path):
    src = _src(tmp_path)
    target = tmp_path / "future_dir"
    proj = Project.for_single_file(src, target)
    assert proj.output_dir == target.resolve()
    assert proj.single_output_filename == "report.anonymized.pdf"


def test_single_dst_with_pseudo_anonymized_suffix_is_directory(tmp_path):
    """``foo.anonymized`` (no real extension) is the project DIR, not a file."""
    src = _src(tmp_path)
    target = tmp_path / "report.anonymized"
    proj = Project.for_single_file(src, target)
    assert proj.output_dir == target.resolve()
    assert proj.single_output_filename == "report.anonymized.pdf"


def test_single_dst_with_unrelated_suffix_is_treated_as_file(tmp_path):
    """A path that ends with an obvious file extension is split."""
    src = _src(tmp_path)
    target = tmp_path / "renamed.pdf"
    proj = Project.for_single_file(src, target)
    assert proj.output_dir == target.parent.resolve()
    assert proj.single_output_filename == "renamed.pdf"


def test_output_path_for_uses_single_output_filename(tmp_path):
    src = _src(tmp_path)
    proj = Project.for_single_file(src, tmp_path / "out" / "renamed.pdf")
    out = proj.output_path_for(_FakeScanned(src))
    assert out.name == "renamed.pdf"
    assert out.parent == proj.output_dir


# ---------------------------------------------------------------------------
# end-to-end with the deterministic applier (no LLM)
# ---------------------------------------------------------------------------


def test_apply_writes_anonymized_file_inside_directory(tmp_path):
    """``apply()`` must NOT clobber ``output_dir`` with the destination file."""
    src = tmp_path / "doc.txt"
    src.write_text("hello AcmeApp world\n", encoding="utf-8")

    target = tmp_path / "out" / "doc.anonymized.txt"
    proj = Project.for_single_file(src, target)

    from anonymize.scanner import scan_path
    from anonymize.applier import apply
    from anonymize.format_adapters.base import SubstitutionRule

    scan = scan_path(src)
    rules = [SubstitutionRule(from_="AcmeApp", to="Vendor", category="brand")]
    rep = apply(proj, scan, rules)

    assert rep.total_files == 1
    assert rep.total_events >= 1, f"no substitutions applied: {rep.to_dict()}"
    out = proj.output_dir / proj.single_output_filename
    assert out.exists() and out.is_file()
    assert "Vendor" in out.read_text(encoding="utf-8")
    assert "AcmeApp" not in out.read_text(encoding="utf-8")
    # output_dir really is a directory after apply()
    assert proj.output_dir.is_dir()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeScanned:
    def __init__(self, src: Path) -> None:
        self.path = src
        self.rel = Path(src.name)
