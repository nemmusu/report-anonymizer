"""Unit tests for the pure-pixel ImageRedactor primitives."""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from anonymize.image_redactor import ImageRedaction, ImageRedactor


def _png_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    img.load()
    return img


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> bytes:
    return _png_bytes(Image.new("RGB", size, color))


def test_no_rects_roundtrip_byte_identical() -> None:
    """An empty rect list must NOT touch the bytes (skip-path)."""
    src = _solid((200, 200, 200))
    out = ImageRedactor.redact_bytes(src, "png", [])
    assert out.bytes_ == src
    assert out.fmt_out == "png"
    assert out.warnings == []


def test_blackout_paints_solid_black_inside_rect_only() -> None:
    src = _solid((255, 0, 0), size=(40, 40))
    out = ImageRedactor.redact_bytes(
        src, "png", [ImageRedaction(x=10, y=10, w=20, h=20, tool="blackout")]
    )
    img = _decode(out.bytes_)
    # Centre of rect must be pure black.
    assert img.getpixel((20, 20)) == (0, 0, 0)
    # Corner pixel of the source rect must be pure black too.
    assert img.getpixel((10, 10)) == (0, 0, 0)
    # Just outside the rect must be unchanged red.
    assert img.getpixel((5, 5)) == (255, 0, 0)
    assert img.getpixel((35, 35)) == (255, 0, 0)


def test_blur_reduces_local_variance_but_keeps_outside_intact() -> None:
    # Source has a sharp 8x8 white square inside a black field; the
    # blur rect covers it. After blur, the centre is no longer pure
    # white (variance with the surrounding black drops).
    img = Image.new("RGB", (40, 40), (0, 0, 0))
    for y in range(16, 24):
        for x in range(16, 24):
            img.putpixel((x, y), (255, 255, 255))
    src = _png_bytes(img)
    out = ImageRedactor.redact_bytes(
        src, "png",
        [ImageRedaction(x=10, y=10, w=20, h=20, tool="blur", intensity=4)],
    )
    blurred = _decode(out.bytes_)
    # Inside the rect: centre pixel was pure white, after blur it
    # should be lighter than the outer black but no longer 255.
    inside = blurred.getpixel((20, 20))
    assert inside[0] > 100, f"blur did not affect centre: {inside}"
    assert inside[0] < 255, f"blur did not soften centre: {inside}"
    # Outside the rect must stay pure black.
    assert blurred.getpixel((1, 1)) == (0, 0, 0)


def test_pixelate_collapses_detail_inside_rect() -> None:
    # Source is a noisy gradient inside the rect. After pixelation
    # with a 4-pixel block, all pixels inside one block must be
    # equal (block uniform).
    img = Image.new("RGB", (40, 40), (0, 0, 0))
    for y in range(40):
        for x in range(40):
            img.putpixel((x, y), (x * 6 % 256, y * 6 % 256, 128))
    src = _png_bytes(img)
    out = ImageRedactor.redact_bytes(
        src, "png",
        [ImageRedaction(x=8, y=8, w=24, h=24, tool="pixelate", intensity=4)],
    )
    pix = _decode(out.bytes_)
    # Two adjacent pixels inside the same 4-pixel block must be equal.
    block_a = pix.getpixel((10, 10))
    block_b = pix.getpixel((11, 11))
    assert block_a == block_b, f"pixelate did not flatten block: {block_a} vs {block_b}"
    # Outside the rect: original gradient must still be there.
    assert pix.getpixel((1, 1)) == (6, 6, 128)


def test_text_overlay_writes_label_inside_rect() -> None:
    src = _solid((50, 50, 50), size=(120, 60))
    out = ImageRedactor.redact_bytes(
        src, "png",
        [
            ImageRedaction(
                x=10, y=10, w=100, h=40,
                tool="text_overlay",
                text="REDACTED",
                font_size=18,
                fg="#FFFFFF",
                bg="#000000",
            )
        ],
    )
    img = _decode(out.bytes_)
    # The corners of the rect must be the bg colour (black).
    assert img.getpixel((10, 10)) == (0, 0, 0)
    # Outside the rect must stay the original 50/50/50 grey.
    assert img.getpixel((1, 1)) == (50, 50, 50)
    # At least one pixel in the rect's interior must be near-white,
    # i.e. part of a glyph stroke. We probe the row of the glyph
    # baseline; with REDACTED at fontsize 18 inside a 100x40 rect,
    # at least some row will contain a white pixel.
    found_white = False
    for y in range(15, 45):
        for x in range(15, 105):
            r, g, b = img.getpixel((x, y))
            if r > 200 and g > 200 and b > 200:
                found_white = True
                break
        if found_white:
            break
    assert found_white, "text overlay did not draw any visible glyph"


def test_multiple_rects_apply_in_order_last_overpaints_overlap() -> None:
    src = _solid((128, 128, 128), size=(60, 60))
    out = ImageRedactor.redact_bytes(
        src, "png",
        [
            # First rect blackouts the whole 30x30 square (top-left).
            ImageRedaction(x=0, y=0, w=30, h=30, tool="blackout"),
            # Second rect overlaps the first one with a text_overlay
            # whose bg is pure red. Last-wins rule means the overlap
            # area must show red, not black.
            ImageRedaction(
                x=15, y=15, w=20, h=20,
                tool="text_overlay",
                text="X",
                font_size=10,
                fg="#FFFFFF",
                bg="#FF0000",
            ),
        ],
    )
    img = _decode(out.bytes_)
    # Outside both rects: original grey unchanged.
    assert img.getpixel((50, 50)) == (128, 128, 128)
    # Inside only the first rect (top-left), the blackout dominates.
    assert img.getpixel((5, 5)) == (0, 0, 0)
    # Inside the overlap (15..30, 15..30): second rect wins, must be
    # red bg (NOT the blackout black). Sample a corner of the
    # overlap so we definitely miss the centred glyph stroke.
    overlap = img.getpixel((16, 16))
    assert overlap == (255, 0, 0), (
        f"second rect did not overpaint the blackout in overlap: {overlap}"
    )


def test_oversize_rect_clamps_to_image_bounds() -> None:
    src = _solid((100, 100, 100), size=(20, 20))
    out = ImageRedactor.redact_bytes(
        src, "png",
        [ImageRedaction(x=-10, y=-10, w=100, h=100, tool="blackout")],
    )
    img = _decode(out.bytes_)
    # Whole image painted black.
    for x in (0, 10, 19):
        for y in (0, 10, 19):
            assert img.getpixel((x, y)) == (0, 0, 0)


def test_rect_fully_outside_image_is_dropped_silently() -> None:
    src = _solid((100, 100, 100), size=(20, 20))
    out = ImageRedactor.redact_bytes(
        src, "png",
        [ImageRedaction(x=100, y=100, w=10, h=10, tool="blackout")],
    )
    img = _decode(out.bytes_)
    # Image untouched.
    assert img.getpixel((10, 10)) == (100, 100, 100)


def test_jpeg_input_roundtrips_to_jpeg_output() -> None:
    img = Image.new("RGB", (40, 40), (220, 30, 30))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    out = ImageRedactor.redact_bytes(
        buf.getvalue(), "jpeg",
        [ImageRedaction(x=5, y=5, w=10, h=10, tool="blackout")],
    )
    assert out.fmt_out == "jpeg"
    decoded = _decode(out.bytes_)
    # JPEG quantisation makes equality fuzzy, but the rect MUST be
    # close to black.
    cx = decoded.getpixel((10, 10))
    assert cx[0] < 30 and cx[1] < 30 and cx[2] < 30


def test_cmyk_input_emits_rgb_jpeg_with_warning() -> None:
    cmyk = Image.new("CMYK", (40, 40), (0, 100, 100, 0))
    buf = BytesIO()
    cmyk.save(buf, format="JPEG")
    out = ImageRedactor.redact_bytes(
        buf.getvalue(), "jpeg",
        [ImageRedaction(x=5, y=5, w=10, h=10, tool="blackout")],
    )
    assert out.fmt_out == "jpeg"
    assert any("CMYK->RGB" in w for w in out.warnings), out.warnings
    decoded = _decode(out.bytes_)
    assert decoded.mode == "RGB"


def test_invalid_tool_rejected_at_dataclass_level() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        ImageRedaction(x=0, y=0, w=10, h=10, tool="acid_burn")  # type: ignore[arg-type]


def test_text_overlay_requires_text() -> None:
    with pytest.raises(ValueError, match="text_overlay requires"):
        ImageRedaction(x=0, y=0, w=10, h=10, tool="text_overlay", text="")


def test_zero_size_rect_rejected() -> None:
    with pytest.raises(ValueError, match="non-positive size"):
        ImageRedaction(x=0, y=0, w=0, h=10, tool="blackout")
