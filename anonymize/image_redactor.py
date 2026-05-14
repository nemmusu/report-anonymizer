"""Pure-pixel image redaction primitives.

The four tools we ship in MVP, mirroring the Flameshot / Greenshot
visual language pentest operators are already used to:

* ``blackout``     solid black rectangle, the standard "this was here
                   but you cannot see it" mark.
* ``blur``         gaussian blur of the rect; readable as text-once-
                   was, no actual content visible. Useful on faces or
                   on UI chrome that should stay recognisable as a
                   region but not legible.
* ``pixelate``     downscale + nearest-neighbour upscale. Same
                   semantic as blur, different aesthetic; some teams
                   prefer this for screenshots because the pixel grid
                   reads as "synthetic" at a glance.
* ``text_overlay`` filled background rect with a centred text label.
                   Used when the operator wants to keep the screenshot
                   readable as "there was a hostname here" by
                   stamping ``REDACTED`` (or any label) over the
                   region.

Implementation notes:

* All operations are pure functions on bytes -> bytes; no I/O. The
  per-format adapters call this module to get the redacted image
  bytes, then push them back into the container (PDF xref, DOCX
  inline shape, PPTX picture).
* The output preserves the input format (PNG -> PNG, JPEG -> JPEG)
  so the format adapter can re-insert the bytes without changing
  the image dictionary's filter / colorspace. CMYK is the lone
  exception: PIL cannot save a modified CMYK as PNG, so we convert
  to RGB and emit JPEG with a documented warning the caller can
  surface.
* The order of rects in the input list is the order of application:
  later rects overpaint earlier ones on overlap, deterministically.
* Coordinates are in original image pixel space, not in PDF / page
  points. The format adapter is responsible for resolving the
  ``image_id`` to the correct embedded bytes; pixel-space rects are
  what makes the same redaction decision portable across re-scans.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# Type aliases kept narrow on purpose: anything outside this set is
# rejected at the dataclass boundary before reaching the dispatcher.
Tool = Literal["blackout", "blur", "pixelate", "text_overlay"]
_TOOLS: frozenset[str] = frozenset(("blackout", "blur", "pixelate", "text_overlay"))


@dataclass(frozen=True)
class ImageRedaction:
    """One redaction action on one image, in original pixel space."""
    x: int
    y: int
    w: int
    h: int
    tool: Tool
    intensity: Optional[int] = None       # blur radius / pixelate block size
    text: Optional[str] = None            # text_overlay label
    font_size: Optional[int] = None       # text_overlay font size (px)
    fg: Optional[str] = None              # text_overlay text colour, hex (#RRGGBB)
    bg: Optional[str] = None              # text_overlay background colour, hex

    def __post_init__(self) -> None:
        if self.tool not in _TOOLS:
            raise ValueError(f"unknown tool: {self.tool!r}")
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"non-positive size: w={self.w} h={self.h}")
        if self.tool == "text_overlay" and not self.text:
            raise ValueError("text_overlay requires a non-empty 'text'")


@dataclass
class RedactResult:
    """Output of ``ImageRedactor.redact_bytes``."""
    bytes_: bytes
    fmt_out: str                           # "png" / "jpeg" / ...
    warnings: list[str]                    # e.g. "colorspace_changed:CMYK->RGB"


# Default parameters per tool, applied when the caller does not
# specify. Picked empirically: 8 px gaussian and 12 px pixel block
# both produce a "clearly redacted" result without making the rect
# look like a rendering bug at typical screenshot resolutions.
_DEFAULT_BLUR_RADIUS = 8
_DEFAULT_PIXELATE_BLOCK = 12
_DEFAULT_FONT_SIZE = 18
_DEFAULT_TEXT_FG = "#FFFFFF"
_DEFAULT_TEXT_BG = "#000000"

# Re-encode quality for any rect that triggered a JPEG re-save.
# Picked high enough that the unaffected regions are visually
# indistinguishable from the original; lower would be smaller files
# but visible JPEG ringing creeps in around 80.
_JPEG_QUALITY = 92

# Cap to keep absurd intensity values from blowing up memory on
# pathological input ("intensity = 99999" on a 4K image).
_MAX_BLUR_RADIUS = 64
_MAX_PIXELATE_BLOCK = 128


class ImageRedactor:
    """Stateless dispatcher: bytes + rects -> redacted bytes."""

    @staticmethod
    def redact_bytes(
        src_bytes: bytes,
        fmt_hint: str,
        rects: list[ImageRedaction],
    ) -> RedactResult:
        """Apply ``rects`` to ``src_bytes`` and return the new bytes.

        ``fmt_hint`` is the original image format as reported by the
        per-format adapter (``"png"`` / ``"jpeg"`` / ``"jp2"`` /
        ``"tiff"`` / etc). We try to honour it on output so the
        container's image dictionary can stay unchanged. CMYK is the
        documented exception (RGB JPEG, with a warning).

        An empty ``rects`` list is a no-op: returns the original
        bytes byte-for-byte. This matters for the apply-pass loop:
        the operator may have a "skip" decision, in which case no
        redaction is requested and the image must round-trip
        identically.
        """
        warnings: list[str] = []
        if not rects:
            return RedactResult(bytes_=src_bytes, fmt_out=(fmt_hint or "png").lower(), warnings=warnings)

        try:
            img = Image.open(BytesIO(src_bytes))
            img.load()
        except Exception as e:
            raise ValueError(f"cannot decode image bytes: {e}") from e

        original_mode = img.mode
        original_format = (img.format or fmt_hint or "PNG").upper()

        # CMYK -> RGB so that draw / blur / pixelate / save-as-PNG
        # all behave. PIL cannot encode modified CMYK to PNG and
        # reliable CMYK JPEG re-encoding requires an ICC profile we
        # don't carry, so we accept a colorspace conversion.
        if original_mode == "CMYK":
            img = img.convert("RGB")
            warnings.append("colorspace_changed:CMYK->RGB")
        elif original_mode in ("LA", "PA"):
            # Less common modes; promote to RGBA so paste back works.
            img = img.convert("RGBA")
        elif original_mode == "P":
            # Palette images: promoting preserves transparency for
            # the unaffected regions. The output container may need
            # to switch from indexed to direct colour, the
            # per-format adapter handles that.
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")

        # Apply each rect in order; later rects overpaint earlier
        # ones on overlap.
        for r in rects:
            ImageRedactor._apply_one(img, r)

        # Pick the output format. Stay on the input format when we
        # can; downgrade CMYK to JPEG since the conversion above
        # makes that the only sensible choice.
        out_fmt = original_format if original_mode != "CMYK" else "JPEG"
        # Normalise PIL-style format strings for the caller.
        out_fmt_lower = out_fmt.lower()
        if out_fmt_lower in ("jpg",):
            out_fmt_lower = "jpeg"

        buf = BytesIO()
        save_kwargs: dict = {}
        if out_fmt_lower == "jpeg":
            # JPEG cannot encode an alpha channel; flatten RGBA to
            # RGB on a white background only when the original was
            # already JPEG. For PNG-source-but-CMYK-converted we
            # already promoted to RGB.
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            save_kwargs["quality"] = _JPEG_QUALITY
            save_kwargs["optimize"] = True
        elif out_fmt_lower == "png":
            save_kwargs["optimize"] = True

        try:
            img.save(buf, format=out_fmt_lower.upper(), **save_kwargs)
        except (OSError, ValueError) as e:
            # Fall back to PNG if the original format is unknown to
            # PIL or refuses our mode. PNG is universally accepted by
            # PyMuPDF's replace_image.
            buf = BytesIO()
            if img.mode == "CMYK":
                img = img.convert("RGB")
            img.save(buf, format="PNG", optimize=True)
            out_fmt_lower = "png"
            warnings.append(f"format_fallback:{out_fmt}->PNG ({e.__class__.__name__})")

        return RedactResult(
            bytes_=buf.getvalue(),
            fmt_out=out_fmt_lower,
            warnings=warnings,
        )

    # ---- per-tool implementations ----------------------------------

    @staticmethod
    def _apply_one(img: Image.Image, r: ImageRedaction) -> None:
        """Mutate ``img`` in place with one redaction."""
        # Clamp the rect to the image bounds so an oversized rect
        # (operator dragged past the canvas edge) does not raise.
        x0 = max(0, min(img.width, r.x))
        y0 = max(0, min(img.height, r.y))
        x1 = max(0, min(img.width, r.x + r.w))
        y1 = max(0, min(img.height, r.y + r.h))
        if x1 <= x0 or y1 <= y0:
            return  # rect fully outside the image; silently drop

        if r.tool == "blackout":
            ImageRedactor._draw_blackout(img, x0, y0, x1, y1)
        elif r.tool == "blur":
            radius = _clamp(r.intensity or _DEFAULT_BLUR_RADIUS, 1, _MAX_BLUR_RADIUS)
            ImageRedactor._draw_blur(img, x0, y0, x1, y1, radius)
        elif r.tool == "pixelate":
            block = _clamp(r.intensity or _DEFAULT_PIXELATE_BLOCK, 2, _MAX_PIXELATE_BLOCK)
            ImageRedactor._draw_pixelate(img, x0, y0, x1, y1, block)
        elif r.tool == "text_overlay":
            ImageRedactor._draw_text_overlay(
                img, x0, y0, x1, y1,
                text=r.text or "",
                font_size=r.font_size or _DEFAULT_FONT_SIZE,
                fg=r.fg or _DEFAULT_TEXT_FG,
                bg=r.bg or _DEFAULT_TEXT_BG,
            )

    @staticmethod
    def _draw_blackout(img: Image.Image, x0: int, y0: int, x1: int, y1: int) -> None:
        # Solid black rectangle. Use ImageDraw because it composites
        # cleanly even on RGBA / palette modes.
        draw = ImageDraw.Draw(img)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(0, 0, 0))

    @staticmethod
    def _draw_blur(img: Image.Image, x0: int, y0: int, x1: int, y1: int, radius: int) -> None:
        # Crop, blur, paste back. This keeps the blur strictly inside
        # the rect (a whole-image blur with a mask would smear blurred
        # pixels OUT of the rect, which is the wrong semantic for a
        # redaction).
        crop = img.crop((x0, y0, x1, y1))
        blurred = crop.filter(ImageFilter.GaussianBlur(radius=radius))
        img.paste(blurred, (x0, y0))

    @staticmethod
    def _draw_pixelate(img: Image.Image, x0: int, y0: int, x1: int, y1: int, block: int) -> None:
        # Downscale to roughly 1 pixel per ``block``, then upscale
        # nearest-neighbour. The crop/paste-back keeps the rest of
        # the image untouched.
        crop = img.crop((x0, y0, x1, y1))
        w = max(1, crop.width // block)
        h = max(1, crop.height // block)
        small = crop.resize((w, h), Image.Resampling.NEAREST)
        big = small.resize(crop.size, Image.Resampling.NEAREST)
        img.paste(big, (x0, y0))

    @staticmethod
    def _draw_text_overlay(
        img: Image.Image,
        x0: int, y0: int, x1: int, y1: int,
        *,
        text: str,
        font_size: int,
        fg: str,
        bg: str,
    ) -> None:
        # Step 1: fill the rect with the background colour.
        draw = ImageDraw.Draw(img)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=_hex_to_rgb(bg))

        # Step 2: try to load a real font (better metrics + kerning).
        # Fall back to PIL's built-in bitmap font when no truetype is
        # available (PIL still ships a fixed 8 px font at the worst).
        font = _load_font(font_size)

        # Step 3: centre the text inside the rect using the actual
        # text bbox. PIL >= 8 has draw.textbbox; older releases need
        # textsize. We use textbbox and accept PIL >= 8 as a hard
        # requirement (already true via the project's wheel).
        try:
            tb = draw.textbbox((0, 0), text, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            tx_off, ty_off = tb[0], tb[1]
        except Exception:
            tw, th = font_size * len(text) // 2, font_size
            tx_off = ty_off = 0

        cx = x0 + ((x1 - x0) - tw) // 2 - tx_off
        cy = y0 + ((y1 - y0) - th) // 2 - ty_off
        draw.text((cx, cy), text, font=font, fill=_hex_to_rgb(fg))


# ---- helpers --------------------------------------------------------

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse ``#RRGGBB`` or ``RRGGBB`` to a 3-tuple. Defaults to black on bad input."""
    s = (hex_str or "").lstrip("#").strip()
    if len(s) != 6:
        return (0, 0, 0)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (0, 0, 0)


# Cache the font lookup: opening a TrueType file is slow and we may
# render hundreds of overlays in a single apply pass.
_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}

# Common font paths we probe in order. These are the fonts that
# Liberation / DejaVu ship on most Linux desktops, plus the macOS
# default and a Windows fallback. PIL handles missing files
# silently, we just probe until one opens.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size=size)
        except (OSError, IOError):
            continue
        _FONT_CACHE[size] = font
        return font
    # Last-resort fallback: PIL's built-in bitmap font (always present
    # but tiny: ~10 px tall regardless of requested size). Users on
    # an unusual environment will see the label, just smaller.
    fallback = ImageFont.load_default()
    _FONT_CACHE[size] = fallback
    return fallback


__all__ = [
    "ImageRedaction",
    "ImageRedactor",
    "RedactResult",
    "Tool",
]
