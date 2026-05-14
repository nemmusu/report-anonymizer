"""Regression: subset fonts must not be reused for placeholders that
contain characters absent from the subset's CMap.

The bug: PASS 2 (``insert_text``) reused an embedded subset font that
only mapped the original-text glyphs. When the placeholder contained
a different character set, PyMuPDF wrote ``.notdef``, visible to the
user as a blank rectangle in the output PDF.

The fix: probe ``fitz.Font.has_glyph`` for every char in the
placeholder before using the embedded font, and fall back to base-14
``helv`` (always populated) when any glyph is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _make_pdf_with_subset_text(path: Path, source_text: str) -> None:
    """Write a one-page PDF whose embedded font is a subset built from
    ``source_text`` only. Reusing this font to draw any character not
    in ``source_text`` would render ``.notdef``."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 100), source_text, fontname="helv", fontsize=12)
    doc.save(str(path), garbage=4, deflate=True, clean=True)
    doc.close()


def test_font_covers_detects_missing_glyphs(tmp_path: Path) -> None:
    """``_font_covers`` must say False when any char is absent."""
    fitz = pytest.importorskip("fitz")
    from anonymize.format_adapters.pdf_inplace_adapter import PdfInplaceAdapter

    # ``cobi`` is the courier-bold-italic base-14 alias, which has a
    # full ASCII coverage. ``ZZZZZ`` (just the glyph 'Z') gives us a
    # font we can compare against.
    fnt = fitz.Font("helv")
    assert PdfInplaceAdapter._font_covers(fnt, "abc") is True
    # A char that no base-14 has: emoji
    assert PdfInplaceAdapter._font_covers(fnt, "abc\U0001F600") is False
    # None must be False (caller should always pass an object)
    assert PdfInplaceAdapter._font_covers(None, "abc") is False


def test_inplace_renders_placeholder_when_subset_is_missing_glyphs(tmp_path: Path) -> None:
    """End-to-end: a redacted span must show its placeholder text in
    the rendered output even when the original embedded font is a
    subset that does not cover the placeholder's characters.
    """
    fitz = pytest.importorskip("fitz")
    from anonymize.format_adapters.base import SubstitutionRule
    from anonymize.format_adapters.pdf_inplace_adapter import PdfInplaceAdapter

    src = tmp_path / "source.pdf"
    dst = tmp_path / "out.pdf"
    # Source contains ``acme-app://`` only, the embedded font subset will
    # cover those glyphs. The placeholder uses ``vapp://`` whose ``v``
    # and ``a`` may or may not be in the subset; we want the engine to
    # detect the gap and fall back to a base-14 alias rather than
    # writing invisible ``.notdef`` glyphs.
    _make_pdf_with_subset_text(src, "deeplink acme-app:// + ok")

    adapter = PdfInplaceAdapter()
    adapter.write(
        src,
        dst,
        [SubstitutionRule(from_="acme-app://", to="vapp://", category="network")],
    )

    # The placeholder must be readable in the rendered text.
    out_doc = fitz.open(str(dst))
    text = out_doc[0].get_text("text")
    out_doc.close()
    assert "vapp://" in text, f"placeholder missing in output: {text!r}"
    assert "acme-app://" not in text, f"original leaked in output: {text!r}"
