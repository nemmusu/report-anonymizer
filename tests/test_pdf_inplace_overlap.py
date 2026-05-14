"""Regression tests for the PDF in-place adapter:

* When two substitution rules have overlapping ``from_`` values
  (``X-AcmeServer-Auth`` is a superstring of ``AcmeServer``), the
  adapter must apply only the LONGEST matching rule per region. Otherwise
  both substitutions get rendered on top of each other and the resulting
  PDF shows duplicated/overlapping text.
* The replacement text is actually rendered (not just an empty redaction
  rectangle) and is extractable via ``page.get_text``.
"""
from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from anonymize.format_adapters.base import SubstitutionRule
from anonymize.format_adapters.pdf_inplace_adapter import PdfInplaceAdapter


def _build_pdf(tmp_path, text: str):
    p = tmp_path / "in.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 100), text, fontsize=12, fontname="helv")
    doc.save(str(p))
    doc.close()
    return p


def test_pdf_inplace_replaces_simple_token(tmp_path):
    src = _build_pdf(tmp_path, "Hello AcmeApp World")
    dst = tmp_path / "out.pdf"
    rules = [
        SubstitutionRule(from_="AcmeApp", to="VendorWorld", category="brand"),
    ]
    rep = PdfInplaceAdapter().write(src, dst, rules)
    assert not rep.warnings, rep.warnings
    text = "".join(p.get_text("text") for p in fitz.open(str(dst)))
    assert "AcmeApp" not in text
    assert "VendorWorld" in text


def test_pdf_inplace_overlap_longest_wins(tmp_path):
    """``X-AcmeServer-Auth`` and ``AcmeServer`` overlap; only the
    longest rule should be applied to that region (no duplicated text)."""
    src = _build_pdf(tmp_path, "header X-AcmeServer-Auth set")
    dst = tmp_path / "out.pdf"
    rules = [
        SubstitutionRule(
            from_="X-AcmeServer-Auth", to="X-VendorServer-Auth", category="headers"
        ),
        SubstitutionRule(from_="AcmeServer", to="VendorServer", category="brand"),
    ]
    rep = PdfInplaceAdapter().write(src, dst, rules)
    text = "".join(p.get_text("text") for p in fitz.open(str(dst)))
    assert "X-VendorServer-Auth" in text
    # The standalone "VendorServer" should NOT appear inside the header
    # region (only the longest rule applies). It's OK if it appears elsewhere
    # but the count must match exactly the X-VendorServer-Auth count.
    occ_long = text.count("X-VendorServer-Auth")
    occ_short = text.count("VendorServer")
    # Each X-VendorServer-Auth contains the substring "VendorServer" once;
    # therefore short-count must equal long-count (no extra inserts).
    assert occ_short == occ_long


def test_pdf_inplace_proprietary_uri_replaced(tmp_path):
    """Replacement text is visible after redaction (not an empty
    rectangle), covers a proprietary URI scheme being rewritten to
    a neutral one."""
    src = _build_pdf(tmp_path, "deeplink acme-app:// + dialog")
    dst = tmp_path / "out.pdf"
    rules = [
        SubstitutionRule(from_="acme-app://", to="vapp://", category="network"),
    ]
    PdfInplaceAdapter().write(src, dst, rules)
    text = "".join(p.get_text("text") for p in fitz.open(str(dst)))
    assert "acme-app://" not in text
    assert "vapp://" in text
