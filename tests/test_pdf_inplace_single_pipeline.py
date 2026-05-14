"""End-to-end smoke test for the single-PDF input flow.

Reproduces the user-reported scenario:
  * one PDF passed as input,
  * default output destination looks like a file (``foo.anonymized.pdf``),
  * scan -> apply (no LLM, no critic) using a small map.

Before the fix, ``apply()`` would mkdir ``output_dir`` (i.e. the user-given
"file"), the atomic rename ``<dst>.tmp -> <dst>`` would race against that
directory, and the report ended up with ``adapter error: Is a directory``
plus 0 events.
"""
from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from anonymize.applier import apply
from anonymize.format_adapters.base import SubstitutionRule
from anonymize.project import Project
from anonymize.scanner import scan_path


def _build_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Internal app: AcmeApp Pro\nPhone: +393331234567\n",
        fontname="helv",
        fontsize=12,
    )
    doc.save(str(path))
    doc.close()


def test_single_pdf_dst_with_pdf_suffix_works_end_to_end(tmp_path):
    src = tmp_path / "report.pdf"
    _build_pdf(src)

    # User picks a "file-like" destination right next to the input. This is
    # exactly what the import dialog used to default to and what triggered
    # the regression.
    target = tmp_path / "out_dir" / "report.anonymized.pdf"
    proj = Project.for_single_file(src, target)

    scan = scan_path(src)
    rules = [
        SubstitutionRule(from_="AcmeApp Pro", to="VendorApp Pro", category="brand"),
        SubstitutionRule(from_="+393331234567", to="+393330000001", category="phones"),
    ]
    rep = apply(proj, scan, rules)

    assert rep.cancelled is False
    assert rep.total_files == 1
    # No "Is a directory" warnings.
    for f in rep.files:
        for w in f["warnings"]:
            assert "Is a directory" not in w, w
            assert "È una directory" not in w, w
    assert rep.total_events >= 1, rep.to_dict()

    out = proj.output_dir / proj.single_output_filename
    assert out.exists() and out.is_file()
    assert proj.output_dir.is_dir()

    # Read it back and check the substitutions actually landed in the PDF.
    doc = fitz.open(str(out))
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    assert "AcmeApp Pro" not in text
    assert "+393331234567" not in text
    assert "VendorApp Pro" in text or "+393330000001" in text
