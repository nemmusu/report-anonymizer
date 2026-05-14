"""``pdf_inplace`` adapter must preserve font, color and baseline.

We synthesise a tiny PDF with two spans (a colored bold word and a
regular black word), apply substitutions, then re-open the result and
verify that:

- the substituted spans inherit the original ``font``, ``size`` and
  ``color`` of the source spans (no silent fallback to Helvetica /
  black),
- the original baseline ``origin`` is preserved within a small tolerance.
"""
from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="PyMuPDF is required for this test")

from anonymize.format_adapters.base import SubstitutionRule
from anonymize.format_adapters.pdf_inplace_adapter import PdfInplaceAdapter


def _make_synthetic(path: Path) -> dict:
    """Two spans on a single page; returns the originals' meta for asserts."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    p1 = (50.0, 80.0)
    p2 = (50.0, 120.0)
    page.insert_text(
        p1,
        "AcmeApp Pro",
        fontname="hebo",  # Helvetica Bold
        fontsize=14,
        color=(0.8, 0.0, 0.0),  # red
    )
    page.insert_text(
        p2,
        "+393337310009",
        fontname="cour",  # Courier
        fontsize=11,
        color=(0.0, 0.0, 0.0),  # black
    )
    doc.save(str(path))
    doc.close()
    return {"p1": p1, "p2": p2}


def _spans_by_text(page) -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                t = (sp.get("text") or "").strip()
                if t:
                    out[t] = sp
    return out


def test_pdf_inplace_preserves_font_color_baseline(tmp_path: Path) -> None:
    src = tmp_path / "in.pdf"
    dst = tmp_path / "out.pdf"
    meta = _make_synthetic(src)

    rules = [
        SubstitutionRule(
            from_="AcmeApp Pro", to="AcmeApp Pro", category="brand"
        ),
        SubstitutionRule(
            from_="+393337310009", to="+393440000001", category="phones"
        ),
    ]
    PdfInplaceAdapter().write(src, dst, rules)

    out = fitz.open(str(dst))
    page = out[0]
    spans = _spans_by_text(page)
    assert "AcmeApp Pro" in spans, (
        "the substitution must produce a visible span; available spans: "
        f"{list(spans)}"
    )
    assert "+393440000001" in spans, list(spans)

    brand = spans["AcmeApp Pro"]
    phone = spans["+393440000001"]

    # Color preservation: red brand stays red, black phone stays black.
    # Allow tiny rounding error (PDF colors are 8-bit per channel).
    def _rgb(span):
        c = int(span.get("color") or 0)
        return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)

    br, bg, bb = _rgb(brand)
    pr, pg, pb = _rgb(phone)
    assert br > 150 and bg < 60 and bb < 60, (
        f"brand color should stay red-ish, got rgb=({br},{bg},{bb})"
    )
    assert pr < 30 and pg < 30 and pb < 30, (
        f"phone color should stay black-ish, got rgb=({pr},{pg},{pb})"
    )

    # Baseline preservation: y of the new origin within 1pt of the original.
    bo = brand.get("origin")
    po = phone.get("origin")
    assert bo and abs(bo[1] - meta["p1"][1]) < 1.5, (bo, meta["p1"])
    assert po and abs(po[1] - meta["p2"][1]) < 1.5, (po, meta["p2"])

    # Font heuristic: the brand uses a bold font (helv-bold or its alias).
    bf = (brand.get("font") or "").lower()
    pf = (phone.get("font") or "").lower()
    assert "bold" in bf or bf.startswith("hebo") or bf.startswith("f"), bf
    assert (
        "cour" in pf
        or "mono" in pf
        or pf.startswith("f")
        or "fxn" in pf
    ), pf

    out.close()


def test_pdf_inplace_no_overlap_double_redaction(tmp_path: Path) -> None:
    """Longest-first invariant: AcmeServer + X-AcmeServer-Auth.

    The longer rule must win and the shorter one must NOT also try to
    redact a sub-range of the same span (which would produce a
    second blank box with no insert).
    """
    src = tmp_path / "in.pdf"
    dst = tmp_path / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=500, height=200)
    page.insert_text((40, 80), "X-AcmeServer-Auth: token", fontsize=12)
    doc.save(str(src))
    doc.close()

    rules = [
        SubstitutionRule(
            from_="X-AcmeServer-Auth", to="X-Vendor-Auth", category="headers"
        ),
        SubstitutionRule(
            from_="AcmeServer", to="Vendor", category="brand"
        ),
    ]
    PdfInplaceAdapter().write(src, dst, rules)
    out = fitz.open(str(dst))
    text = out[0].get_text("text") or ""
    out.close()
    assert "X-Vendor-Auth" in text, text
    # The shorter "Vendor" must NOT have been spliced inside the new
    # header (which would produce "X-VVendorVendor-..." / similar mess).
    assert "X-VendorVendor" not in text


def test_replacement_uses_substring_x_not_span_x(tmp_path: Path) -> None:
    """Inserted replacement must use search_for rect x0, not span origin[0].

    A single drawn line ``Phone: +3933...`` has one span whose origin starts
    at the ``P``; the phone match is a substring with a larger x0. Drawing
    at origin[0] would paint over ``Phone:``."""
    src = tmp_path / "in.pdf"
    dst = tmp_path / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=500, height=200)
    line_origin = (50.0, 100.0)
    page.insert_text(
        line_origin,
        "Phone: +393337310009",
        fontname="helv",
        fontsize=12,
    )
    doc.save(str(src))
    doc.close()

    with fitz.open(str(src)) as pre:
        hits = list(pre[0].search_for("+393337310009") or [])
    assert hits, "precondition: substring must be findable"
    substring_x0 = float(hits[0].x0)

    rules = [
        SubstitutionRule(
            from_="+393337310009",
            to="+39999000000",
            category="phones",
        ),
    ]
    PdfInplaceAdapter().write(src, dst, rules)

    out = fitz.open(str(dst))
    page_out = out[0]
    repl_hits = list(page_out.search_for("+39999000000") or [])
    assert repl_hits, "replacement must be findable in output"
    ox = float(repl_hits[0].x0)
    out.close()
    assert abs(ox - substring_x0) < 2.0, (
        f"replacement should start at substring x0≈{substring_x0}, got x0={ox}"
    )
    assert ox > line_origin[0] + 25.0, (
        "wrong baseline would pin near line start and overlap 'Phone:'"
    )
