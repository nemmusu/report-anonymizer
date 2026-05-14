"""PDF in-place redaction adapter built on PyMuPDF (``fitz``).

Strategy (production-grade, overlap-safe and style-preserving):

1. Open the PDF. For every page, list every text span with bbox + font +
   size + color + ``origin`` baseline + ``flags`` (``page.get_text("dict")``).
2. Pre-extract every embedded font referenced by the page's spans into a
   ``fitz.Font(buffer=...)`` cache. We then register those fonts on each
   page via ``page.insert_font(fontbuffer=...)`` and reuse them for the
   substitutions, so the inserted text matches the original glyph design
   (e.g. Open Sans stays Open Sans, not Helvetica). Fonts that cannot be
   extracted (CID type0 / type3) fall back to a base-14 chosen from the
   span ``flags`` (bold / italic / mono / serif).
3. For each substitution rule, locate matches via ``page.search_for(from_)``
   (fitz handles word-wrap/dehyphenation across spans; flag
   ``TEXT_DEHYPHENATE | TEXT_PRESERVE_LIGATURES``).
4. Deduplicate overlapping matches (longest-first invariant) so one rule
   never clobbers another's rectangle.
5. PASS 1: ``add_redact_annot(rect, fill=False)`` on every claimed
   rect, then ``apply_redactions()``. We pass ``fill=False`` so
   PyMuPDF removes the glyphs from the content stream WITHOUT
   painting a fill rectangle on top: a flat fill (even one colour-
   matched to the page) would leave a visible boundary against any
   antialiased cell border, gradient, table stripe or row shading,
   while skipping it lets the original background show through
   unchanged. Security (no residual text) is preserved by the post-
   redaction ``page.search_for(orig)`` check, which warns if a glyph
   survived. We also deliberately do NOT use the optional ``text=``
   argument because PyMuPDF can fail to render the appearance text
   at the right baseline.
6. PASS 2: ``page.insert_text((origin_x, origin_y), repl, fontname=...,
   fontsize=..., color=...)`` using the ORIGINAL span baseline. This is
   what fixes the visible "wrong vertical alignment" / "different font"
   regressions the user reported.

This preserves layout/images/font/colour for typical text PDFs. Image
based (scanned) PDFs cannot be processed in-place; the adapter raises a
clear RuntimeError that the GUI surfaces with an "Use re-derive" hint.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .base import (
    FormatAdapter,
    ImageReport,
    InventoryImageRaw,
    Segment,
    SubstitutionRule,
    WriteEvent,
    WriteReport,
    apply_to_text,
)


# PyMuPDF span ``flags`` bit semantics (cf. fitz docs):
#   bit 0 (0x01): superscripted
#   bit 1 (0x02): italic
#   bit 2 (0x04): serifed
#   bit 3 (0x08): monospaced
#   bit 4 (0x10): bold
_FLAG_ITALIC = 0x02
_FLAG_SERIF = 0x04
_FLAG_MONO = 0x08
_FLAG_BOLD = 0x10


class PdfInplaceAdapter(FormatAdapter):
    name = "pdf_inplace"
    extensions = {".pdf"}
    mimes = {"application/pdf"}

    def __init__(self) -> None:
        try:
            import fitz  # type: ignore  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                f"PyMuPDF (fitz) is required for the pdf_inplace adapter: {e}"
            )

    def extract(self, path: Path) -> list[Segment]:
        import fitz  # type: ignore

        out: list[Segment] = []
        with fitz.open(str(path)) as doc:
            if doc.needs_pass:
                raise RuntimeError(
                    "PDF is encrypted; provide the password via --pdf-password or "
                    "decrypt it (qpdf --decrypt) before processing."
                )
            scanned_pages = 0
            for pi, page in enumerate(doc):
                text = page.get_text("text") or ""
                if not text.strip():
                    scanned_pages += 1
                    continue
                out.append(
                    Segment(
                        seg_id=f"page{pi}", text=text, meta={"page": pi + 1}
                    )
                )
            if not out and scanned_pages > 0:
                raise RuntimeError(
                    f"PDF appears to be scanned ({scanned_pages} image-only pages); "
                    "OCR support requires `tesseract` and `ocrmypdf` to be installed; "
                    "or use the rederive PDF strategy after manual OCR."
                )
        return out

    # ---- image inventory + apply ------------------------------------------

    def inventory_images(self, path: Path) -> list[InventoryImageRaw]:
        """Walk every page, enumerate raster images, return their raw
        bytes plus the (page_index, xref, bbox) location.

        Soft masks (alpha) are recorded as a warning so the apply
        pass can composite them before redacting; the apply pass
        ALSO replaces the smask region with opaque alpha for any
        rect the operator paints, so blackout actually shows up
        black (not a transparent hole).

        Vector graphics emitted via ``page.draw_path`` / drawings
        operators are NOT raster images, ``page.get_images()``
        ignores them. They are surfaced as a per-page warning so
        the GUI can offer a "manual draw_rect overlay" workflow in
        a future phase; the MVP just acknowledges they exist.
        """
        import fitz  # type: ignore

        out: list[InventoryImageRaw] = []
        with fitz.open(str(path)) as doc:
            for pi, page in enumerate(doc):
                # ``full=True`` includes the smask xref (column 1) and
                # bpc / colorspace metadata we surface as warnings.
                try:
                    entries = page.get_images(full=True) or []
                except Exception:
                    entries = []
                if not entries:
                    continue
                seen_xrefs: set[int] = set()
                for ent in entries:
                    try:
                        xref = int(ent[0])
                    except Exception:
                        continue
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    try:
                        info = doc.extract_image(xref)
                    except Exception:
                        continue
                    raw_bytes = info.get("image") if info else None
                    if not raw_bytes:
                        continue
                    fmt = (info.get("ext") or "png").lower()
                    width = int(info.get("width") or 0)
                    height = int(info.get("height") or 0)
                    warnings: list[str] = []
                    cs = info.get("colorspace")
                    if isinstance(cs, int) and cs == 4:
                        # Per PyMuPDF: colorspace == 4 means CMYK.
                        warnings.append("cmyk")
                    if info.get("smask"):
                        warnings.append("soft_mask_present")
                    bbox = self._first_image_bbox(page, xref)
                    out.append(
                        InventoryImageRaw(
                            raw_bytes=bytes(raw_bytes),
                            fmt=fmt,
                            width=width,
                            height=height,
                            location={
                                "kind": "pdf",
                                "page_index": pi,
                                "xref": xref,
                                "bbox": list(bbox) if bbox else None,
                            },
                            warnings=warnings,
                        )
                    )
        return out

    @staticmethod
    def _first_image_bbox(page, xref: int) -> Optional[tuple[float, float, float, float]]:
        """Return the rect at which an image is first rendered on a page.

        For overlay / preview purposes only. The apply pass uses xref
        for the in-place replacement, the bbox just helps the GUI
        show "this image lives here on page N".
        """
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        if not rects:
            return None
        try:
            r = rects[0]
            return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
        except Exception:
            return None

    def apply_image_redactions(
        self,
        dst_path: Path,
        decisions_for_file: dict,
    ) -> ImageReport:
        """In-place rewrite of every image whose ``image_id`` has a
        ``redact`` decision. Same xref, same on-page position, same
        dimensions: identical ordering and structure on output.
        """
        from ..image_inventory import compute_image_id, ImageDecision
        from ..image_redactor import ImageRedaction, ImageRedactor
        import fitz  # type: ignore

        report = ImageReport(file_rel=str(dst_path))
        if not decisions_for_file:
            return report
        # Pre-decode the operator decisions into the format the
        # ImageRedactor accepts. We do this once per file so the
        # per-page loop stays cheap.
        prepared: dict[str, list[ImageRedaction]] = {}
        skip_ids: set[str] = set()
        for image_id, decision in decisions_for_file.items():
            if not isinstance(decision, ImageDecision):
                # Tolerate dict-shaped input for callers that have
                # not yet boxed the decisions. Best-effort.
                from ..image_inventory import ImageDecision as _Dec  # local alias
                decision = _Dec.from_dict(image_id, decision or {})
            if decision.decision in ("skip", "defer"):
                skip_ids.add(image_id)
                continue
            if decision.decision != "redact" or not decision.rects:
                # Nothing actionable; treat as skip.
                skip_ids.add(image_id)
                continue
            prepared[image_id] = [
                ImageRedaction(
                    x=r.x, y=r.y, w=r.w, h=r.h,
                    tool=r.tool,
                    intensity=r.intensity,
                    text=r.text,
                    font_size=r.font_size,
                    fg=r.fg,
                    bg=r.bg,
                ) for r in decision.rects
            ]

        if not prepared and not skip_ids:
            return report

        try:
            doc = fitz.open(str(dst_path))
        except Exception as e:
            report.warnings.append(f"open_failed:{e}")
            return report
        try:
            seen_xrefs: set[int] = set()
            for page in doc:
                try:
                    entries = page.get_images(full=True) or []
                except Exception:
                    entries = []
                for ent in entries:
                    try:
                        xref = int(ent[0])
                    except Exception:
                        continue
                    if xref in seen_xrefs:
                        # An image referenced by multiple pages still
                        # has one xref, replacing it once redacts every
                        # occurrence, the desired semantics for
                        # "logo on every page".
                        continue
                    try:
                        info = doc.extract_image(xref)
                    except Exception:
                        continue
                    raw = info.get("image") if info else None
                    if not raw:
                        continue
                    image_id = compute_image_id(bytes(raw))
                    if image_id in skip_ids:
                        seen_xrefs.add(xref)
                        report.skipped += 1
                        continue
                    rects = prepared.get(image_id)
                    if not rects:
                        # No decision for this image_id: untouched.
                        report.untouched += 1
                        continue
                    seen_xrefs.add(xref)
                    fmt_hint = (info.get("ext") or "png").lower()
                    try:
                        result = ImageRedactor.redact_bytes(
                            bytes(raw), fmt_hint, rects
                        )
                    except Exception as e:
                        report.warnings.append(
                            f"redact_failed:{image_id}:{e}"
                        )
                        continue
                    try:
                        page.replace_image(xref, stream=result.bytes_)
                    except Exception as e:
                        report.warnings.append(
                            f"replace_failed:{image_id}:{e}"
                        )
                        continue
                    if result.warnings:
                        report.warnings.extend(
                            f"{image_id}:{w}" for w in result.warnings
                        )
                    report.applied += 1
            # ``garbage=4`` runs the most aggressive object GC PyMuPDF
            # offers: it drops unreferenced objects and rebuilds the
            # xref table. This matters for redaction because
            # ``replace_image`` may leave the original image's xref
            # orphaned in the file (still extractable with a hex
            # editor) unless we explicitly purge unreferenced objects.
            # ``garbage=4`` physically removes the ORIGINAL
            # un-redacted bytes from the output PDF, the security
            # guarantee operators expect from a redaction pass.
            #
            # ``incremental=False`` requires writing to a path
            # distinct from the source. ``dst_path`` IS the freshly-
            # written textual-anonymisation output (distinct from the
            # input PDF), but PyMuPDF still flags writing back to the
            # same opened path as incremental, so we route through a
            # tempfile and ``os.replace`` for the atomic swap.
            tmp_save = dst_path.with_suffix(dst_path.suffix + ".imgtmp")
            try:
                doc.save(
                    str(tmp_save),
                    incremental=False,
                    deflate=True,
                    garbage=4,
                    clean=True,
                )
            except Exception as e:
                report.warnings.append(f"save_failed:{e}")
                tmp_save = None
        finally:
            doc.close()
        if tmp_save is not None and tmp_save.exists():
            import os as _os
            _os.replace(str(tmp_save), str(dst_path))
        return report

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _strip_subset_prefix(name: str) -> str:
        """``AAAAAA+LiberationSans`` -> ``LiberationSans`` (PDF subset tag)."""
        if not name:
            return ""
        if len(name) > 7 and name[6] == "+" and name[:6].isupper() and name[:6].isalpha():
            return name[7:]
        return name

    @staticmethod
    def _build_page_font_xref_map(page) -> dict[str, int]:
        """Return ``{stripped_basefont: xref}`` for every font on ``page``.

        Used in PASS 2 to look up the original embedded font from the
        ``span["font"]`` value (which is the un-prefixed basefont), so we
        can extract its binary stream and reuse it for ``insert_text``.
        """
        try:
            entries = page.get_fonts(full=True) or []
        except Exception:
            return {}
        out: dict[str, int] = {}
        for ent in entries:
            try:
                xref = int(ent[0])
                basefont = str(ent[3] or "")
            except Exception:
                continue
            if not basefont:
                continue
            stripped = PdfInplaceAdapter._strip_subset_prefix(basefont)
            # First occurrence wins; multiple xrefs for the same logical
            # font are rare on a single page.
            out.setdefault(stripped, xref)
        return out

    @staticmethod
    def _find_span_props(
        page,
        rect,
        *,
        default_font: str,
        default_size: float,
        default_color: int,
    ):
        """Return (fontname, fontsize, color_int, origin, flags).

        The match is the smallest-area span whose bbox vertically contains
        the search rect midline AND horizontally overlaps it by >50%.
        That avoids picking the wrong span when adjacent lines have very
        tight bbox overlap.
        """
        try:
            d = page.get_text("dict")
        except Exception:
            return (
                default_font,
                default_size,
                default_color,
                None,
                0,
            )
        rx0, ry0, rx1, ry1 = rect[0], rect[1], rect[2], rect[3]
        ry_mid = (ry0 + ry1) / 2.0
        best = None
        best_area = float("inf")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sb = span.get("bbox")
                    if not sb:
                        continue
                    sx0, sy0, sx1, sy1 = sb[0], sb[1], sb[2], sb[3]
                    if not (sy0 - 1.0 <= ry_mid <= sy1 + 1.0):
                        continue
                    overlap = max(0.0, min(sx1, rx1) - max(sx0, rx0))
                    if overlap < (rx1 - rx0) * 0.5:
                        continue
                    area = max(0.0, sx1 - sx0) * max(0.0, sy1 - sy0)
                    if area < best_area:
                        best = span
                        best_area = area
        if best is None:
            return (
                default_font,
                default_size,
                default_color,
                None,
                0,
            )
        # ``span.get("color", default)`` is the right form: ``or`` would
        # collapse a legitimate 0 (= black) to the default.
        return (
            best.get("font") or default_font,
            float(best.get("size") or default_size),
            int(best.get("color", default_color)),
            best.get("origin"),
            int(best.get("flags") or 0),
        )

    @staticmethod
    def _rects_overlap(a, b, *, tol: float = 0.5) -> bool:
        """Two PyMuPDF rects (x0, y0, x1, y1) overlap if their interiors do."""
        return not (
            a[2] <= b[0] + tol
            or b[2] <= a[0] + tol
            or a[3] <= b[1] + tol
            or b[3] <= a[1] + tol
        )

    @staticmethod
    def _safe_fontname(name: str, *, flags: int = 0) -> str:
        """Pick a base-14 fitz alias from the source font name + span flags.

        The flags-based path is more reliable than name parsing because
        many fonts use opaque PostScript names ("AAAAAB+TexGyreHeros-Bold")
        that don't match common substrings.
        """
        is_bold = bool(flags & _FLAG_BOLD)
        is_italic = bool(flags & _FLAG_ITALIC)
        is_mono = bool(flags & _FLAG_MONO)
        is_serif = bool(flags & _FLAG_SERIF)
        n = (name or "").lower()
        if not is_bold and ("bold" in n or "black" in n or "heavy" in n):
            is_bold = True
        if not is_italic and ("italic" in n or "oblique" in n):
            is_italic = True
        if not is_mono and ("mono" in n or "courier" in n or "consolas" in n):
            is_mono = True
        if not is_serif and (
            "times" in n or "serif" in n or "garamond" in n or "georgia" in n
        ):
            is_serif = True
        if is_mono:
            if is_bold and is_italic:
                return "cobi"
            if is_bold:
                return "cobo"
            if is_italic:
                return "coit"
            return "cour"
        if is_serif:
            if is_bold and is_italic:
                return "tibi"
            if is_bold:
                return "tibo"
            if is_italic:
                return "tiit"
            return "tiro"
        if is_bold and is_italic:
            return "hebi"
        if is_bold:
            return "hebo"
        if is_italic:
            return "heit"
        return "helv"

    @staticmethod
    def _color_from_int(color_int: int) -> tuple[float, float, float]:
        return (
            ((color_int >> 16) & 0xFF) / 255.0,
            ((color_int >> 8) & 0xFF) / 255.0,
            (color_int & 0xFF) / 255.0,
        )

    @staticmethod
    def _sample_bg_color(page, rect) -> tuple[float, float, float]:
        # Return the page background colour under ``rect``. Hardcoding white
        # broke redaction over coloured backgrounds (dark code blocks, hero
        # banners, table cells with a fill colour) by leaving a visible
        # white rectangle under the placeholder text. The naive single-offset
        # version of this helper then mis-fired when the rect sat next to
        # an inline highlight (a coloured ``<mark>`` style span, a hyperlink
        # underlay), because the sampler picked up the highlight colour
        # instead of the surrounding block background.
        #
        # Strategy: shoot 8 probes around the rect at two different offsets
        # (close-in and farther out, so a narrow inline highlight cannot win
        # at every distance), quantise each probe to 8-bit-per-channel
        # buckets so anti-aliasing variants collapse, and pick the *mode*
        # (most-voted bucket). Tie-break by the darker bucket, since the
        # outer block background is usually a single solid colour while
        # inline highlights vary. Falls back to white on any error so
        # white-page documents render identically.
        try:
            import fitz  # type: ignore
        except Exception:
            return (1.0, 1.0, 1.0)
        try:
            x0 = float(rect[0])
            y0 = float(rect[1])
            x1 = float(rect[2])
            y1 = float(rect[3])
        except Exception:
            return (1.0, 1.0, 1.0)
        page_rect = getattr(page, "rect", None)
        if page_rect is not None:
            min_x = float(page_rect.x0)
            min_y = float(page_rect.y0)
            max_x = float(page_rect.x1)
            max_y = float(page_rect.y1)
        else:
            min_x, min_y = 0.0, 0.0
            max_x, max_y = x1 + 32.0, y1 + 32.0
        mid_x = (x0 + x1) / 2.0
        mid_y = (y0 + y1) / 2.0
        # Two distances so a thin inline highlight cannot dominate every
        # probe: the close ring catches the immediate background, the far
        # ring confirms it against the rest of the block.
        candidates: list[tuple[float, float]] = []
        for off in (4.0, 14.0):
            # Left and right margins, mid-height.
            candidates.append((max(min_x, x0 - off), mid_y))
            candidates.append((min(max_x - 0.5, x1 + off), mid_y))
            # Above and below, at the rect's horizontal centre.
            candidates.append((mid_x, max(min_y, y0 - off)))
            candidates.append((mid_x, min(max_y - 0.5, y1 + off)))
        # bucket key -> [count, sum_r, sum_g, sum_b]
        buckets: dict[tuple[int, int, int], list[int]] = {}
        for sx, sy in candidates:
            try:
                clip = fitz.Rect(sx - 0.5, sy - 0.5, sx + 0.5, sy + 0.5)
                pm = page.get_pixmap(clip=clip, alpha=False)
            except Exception:
                continue
            try:
                w = int(getattr(pm, "width", 0) or 0)
                h = int(getattr(pm, "height", 0) or 0)
                if w <= 0 or h <= 0:
                    continue
                px = pm.pixel(w // 2, h // 2)
            except Exception:
                continue
            if not px or len(px) < 3:
                continue
            try:
                r, g, b = int(px[0]), int(px[1]), int(px[2])
            except Exception:
                continue
            key = (r >> 3, g >> 3, b >> 3)  # 32-bucket per channel
            slot = buckets.setdefault(key, [0, 0, 0, 0])
            slot[0] += 1
            slot[1] += r
            slot[2] += g
            slot[3] += b
        if not buckets:
            return (1.0, 1.0, 1.0)
        # Mode wins; on ties prefer the darker bucket (the page background
        # is more likely to be the consistent "outer" colour, while
        # inline highlights tend to be lighter accents).
        best = max(
            buckets.values(),
            key=lambda v: (v[0], -(v[1] + v[2] + v[3])),
        )
        n = best[0]
        return (
            best[1] / n / 255.0,
            best[2] / n / 255.0,
            best[3] / n / 255.0,
        )

    @staticmethod
    def _sample_text_color(
        page,
        rect,
        bg_rgb: tuple[float, float, float],
    ) -> Optional[tuple[float, float, float]]:
        # Return the colour of the actual rendered glyphs inside ``rect``,
        # or None when sampling is impossible (rect too small, fitz
        # missing, no clearly-non-background pixels). PyMuPDF's
        # ``page.get_text("dict")`` reports a per-span ``color`` field,
        # but it can be stale: the PDF content stream often issues a
        # ``setrgbcolor`` operator that overrides the span's nominal
        # colour, and a search rect that crosses two adjacent spans
        # of different colours always picks the first span's metadata.
        # The pixels you see on screen are the ground truth, so we
        # rasterise the rect, threshold against the surrounding
        # background, and pick the mode of the foreground pixels.
        try:
            import fitz  # type: ignore
        except Exception:
            return None
        try:
            x0 = float(rect[0])
            y0 = float(rect[1])
            x1 = float(rect[2])
            y1 = float(rect[3])
        except Exception:
            return None
        if x1 - x0 < 2.0 or y1 - y0 < 2.0:
            return None
        try:
            # Render the rect at 2x so antialiased glyph centres land
            # solidly on real pixels. clip is in PDF user-space.
            pm = page.get_pixmap(
                clip=fitz.Rect(x0, y0, x1, y1),
                alpha=False,
                matrix=fitz.Matrix(2.0, 2.0),
            )
        except Exception:
            return None
        try:
            w = int(getattr(pm, "width", 0) or 0)
            h = int(getattr(pm, "height", 0) or 0)
            if w <= 0 or h <= 0:
                return None
            samples_raw = pm.samples
        except Exception:
            return None
        bg_r = int(round(bg_rgb[0] * 255.0))
        bg_g = int(round(bg_rgb[1] * 255.0))
        bg_b = int(round(bg_rgb[2] * 255.0))
        # Two-pass scan against an ADAPTIVE distance-from-background
        # threshold. The naive single-threshold version (any pixel
        # further than ~32 from bg counts as foreground, mode wins)
        # was biased toward antialiased glyph edges: at small font
        # sizes the AA halo of every stroke contains 2x more pixels
        # than the solid stroke centre, and on low-contrast cells
        # (dark text on a light-grey table cell) those halo pixels
        # cluster on a single mid-grey bucket that out-votes the
        # actual text-colour bucket. Result: the placeholder ended
        # up rendered in `#CBD5E1` instead of `#1A1F2B`.
        #
        # Strategy: compute every pixel's distance from bg, keep the
        # maximum, then bucket only pixels whose distance is at least
        # 60 % of that maximum. Those are the pixels firmly on the
        # text side of the AA gradient (the solid stroke interior).
        # Mode wins among those with darker tie-break.
        try:
            buf = samples_raw
            stride = 3
            n_px = w * h
            # Pass 1: find max distance.
            max_dist_sq = 0
            min_threshold_sq = 32 * 32 * 3  # baseline: cull near-bg noise
            for i in range(n_px):
                base = i * stride
                dr = buf[base] - bg_r
                dg = buf[base + 1] - bg_g
                db = buf[base + 2] - bg_b
                d = dr * dr + dg * dg + db * db
                if d > max_dist_sq:
                    max_dist_sq = d
            if max_dist_sq < min_threshold_sq:
                # No clearly-foreground pixels in the rect.
                return None
            # The square of (0.6 * sqrt(max_dist_sq)) equals 0.36 * max_dist_sq.
            adaptive_thr = max(min_threshold_sq, int(0.36 * max_dist_sq))
            # Pass 2: bucket only pixels above the adaptive threshold.
            buckets: dict[tuple[int, int, int], list[int]] = {}
            for i in range(n_px):
                base = i * stride
                r = buf[base]
                g = buf[base + 1]
                b = buf[base + 2]
                dr = r - bg_r
                dg = g - bg_g
                db = b - bg_b
                if dr * dr + dg * dg + db * db < adaptive_thr:
                    continue
                key = (r >> 3, g >> 3, b >> 3)
                slot = buckets.setdefault(key, [0, 0, 0, 0])
                slot[0] += 1
                slot[1] += r
                slot[2] += g
                slot[3] += b
        except Exception:
            return None
        if not buckets:
            return None
        # Need a meaningful number of solid-stroke-interior pixels.
        # A thin decorative border or a single stray glyph edge
        # might still produce a handful of "above-adaptive-threshold"
        # pixels, but not enough to be the actual text fill colour.
        total = sum(v[0] for v in buckets.values())
        if total < 8:
            return None
        # Pick the mode bucket; tie-break by extreme bucket (the one
        # furthest from bg, since the text fill colour is the most
        # extreme value while any residual edge pixels sit closer to
        # bg). For dark-text-on-light-bg this resolves to the darkest
        # bucket; for light-text-on-dark-bg, to the lightest.
        def _extremity(v: list[int]) -> int:
            avg_r = v[1] // max(1, v[0])
            avg_g = v[2] // max(1, v[0])
            avg_b = v[3] // max(1, v[0])
            dr = avg_r - bg_r
            dg = avg_g - bg_g
            db = avg_b - bg_b
            return dr * dr + dg * dg + db * db

        best = max(
            buckets.values(),
            key=lambda v: (v[0], _extremity(v)),
        )
        n = best[0]
        return (
            best[1] / n / 255.0,
            best[2] / n / 255.0,
            best[3] / n / 255.0,
        )

    @staticmethod
    def _extract_font_buffer(doc, xref: int) -> Optional[bytes]:
        """Return the font binary stream for ``xref`` (or None if not extractable)."""
        if not xref:
            return None
        try:
            tup = doc.extract_font(xref)
        except Exception:
            return None
        if not tup:
            return None
        # PyMuPDF returns (basename, ext, type, buffer)
        try:
            buf = tup[3] if len(tup) >= 4 else None
        except Exception:
            buf = None
        if not buf:
            return None
        return bytes(buf)

    @staticmethod
    def _count_text_matches(
        haystack: str, needle: str, *, case_insensitive: bool = False
    ) -> int:
        """Number of non-overlapping occurrences of ``needle`` in
        ``haystack``. Mirrors ``str.count`` but stays explicit so
        the call site reads as "expected number of search_for matches".

        ``case_insensitive`` must mirror the matching mode used to
        produce ``rects``: PyMuPDF's ``page.search_for`` is
        case-insensitive by default, so the planning loop measures
        the count case-insensitively too whenever the rule allows
        it. Mismatching the modes returns zero matches for purely
        lowercase pages searched with a Camel-Cased ``needle`` and
        causes ``_cluster_rects`` to drop every rect, which would
        silently skip PASS 1+2 and let PASS 3 fall back to a generic
        helv stamp.
        """
        if not needle:
            return 0
        if case_insensitive:
            return haystack.lower().count(needle.lower())
        return haystack.count(needle)

    @staticmethod
    def _cluster_rects(rects: list, expected_matches: int) -> list[list]:
        """Group rects returned by ``page.search_for`` into clusters of
        rects belonging to the *same* logical match.

        PyMuPDF returns one rect per visible glyph cluster; a long
        ``from_`` that wraps across two or three lines therefore yields
        two or three rects for a *single* match in the underlying text.
        Naively iterating over those rects and inserting the placeholder
        on each would draw the same replacement two or three times in a
        row (the "Lab1 (…)  Lab1 (…)  Lab1 (…)" duplicate-render bug).

        Strategy: rely on the fact that ``apply_to_text`` already tells
        us how many true matches there are in the page text. If
        ``expected_matches`` is 1 and we got several rects, all rects
        belong to the same match (most common wrap case). For larger
        ``expected_matches`` we partition the rect list in document
        flow order (top-to-bottom, left-to-right): each consecutive
        chunk of ``ceil(len(rects) / expected_matches)`` rects becomes
        one cluster. This is approximate but safe, the worst case is
        a placeholder on the wrong line, never the duplicate-render bug.
        """
        if expected_matches <= 0 or not rects:
            return []
        ordered = sorted(rects, key=lambda r: (round(float(r[1]) / 4.0), float(r[0])))
        if expected_matches == 1:
            return [ordered]
        if len(ordered) == expected_matches:
            return [[r] for r in ordered]
        # len(ordered) > expected_matches: partition in flow order.
        per = max(1, (len(ordered) + expected_matches - 1) // expected_matches)
        clusters: list[list] = []
        i = 0
        while i < len(ordered):
            clusters.append(ordered[i : i + per])
            i += per
        # If we created too many clusters (rounding), merge the trailing
        # ones into the last expected cluster.
        while len(clusters) > expected_matches:
            tail = clusters.pop()
            clusters[-1].extend(tail)
        return clusters

    @staticmethod
    def _flatten_chars(page) -> tuple[str, list[tuple[float, float, float, float]]]:
        """Walk ``page.get_text("rawdict")`` and return ``(flat_text,
        char_bboxes)`` aligned char-by-char.

        Used to redact occurrences that ``page.search_for`` could not
        find because the underlying spans split the leak across
        multiple PDF text-objects. The deterministic apply layer
        already knows the leak (the map's ``from`` value), so once we
        spot it in the flattened text we can union the per-char
        bounding boxes and redact the union.
        """
        try:
            d = page.get_text("rawdict")
        except Exception:
            return "", []
        flat: list[str] = []
        bboxes: list[tuple[float, float, float, float]] = []
        for blk in d.get("blocks", []) or []:
            for line in blk.get("lines", []) or []:
                for span in line.get("spans", []) or []:
                    for ch in span.get("chars", []) or []:
                        text = ch.get("c", "")
                        if not isinstance(text, str) or not text:
                            continue
                        bb = ch.get("bbox") or [0.0, 0.0, 0.0, 0.0]
                        try:
                            tup = (
                                float(bb[0]),
                                float(bb[1]),
                                float(bb[2]),
                                float(bb[3]),
                            )
                        except Exception:
                            continue
                        flat.append(text)
                        bboxes.append(tup)
                # Lines are separated in the linearised text by a
                # newline so substrings spanning a wrap aren't
                # accidentally fused, char_bboxes stays one-to-one
                # with chars only (the newline has no bbox).
                flat.append("\n")
                bboxes.append(None)  # type: ignore[arg-type]
        return "".join(flat), bboxes

    @staticmethod
    def _font_covers(font_obj, text: str) -> bool:
        """True if every char in ``text`` has a non-zero glyph in ``font_obj``.

        Subset fonts embedded in the source PDF only include glyphs for
        characters actually used by the original text. Reusing such a
        font to draw a *placeholder* containing characters that are
        absent from the subset produces invisible ``.notdef`` glyphs
        (the user-visible "white rectangle" bug). We probe each
        character explicitly so we can fall back to a base-14 font
        before drawing.
        """
        if font_obj is None:
            return False
        try:
            for ch in text:
                if font_obj.has_glyph(ord(ch)) == 0:
                    return False
        except Exception:
            return False
        return True

    # ---- write -------------------------------------------------------------

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        import fitz  # type: ignore

        events: list[WriteEvent] = []
        warnings: list[str] = []
        rules = sorted(
            (r for r in substitutions if r.from_), key=lambda r: -len(r.from_)
        )
        with fitz.open(str(src_path)) as doc:
            # Cache: xref -> font binary stream (or None if not extractable).
            font_buffer_cache: dict[int, Optional[bytes]] = {}
            for pi, page in enumerate(doc):
                page_text = page.get_text("text") or ""
                if not page_text.strip():
                    continue

                _new_text, page_events = apply_to_text(
                    page_text, substitutions, seg_id=f"page{pi}"
                )

                # Map ``span["font"]`` (basefont without subset prefix) ->
                # xref of the embedded font. Used in PASS 2 to extract and
                # reuse the original glyph design.
                font_xref_map = self._build_page_font_xref_map(page)

                # Rectangles whose PASS 2 reinsertion failed for *every*
                # font path. After PASS 2 we rasterize the page and
                # overlay these placeholders directly on the bitmap, so
                # the user never sees a blank white box.
                rasterize_page_rects: list[tuple[
                    float, float, float, float, str, float,
                    tuple[float, float, float], str, object,
                ]] = []

                claimed: list[tuple[float, float, float, float]] = []
                # Per-rule cluster bookkeeping for the rendered diff
                # view: maps each rule's ``from_`` value to the
                # in-document order of clusters that survived the
                # dedup. Used below to enrich ``WriteEvent.rects`` so
                # the GUI can overlay highlights on the rasterised
                # page without re-running ``search_for``.
                clusters_by_from: dict[str, list[list[tuple[float, float, float, float]]]] = {}
                planned: list[
                    tuple[
                        object,  # rect
                        str,     # orig
                        str,     # repl
                        float,   # size
                        int,     # color_int (span metadata, fallback)
                        str,     # font name
                        int,     # flags
                        Optional[tuple[float, float]],  # origin
                        SubstitutionRule,
                        Optional[tuple[float, float, float]],  # text_rgb sampled
                    ]
                ] = []
                for r in rules:
                    flags = (
                        getattr(fitz, "TEXT_DEHYPHENATE", 0)
                        | getattr(fitz, "TEXT_PRESERVE_LIGATURES", 0)
                    )
                    rects: list = []
                    try:
                        rects = list(page.search_for(r.from_, flags=flags) or [])
                    except TypeError:
                        rects = list(page.search_for(r.from_) or [])
                    except Exception:
                        rects = []
                    if r.case_insensitive and not rects:
                        try:
                            rects = list(
                                page.search_for(r.from_.lower(), flags=flags) or []
                            )
                        except Exception:
                            rects = []
                    # Group rects that PyMuPDF returns for a SINGLE multi-line
                    # match (long ``from_`` that wraps across visual lines).
                    # We render the placeholder only on the first rect of
                    # each cluster; the trailing rects are still redacted
                    # but no second copy of the placeholder is drawn over
                    # them, that's the bug that produced ``Lab1 (…) Lab1
                    # (…) Lab1 (…)`` triple-rendering on wrapped table cells.
                    expected_matches = self._count_text_matches(
                        page_text, r.from_, case_insensitive=r.case_insensitive
                    )
                    rect_clusters = self._cluster_rects(rects, expected_matches)
                    for cluster in rect_clusters:
                        # Skip if ANY rect in the cluster overlaps an already
                        # claimed rect (dedupe across rules).
                        if any(
                            self._rects_overlap(
                                (float(rc[0]), float(rc[1]),
                                 float(rc[2]), float(rc[3])),
                                c,
                            )
                            for rc in cluster
                            for c in claimed
                        ):
                            continue
                        # Record this cluster (in document order) so we
                        # can attach its rects to the matching
                        # ``WriteEvent`` below.
                        cluster_rects: list[tuple[float, float, float, float]] = []
                        # The first rect carries the placeholder; the rest
                        # are tagged with ``repl=""`` so they get the
                        # redaction white-fill but no glyphs.
                        for idx_in_cluster, rect in enumerate(cluster):
                            rect_t = (
                                float(rect[0]),
                                float(rect[1]),
                                float(rect[2]),
                                float(rect[3]),
                            )
                            cluster_rects.append(rect_t)
                            claimed.append(rect_t)
                            font, size, color, origin, span_flags = (
                                self._find_span_props(
                                    page,
                                    rect,
                                    default_font="helv",
                                    default_size=10.0,
                                    default_color=0,
                                )
                            )
                            text_for_rect = r.to if idx_in_cluster == 0 else ""
                            # Sample the actually-rendered text colour
                            # before PASS 1 wipes the rect. We need the
                            # background colour first so we can threshold
                            # text-vs-background pixels. Both samplers
                            # tolerate any failure and return safe
                            # defaults.
                            sampled_bg = self._sample_bg_color(page, rect)
                            sampled_text = self._sample_text_color(
                                page, rect, sampled_bg
                            )
                            planned.append(
                                (
                                    rect,
                                    r.from_,
                                    text_for_rect,
                                    size,
                                    color,
                                    font,
                                    span_flags,
                                    origin,
                                    r,
                                    sampled_text,
                                )
                            )
                        clusters_by_from.setdefault(r.from_, []).append(cluster_rects)

                # Annotate page_events with the page index + the on-page
                # rects the GUI rendered-diff overlay needs. We pop one
                # cluster per (rule, occurrence) in document order,
                # which matches ``apply_to_text``'s ordering.
                pop_idx: dict[str, int] = {}
                for ev in page_events:
                    ev.page = pi
                    clusters = clusters_by_from.get(ev.from_, [])
                    i = pop_idx.get(ev.from_, 0)
                    if i < len(clusters):
                        ev.rects = clusters[i]
                        pop_idx[ev.from_] = i + 1

                if not planned:
                    if page_events:
                        events.extend(page_events)
                    continue

                # PASS 1: text-only redaction on **expanded** rects so
                # italic / kerning tails are fully covered. PASS 2 still
                # uses the tight ``search_for`` rect for width + placement.
                #
                # We pass ``fill=False`` so PyMuPDF removes the glyphs
                # from the content stream WITHOUT painting a fill
                # rectangle on top. Painting a fill (even one colour-
                # matched to the background) leaves a visible rectangle
                # whenever the underlying graphics have antialiased
                # edges, gradients, row stripes, table borders, or any
                # subtle texture: a flat fill on top of an antialiased
                # cell shows a sharp boundary even when the centre
                # colour matches. Skipping the fill lets the original
                # cell / code-block / page background show through
                # unchanged. The post-redaction ``page.search_for(orig)``
                # check below still catches any leftover glyph and warns,
                # so security (no residual text) is preserved.
                redact_opts: dict = {}
                tr = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", None)
                if tr is not None:
                    redact_opts["text"] = tr
                im = getattr(fitz, "PDF_REDACT_IMAGE_NONE", None)
                if im is not None:
                    redact_opts["images"] = im
                ga = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", None)
                if ga is not None:
                    redact_opts["graphics"] = ga

                for rect, orig, *_rest in planned:
                    try:
                        rx0 = float(rect[0]) - 0.3
                        ry0 = float(rect[1]) - 1.0
                        rx1 = float(rect[2]) + 0.3
                        ry1 = float(rect[3]) + 1.0
                        exp = fitz.Rect(rx0, ry0, rx1, ry1)
                        page.add_redact_annot(exp, fill=False)
                    except Exception as e:
                        warnings.append(
                            f"page {pi+1}: add_redact_annot failed: {e}"
                        )
                try:
                    if redact_opts:
                        try:
                            page.apply_redactions(**redact_opts)
                        except TypeError:
                            page.apply_redactions()
                    else:
                        page.apply_redactions()
                except Exception as e:
                    warnings.append(f"page {pi+1}: apply_redactions failed: {e}")

                _sflags = (
                    getattr(fitz, "TEXT_DEHYPHENATE", 0)
                    | getattr(fitz, "TEXT_PRESERVE_LIGATURES", 0)
                )
                for rect, orig, *_rest in planned:
                    try:
                        still = []
                        try:
                            still = list(
                                page.search_for(orig, flags=_sflags) or []
                            )
                        except TypeError:
                            still = list(page.search_for(orig) or [])
                        rx0, ry0, rx1, ry1 = (
                            float(rect[0]),
                            float(rect[1]),
                            float(rect[2]),
                            float(rect[3]),
                        )
                        for hit in still:
                            if self._rects_overlap(
                                (
                                    float(hit[0]),
                                    float(hit[1]),
                                    float(hit[2]),
                                    float(hit[3]),
                                ),
                                (rx0, ry0, rx1, ry1),
                                tol=2.0,
                            ):
                                warnings.append(
                                    f"page {pi+1}: text-not-removed after redaction "
                                    f"for match {orig!r}"
                                )
                                break
                    except Exception as e:
                        warnings.append(
                            f"page {pi+1}: post-redact check failed: {e}"
                        )

                # PASS 2: insert replacement text reusing the original
                # font (when we can extract it) at the original baseline.
                # Per-page alias map so we don't re-register the same font.
                page_font_aliases: dict[int, str] = {}

                for (
                    rect,
                    orig_text,
                    repl,
                    size,
                    color_int,
                    font,
                    span_flags,
                    origin,
                    rule,
                    sampled_text_rgb,
                ) in planned:
                    # Trailing rect of a multi-line wrapped match: PASS 1
                    # already redacted it. We MUST NOT draw the
                    # placeholder again, that would reproduce the
                    # ``Lab1 (...) Lab1 (...)`` triple-render bug.
                    if not repl:
                        continue
                    # Prefer the colour we sampled from the rendered
                    # glyphs (ground truth: what the user actually
                    # sees). Fall back to the span metadata colour when
                    # sampling failed (rect too small, render error).
                    if sampled_text_rgb is not None:
                        color = sampled_text_rgb
                    else:
                        color = self._color_from_int(color_int)
                    width = rect[2] - rect[0]

                    # Try to register and reuse the original embedded font,
                    # but only if the subset actually contains every glyph
                    # we need to draw. Otherwise PyMuPDF would render
                    # ``.notdef`` for missing chars (invisible squares),
                    # producing the "blank rectangle" regression.
                    primary_alias: Optional[str] = None
                    xref = font_xref_map.get(font, 0)
                    if xref:
                        if xref not in font_buffer_cache:
                            font_buffer_cache[xref] = self._extract_font_buffer(
                                doc, xref
                            )
                        buf = font_buffer_cache[xref]
                        if buf:
                            try:
                                font_obj = fitz.Font(fontbuffer=buf)
                            except Exception:
                                font_obj = None
                            if not self._font_covers(font_obj, repl):
                                # Subset doesn't have all glyphs the
                                # placeholder needs. Use base-14 fallback
                                # for this rectangle (other rectangles
                                # may still benefit from the original
                                # font if their placeholders fit).
                                buf = None
                        if buf:
                            alias = page_font_aliases.get(xref)
                            if alias is None:
                                alias = f"F{xref}"
                                try:
                                    page.insert_font(
                                        fontname=alias, fontbuffer=buf
                                    )
                                    page_font_aliases[xref] = alias
                                except Exception as e:
                                    warnings.append(
                                        f"page {pi+1}: insert_font(F{xref}) failed: {e}; "
                                        "falling back to base-14"
                                    )
                                    alias = None
                            primary_alias = alias

                    base14_alias = self._safe_fontname(font, flags=span_flags)
                    # Try the original font first, then base-14 (which is
                    # always available in PyMuPDF). The fallback chain
                    # guarantees that every redacted rectangle ends up
                    # with a glyph drawn over it, no more white boxes.
                    candidates = []
                    if primary_alias:
                        candidates.append(primary_alias)
                    if base14_alias and base14_alias not in candidates:
                        candidates.append(base14_alias)
                    for safe in ("helv", "tiro", "cour"):
                        if safe not in candidates:
                            candidates.append(safe)

                    if origin and len(origin) >= 2:
                        baseline_x = float(rect[0])
                        baseline_y = float(origin[1])
                    else:
                        baseline_x = float(rect[0])
                        baseline_y = float(rect[3]) - size * 0.18

                    drawn = False
                    last_error: Optional[str] = None
                    for fontname_for_insert in candidates:
                        cur_size = size
                        # Fit the replacement against a *fair* width
                        # budget. ``rect`` comes from ``page.search_for``,
                        # which returns the visual glyph bounding box.
                        # That bbox is NARROWER than the cursor advance
                        # the original characters consumed, especially
                        # for monospace fonts where the bbox of e.g.
                        # "nimbusboard" hugs the glyph extents while the
                        # advance leaves uniform per-character cells.
                        # Comparing the placeholder's advance width
                        # against the bbox width therefore over-reports
                        # overflow, triggering an unnecessary shrink:
                        # ``nimbusboard`` (11) -> ``VendorBoard`` (11) in
                        # mono is a perfect length match, but the naive
                        # check shrinks it to ~85% because Courier's
                        # advance for 11 chars exceeds the visual bbox
                        # of the original 11 chars. The fair budget is
                        # ``max(rect_width, orig_advance_in_same_font)``:
                        # if the placeholder fits in the same horizontal
                        # advance the original would consume in this
                        # font, it definitely fits where the original
                        # glyphs lived.
                        try:
                            orig_advance = fitz.get_text_length(
                                orig_text,
                                fontname=fontname_for_insert,
                                fontsize=size,
                            )
                        except Exception:
                            orig_advance = 0.0
                        target_width = max(width, orig_advance)
                        fits = False
                        while cur_size >= size * 0.5:
                            try:
                                tw = fitz.get_text_length(
                                    repl,
                                    fontname=fontname_for_insert,
                                    fontsize=cur_size,
                                )
                            except Exception:
                                tw = target_width
                            if tw <= target_width:
                                fits = True
                                break
                            cur_size *= 0.95
                        if not fits:
                            # Try the next font, its metrics might fit.
                            last_error = (
                                f"font={fontname_for_insert!r}: width overflow "
                                f"({tw:.1f} > {target_width:.1f})"
                            )
                            continue
                        if origin and len(origin) >= 2:
                            text_y = baseline_y
                        else:
                            text_y = float(rect[3]) - cur_size * 0.18
                        try:
                            page.insert_text(
                                (baseline_x, text_y),
                                repl,
                                fontname=fontname_for_insert,
                                fontsize=cur_size,
                                color=color,
                            )
                            drawn = True
                            if cur_size < size * 0.5 + 0.001:
                                warnings.append(
                                    f"page {pi+1}: replacement {repl!r} shrunk "
                                    f"below 50% to fit (font={fontname_for_insert})"
                                )
                            break
                        except Exception as e:
                            last_error = (
                                f"font={fontname_for_insert!r}: {e}"
                            )
                            continue

                    if not drawn:
                        # No font produced a width that fits in the
                        # rectangle. The placeholder is significantly
                        # wider than the source, usually a stale map
                        # entry such as ``+39LAB`` (5 chars) ->
                        # ``+390000000001`` (13 chars) which the LLM
                        # promoted long before length-preservation was
                        # enforced. We have two defenses, applied in
                        # order:
                        #   1. ``insert_textbox`` clips the text inside
                        #      the rect (returns a negative number when
                        #      it cannot fit even shrunk down).
                        #   2. If the textbox refuses, truncate the
                        #      placeholder character-wise until the
                        #      shortest readable suffix fits.
                        # Either way, the placeholder NEVER spills into
                        # the adjacent column / next argument.
                        tb_rect = fitz.Rect(
                            float(rect[0]),
                            float(rect[1]),
                            float(rect[2]),
                            float(rect[3]),
                        )
                        for tb_font in ("helv", "cour", "tiro"):
                            try:
                                rc = page.insert_textbox(
                                    tb_rect,
                                    repl,
                                    fontname=tb_font,
                                    fontsize=max(4.0, size),
                                    color=color,
                                    align=0,
                                )
                            except Exception:
                                continue
                            if isinstance(rc, (int, float)) and rc >= 0:
                                drawn = True
                                break
                        if not drawn:
                            # Truncate the placeholder until it fits at
                            # the smallest readable size.
                            truncated = repl
                            while len(truncated) > 1:
                                truncated = truncated[:-1]
                                try:
                                    tw_t = fitz.get_text_length(
                                        truncated, fontname="helv",
                                        fontsize=max(4.0, size * 0.5),
                                    )
                                except Exception:
                                    tw_t = width + 1
                                if tw_t <= width:
                                    break
                            try:
                                page.insert_text(
                                    (float(rect[0]),
                                     float(origin[1])
                                     if origin and len(origin) >= 2
                                     else float(rect[3]) - size * 0.18),
                                    truncated,
                                    fontname="helv",
                                    fontsize=max(4.0, size * 0.5),
                                    color=color,
                                )
                                drawn = True
                                warnings.append(
                                    f"page {pi+1}: placeholder {repl!r} "
                                    f"truncated to {truncated!r} to fit rect "
                                    f"(check substitution_map.yml: "
                                    f"to is much longer than from)"
                                )
                            except Exception as e:
                                warnings.append(
                                    f"page {pi+1}: every insert_text path "
                                    f"failed for {repl!r}: {last_error or e}"
                                )

                    if not drawn:
                        # Record the rectangle so the page-level
                        # recovery pass below can rasterize the page.
                        rasterize_page_rects.append(
                            (
                                float(rect[0]),
                                float(rect[1]),
                                float(rect[2]),
                                float(rect[3]),
                                repl,
                                size,
                                color,
                                base14_alias,
                                origin,
                            )
                        )

                # PASS 3: char-level redaction for substitutions whose
                # ``from`` text is still visible on the page after
                # PASS 1+2. PyMuPDF's ``page.search_for`` misses
                # occurrences that the source split across multiple
                # text-objects (different fonts on the same line, kerning
                # tails, ligatures); those would otherwise survive as
                # ``regression:from-value`` residuals and force the
                # operator to triage things they have already approved.
                # We scan the rendered ``rawdict`` instead, find the
                # leak text on the flat char stream and redact the
                # union of per-character bboxes.
                try:
                    page_text_after = page.get_text("text") or ""
                except Exception:
                    page_text_after = ""
                stuck_rules: list[SubstitutionRule] = []
                for r in rules:
                    if not r.from_:
                        continue
                    # Identity / supersetting placeholders legitimately
                    # leave the ``from`` text visible in the output -
                    # it IS the placeholder. Don't redact those again.
                    if r.to == r.from_ or (r.from_ and r.from_ in r.to):
                        continue
                    if r.from_ in page_text_after:
                        stuck_rules.append(r)
                    elif r.case_insensitive and r.from_.lower() in page_text_after.lower():
                        stuck_rules.append(r)
                if stuck_rules:
                    flat, char_bboxes = self._flatten_chars(page)
                    if flat:
                        flat_lower = flat.lower()
                        for r in stuck_rules:
                            needle = r.from_
                            haystack = flat
                            if r.case_insensitive:
                                needle = needle.lower()
                                haystack = flat_lower
                            start = 0
                            while True:
                                hit = haystack.find(needle, start)
                                if hit == -1:
                                    break
                                start = hit + max(1, len(needle))
                                # Aggregate bboxes (skip None entries
                                # introduced for line-separators).
                                rect_items = [
                                    char_bboxes[i]
                                    for i in range(hit, hit + len(needle))
                                    if i < len(char_bboxes)
                                    and char_bboxes[i] is not None
                                ]
                                if not rect_items:
                                    continue
                                # Redact + reinsert per visible line
                                # (group bboxes whose y0 are within 1pt).
                                rect_items.sort(key=lambda b: (round(b[1]), b[0]))
                                groups: list[list[tuple[float, float, float, float]]] = []
                                cur: list[tuple[float, float, float, float]] = []
                                for bb in rect_items:
                                    if cur and abs(cur[-1][1] - bb[1]) > 1.5:
                                        groups.append(cur)
                                        cur = []
                                    cur.append(bb)
                                if cur:
                                    groups.append(cur)
                                first_group = True
                                for group in groups:
                                    union = (
                                        min(b[0] for b in group),
                                        min(b[1] for b in group),
                                        max(b[2] for b in group),
                                        max(b[3] for b in group),
                                    )
                                    rrect = fitz.Rect(*union)
                                    try:
                                        # See PASS 1: ``fill=False`` lets
                                        # the original cell / block
                                        # background show through cleanly
                                        # rather than painting a flat
                                        # rectangle whose edges would be
                                        # visible against any antialiased
                                        # underlying graphics.
                                        page.add_redact_annot(
                                            rrect, fill=False
                                        )
                                    except Exception as e:
                                        warnings.append(
                                            f"page {pi+1}: pass3 redact failed "
                                            f"for {needle!r}: {e}"
                                        )
                                        continue
                                    page.apply_redactions(
                                        **(redact_opts or {})
                                    )
                                    if first_group:
                                        first_group = False
                                        try:
                                            page.insert_text(
                                                (rrect.x0, rrect.y1 - 1.0),
                                                r.to,
                                                fontname="helv",
                                                fontsize=max(
                                                    4.0, (rrect.y1 - rrect.y0) * 0.85
                                                ),
                                                color=(0, 0, 0),
                                            )
                                        except Exception as e:
                                            warnings.append(
                                                f"page {pi+1}: pass3 insert "
                                                f"failed for {r.to!r}: {e}"
                                            )

                if rasterize_page_rects:
                    # Last-resort recovery: rasterize the whole page and
                    # overlay every still-blank rectangle with placeholder
                    # text in a guaranteed font. The page becomes a
                    # bitmap (selectable text is lost on this page) but
                    # the user never sees a white box where a leak used
                    # to be, which is the explicit acceptance criterion.
                    try:
                        page_rect = page.rect
                        zoom = 2.0
                        pix = page.get_pixmap(
                            matrix=fitz.Matrix(zoom, zoom), alpha=False
                        )
                        png_bytes = pix.tobytes("png")
                        page.clean_contents()
                        page.insert_image(page_rect, stream=png_bytes)
                        for (
                            rx0, ry0, rx1, ry1, repl_t, size_t, color_t,
                            font_t, origin_t,
                        ) in rasterize_page_rects:
                            try:
                                # Wipe the area first so the bitmap text
                                # underneath is fully covered, then draw
                                # the placeholder in plain Helvetica. The
                                # fill colour is sampled from the rasterized
                                # page just outside the rect, so coloured
                                # backgrounds (dark code blocks, hero
                                # banners) keep their look.
                                rect_obj = fitz.Rect(rx0, ry0, rx1, ry1)
                                bg_fill = self._sample_bg_color(
                                    page, rect_obj
                                )
                                page.draw_rect(
                                    rect_obj,
                                    color=None, fill=bg_fill,
                                    overlay=True,
                                )
                                ox = rx0
                                oy = (
                                    float(origin_t[1])
                                    if origin_t and len(origin_t) >= 2
                                    else ry1 - size_t * 0.18
                                )
                                page.insert_text(
                                    (ox, oy),
                                    repl_t,
                                    fontname="helv",
                                    fontsize=size_t,
                                    color=color_t,
                                )
                            except Exception as e:
                                warnings.append(
                                    f"page {pi+1}: rasterize-overlay "
                                    f"failed for {repl_t!r}: {e}"
                                )
                    except Exception as e:
                        warnings.append(
                            f"page {pi+1}: rasterize fallback failed: {e}"
                        )

                events.extend(page_events)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(dst_path), garbage=4, deflate=True, clean=True)
        return WriteReport(file_rel=str(dst_path), events=events, warnings=warnings)


__all__ = ["PdfInplaceAdapter"]
