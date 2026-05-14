"""PDF re-derive adapter.

Extracts text via PyMuPDF (``fitz.Page.get_text()``), applies
substitutions on the extracted text, and re-renders a fresh PDF
via ``pandoc + WeasyPrint``. The output looks "clean" but loses the
original layout, fonts, and embedded images. Marked
``is_lossy=True``.

Why PyMuPDF and not ``pdftotext -layout``: ``pdftotext -layout``
emits ONE space between letter-spaced glyphs ("P R I M E"), which
pandoc collapses to "PRIME" in the rendered HTML. PyMuPDF emits
two spaces between word groups, so word boundaries survive the
pandoc round-trip. PyMuPDF also doesn't pad columns with whitespace,
which means tables of pdftotext output (4-space-indented lines)
no longer get rendered as code blocks by accident.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import (
    FormatAdapter,
    Segment,
    SubstitutionRule,
    WriteEvent,
    WriteReport,
    apply_to_text,
)


_DEFAULT_CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body  { font: 11pt/1.45 'Inter','Liberation Sans',Arial,sans-serif; color:#1a1a1a; }
h1,h2,h3,h4 { font-family: 'Inter','Liberation Sans',Arial,sans-serif; }
pre   { background:#f4f4f4; padding:6px 10px; border-radius:4px; font-size:10pt; white-space:pre-wrap; }
code  { background:#f4f4f4; padding:0 3px; border-radius:3px; font-size:10pt; }
table { border-collapse: collapse; width: 100%; }
th,td { border: 1px solid #999; padding: 4px 6px; text-align: left; }
"""


def _which(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} is required for pdf_rederive adapter")
    return p


class PdfRederiveAdapter(FormatAdapter):
    name = "pdf_rederive"
    extensions = {".pdf"}
    mimes = {"application/pdf"}
    is_lossy = True

    def __init__(self) -> None:
        self._pandoc = _which("pandoc")

    def _extract_text(self, path: Path) -> str:
        # PyMuPDF (already a hard dependency for the pdf_inplace path).
        # ``page.get_text()`` defaults to the "text" extractor which
        # groups glyphs into word boundaries, so letter-spaced headers
        # survive the pandoc collapse intact (see module docstring).
        try:
            import fitz  # type: ignore[import-not-found]
        except Exception as e:
            raise RuntimeError(
                f"PyMuPDF (fitz) required for pdf_rederive: {e}"
            )
        try:
            with fitz.open(str(path)) as doc:
                parts = [page.get_text() for page in doc]
        except Exception as e:
            raise RuntimeError(f"PyMuPDF failed for {path}: {e}")
        return "\n\n".join(parts)

    def extract(self, path: Path) -> list[Segment]:
        return [Segment(seg_id="0", text=self._extract_text(path))]

    def _render(self, md: str, dst: Path) -> None:
        # Honour the project-level template choice (set by the pipeline
        # via ``format_adapters.set_export_template``). Falls back to
        # the plain ``_DEFAULT_CSS`` rendering when no template is set
        # or template look-up fails, never error apply just because
        # a template went missing.
        try:
            from . import get_export_template
            tmpl_id = get_export_template()
        except Exception:
            tmpl_id = None
        if tmpl_id:
            try:
                from ..templates import (
                    TemplateContext,
                    get_template,
                    render_pdf_with_template,
                )
                tmpl = get_template(tmpl_id)
                if tmpl is not None:
                    ctx = TemplateContext(title=dst.stem)
                    # ``with_cover=False`` strips the template's cover
                    # header. Re-derive has no meaningful metadata to
                    # populate Engagement / Author / Date with, so a
                    # forced cover lands as an empty stub. Operators
                    # who want a full cover use Export… on the
                    # anonymised text after the run.
                    render_pdf_with_template(
                        md=md, template=tmpl, ctx=ctx, dst=dst,
                        with_cover=False,
                    )
                    return
            except Exception:
                pass

        with tempfile.TemporaryDirectory(prefix="anon_pdf_") as tmp:
            tmp_path = Path(tmp)
            css = tmp_path / "style.css"
            css.write_text(_DEFAULT_CSS, encoding="utf-8")
            html = tmp_path / "out.html"
            proc = subprocess.run(
                [
                    self._pandoc,
                    "-f",
                    "markdown",
                    "-t",
                    "html5",
                    "--standalone",
                    "--embed-resources",
                    "--css",
                    str(css),
                    "-o",
                    str(html),
                ],
                input=md,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"pandoc md->html failed: {proc.stderr.strip()[:300]}"
                )
            # WeasyPrint replaces the wkhtmltopdf subprocess. ``base_url``
            # points at the tempdir so the embedded ``style.css`` link
            # in the pandoc-emitted HTML resolves correctly.
            from ..pdf_render import render_html_to_pdf
            try:
                render_html_to_pdf(
                    html.read_text(encoding="utf-8"),
                    dst,
                    base_url=tmp_path,
                )
            except Exception as e:
                raise RuntimeError(f"weasyprint failed: {str(e)[:300]}")

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        text = self._extract_text(src_path)
        new_text, events = apply_to_text(text, substitutions, seg_id="0")
        # PyMuPDF emits glyphs grouped by word, not padded columns, so
        # there's no columnar whitespace to preserve and no reason to
        # wrap the output in a markdown code fence. The legacy code
        # fence wrapper existed for ``pdftotext -layout`` output and
        # is now dropped unconditionally, same flow whether a
        # template is set or not, just different downstream styling.
        md = new_text
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        self._render(md, dst_path)
        report = WriteReport(file_rel=str(dst_path), events=events, is_lossy=True)
        report.warnings.append(
            "PDF re-rendered: layout, fonts and embedded images are lost."
        )
        return report


__all__ = ["PdfRederiveAdapter"]
