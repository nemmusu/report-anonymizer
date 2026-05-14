"""Round-trip integration tests for the PDF image-redaction path.

Strategy: synthesise a tiny PDF with one or two embedded raster
images at known positions, run the adapter inventory, run the
adapter apply, reopen the output and assert:

* every image_id from the input inventory is still present in the
  output at the same xref / page / dimensions ("nothing lost or
  moved");
* an image with a ``redact`` decision has its bytes changed in the
  expected region (a black pixel where the operator drew a
  blackout rect);
* an image with a ``skip`` decision has its bytes byte-for-byte
  equal in the output ("identity preserved").

The fixtures are built ad-hoc per test using PyMuPDF + PIL, so we
do not need to ship binary PDF blobs in the repo.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import fitz  # type: ignore
import pytest
from PIL import Image

from anonymize.format_adapters.pdf_inplace_adapter import PdfInplaceAdapter
from anonymize.image_inventory import (
    ImageDecision,
    RedactionRect,
    compute_image_id,
)


def _make_pdf_with_image(
    pdf_path: Path,
    image_bytes: bytes,
    *,
    page_count: int = 1,
    image_pos: tuple[float, float, float, float] = (50.0, 50.0, 250.0, 250.0),
) -> None:
    """Build a single-page PDF containing one embedded raster image.

    ``page_count > 1`` repeats the SAME image on each page (logo
    scenario) so we can validate the global-per-image-id semantics.
    """
    doc = fitz.open()
    for _ in range(page_count):
        page = doc.new_page(width=400, height=400)
        page.insert_image(fitz.Rect(*image_pos), stream=image_bytes)
    doc.save(str(pdf_path))
    doc.close()


def _png_solid(color: tuple[int, int, int], size: tuple[int, int] = (200, 200)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _read_pixel(pdf_path: Path, page_index: int, x: int, y: int) -> tuple[int, int, int]:
    """Sample a pixel from a rasterised page at 1:1 scale."""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_index]
        pix = page.get_pixmap(alpha=False)
        try:
            r = pix.pixel(x, y)
        except Exception:
            r = (0, 0, 0)
        return (int(r[0]), int(r[1]), int(r[2]))
    finally:
        doc.close()


def test_inventory_lists_one_image_with_correct_metadata(tmp_path: Path) -> None:
    src = _png_solid((220, 30, 30))
    pdf = tmp_path / "one.pdf"
    _make_pdf_with_image(pdf, src)

    adapter = PdfInplaceAdapter()
    inv = adapter.inventory_images(pdf)
    assert len(inv) == 1
    item = inv[0]
    assert item.fmt in ("png", "jpeg")
    assert item.width == 200 and item.height == 200
    assert item.location["kind"] == "pdf"
    assert item.location["page_index"] == 0
    assert item.location["xref"] > 0
    assert item.location["bbox"] is not None
    assert len(item.raw_bytes) > 0


def test_skip_decision_keeps_image_byte_identical(tmp_path: Path) -> None:
    src = _png_solid((10, 200, 10))
    pdf = tmp_path / "skip.pdf"
    _make_pdf_with_image(pdf, src)

    adapter = PdfInplaceAdapter()
    inv = adapter.inventory_images(pdf)
    assert len(inv) == 1
    image_id = compute_image_id(inv[0].raw_bytes)

    decisions = {image_id: ImageDecision(image_id=image_id, decision="skip")}
    report = adapter.apply_image_redactions(pdf, decisions)
    assert report.applied == 0
    assert report.skipped == 1

    inv2 = PdfInplaceAdapter().inventory_images(pdf)
    assert len(inv2) == 1
    # Bytes must be identical for a skip decision.
    assert compute_image_id(inv2[0].raw_bytes) == image_id


def test_redact_blackout_changes_pixels_inside_rect_only(tmp_path: Path) -> None:
    src = _png_solid((220, 30, 30))     # solid red 200x200
    pdf = tmp_path / "redact.pdf"
    _make_pdf_with_image(pdf, src)

    adapter = PdfInplaceAdapter()
    inv = adapter.inventory_images(pdf)
    image_id = compute_image_id(inv[0].raw_bytes)
    decisions = {
        image_id: ImageDecision(
            image_id=image_id,
            decision="redact",
            image_w=200, image_h=200,
            rects=[RedactionRect(x=50, y=50, w=100, h=100, tool="blackout")],
        )
    }
    report = adapter.apply_image_redactions(pdf, decisions)
    assert report.applied == 1
    assert report.skipped == 0

    inv2 = PdfInplaceAdapter().inventory_images(pdf)
    assert len(inv2) == 1
    new_bytes = inv2[0].raw_bytes
    new_img = Image.open(BytesIO(new_bytes))
    new_img.load()
    # Inside the blackout rect: pure black.
    assert new_img.getpixel((100, 100)) == (0, 0, 0)
    assert new_img.getpixel((50, 50)) == (0, 0, 0)
    # Outside the rect: still red.
    assert new_img.getpixel((10, 10)) == (220, 30, 30)
    assert new_img.getpixel((180, 180)) == (220, 30, 30)
    # Dimensions preserved.
    assert new_img.size == (200, 200)


def test_logo_on_multiple_pages_redacted_with_one_decision(tmp_path: Path) -> None:
    src = _png_solid((30, 200, 30))     # green logo
    pdf = tmp_path / "logo.pdf"
    _make_pdf_with_image(pdf, src, page_count=3)

    adapter = PdfInplaceAdapter()
    inv = adapter.inventory_images(pdf)
    # All 3 pages embed THE SAME image bytes -> same image_id, same xref.
    image_ids = {compute_image_id(im.raw_bytes) for im in inv}
    assert len(image_ids) == 1
    image_id = next(iter(image_ids))

    decisions = {
        image_id: ImageDecision(
            image_id=image_id,
            decision="redact",
            rects=[RedactionRect(x=0, y=0, w=200, h=50, tool="blackout")],
        )
    }
    report = adapter.apply_image_redactions(pdf, decisions)
    assert report.applied >= 1

    # Verify ALL pages render the redacted image with a black band.
    doc = fitz.open(str(pdf))
    try:
        for pi in range(3):
            page = doc[pi]
            pix = page.get_pixmap(alpha=False)
            # Sample a pixel that should fall inside the blackout band
            # of the embedded image. Image is rendered at the rect
            # (50,50)-(250,250) on a 400x400 page; band y=0..50 of the
            # 200x200 image maps to PDF y=50..62.5 ish at default DPI.
            # Use the page's image_rects to compute the actual position.
            rects = page.get_image_rects(page.get_images()[0][0])
            r = rects[0]
            # Band is the top 25% of the image rect.
            sample_x = int((r.x0 + r.x1) / 2)
            sample_y = int(r.y0 + (r.y1 - r.y0) * 0.05)
            color = pix.pixel(sample_x, sample_y)
            assert color[0] < 30 and color[1] < 30 and color[2] < 30, (
                f"page {pi}: expected black band, got {color}"
            )
    finally:
        doc.close()


def test_decisions_dict_input_form_tolerated(tmp_path: Path) -> None:
    """The adapter accepts both ImageDecision instances and raw dicts.

    The pipeline boxes decisions before passing, but tests / scripts
    may pass dicts directly; we want a best-effort round-trip.
    """
    src = _png_solid((100, 100, 220))
    pdf = tmp_path / "dict_input.pdf"
    _make_pdf_with_image(pdf, src)

    adapter = PdfInplaceAdapter()
    inv = adapter.inventory_images(pdf)
    image_id = compute_image_id(inv[0].raw_bytes)

    decisions = {
        image_id: {
            "decision": "redact",
            "rects": [{"x": 10, "y": 10, "w": 50, "h": 50, "tool": "blackout"}],
        }
    }
    report = adapter.apply_image_redactions(pdf, decisions)
    assert report.applied == 1


def test_no_decisions_for_file_is_no_op(tmp_path: Path) -> None:
    src = _png_solid((50, 50, 50))
    pdf = tmp_path / "empty_dec.pdf"
    _make_pdf_with_image(pdf, src)
    before = pdf.read_bytes()
    report = PdfInplaceAdapter().apply_image_redactions(pdf, {})
    after = pdf.read_bytes()
    assert report.applied == 0
    assert report.skipped == 0
    # Even at the byte level the PDF should be untouched.
    assert before == after
