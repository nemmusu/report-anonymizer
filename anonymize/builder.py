"""Rebuild PDF/HTML standalone artefacts for the anonymized mirror.

Production hardening:
  * pandoc subprocess wrapped in :func:`_run_killable` which polls
    ``stop_event`` and ``terminate()/kill()`` the child if cancellation
    is requested - so a global Stop button actually cancels long renders.
  * HTML -> PDF goes through WeasyPrint (pure-Python, Pango/Cairo
    based) via :func:`anonymize.pdf_render.render_html_to_pdf`,
    replacing the legacy wkhtmltopdf subprocess.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional


PAGE_BREAK = '\n\n<div style="page-break-after: always;"></div>\n\n'

DEFAULT_CSS = """\
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body  { font: 11pt/1.45 'Inter','Liberation Sans',Arial,sans-serif; color:#1a1a1a; }
h1,h2,h3,h4 { font-family: 'Inter','Liberation Sans',Arial,sans-serif; }
h1 { border-bottom: 1px solid #999; padding-bottom: 4px; margin-top: 24px; }
pre   { background:#f4f4f4; padding:6px 10px; border-radius:4px; font-size:10pt; white-space:pre-wrap; }
code  { background:#f4f4f4; padding:0 3px; border-radius:3px; font-size:10pt; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th,td { border: 1px solid #999; padding: 4px 6px; text-align: left; vertical-align: top; }
"""


@dataclass
class BuildReport:
    artefacts: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


class Cancelled(Exception):
    pass


def _which(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} required for builder")
    return p


def _read_optional(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _run_killable(
    cmd: list[str],
    *,
    stdin: Optional[str] = None,
    timeout: int = 240,
    stop_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess that can be terminated cooperatively via stop_event."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + timeout
        # When we have stdin, use a small writer thread so we can simultaneously
        # read stdout/stderr (avoiding pipe-buffer deadlocks on large MD inputs)
        # while still polling the cancellation event.
        out_buf: list[str] = []
        err_buf: list[str] = []

        def _drain(stream, buf):
            try:
                for chunk in iter(lambda: stream.read(65536), ""):
                    if not chunk:
                        break
                    buf.append(chunk)
            except Exception:
                pass

        def _writer(stream, payload):
            try:
                stream.write(payload)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threads: list[threading.Thread] = []
        if proc.stdout is not None:
            t = threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True)
            t.start()
            threads.append(t)
        if proc.stderr is not None:
            t = threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)
            t.start()
            threads.append(t)
        if stdin is not None and proc.stdin is not None:
            t = threading.Thread(target=_writer, args=(proc.stdin, stdin), daemon=True)
            t.start()
            threads.append(t)

        while True:
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    proc.kill()
                raise Cancelled()
            if proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.05)
        for t in threads:
            t.join(timeout=2.0)
        return subprocess.CompletedProcess(
            cmd, proc.returncode or 0, "".join(out_buf), "".join(err_buf)
        )
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def _bundle_for_folder(folder: Path) -> str:
    parts: list[str] = []

    def add(p: Path) -> None:
        if p.exists():
            parts.append(_read_optional(p))

    cover = folder / "cover.md"
    readme = folder / "README.md"
    advisory = folder / "ADVISORY.md"
    breakthrough = sorted(folder.glob("BREAKTHROUGH_*.md"))
    exploit = folder / "exploit_usage.md"

    if cover.exists():
        add(cover)
    if readme.exists():
        if parts:
            parts.append(PAGE_BREAK)
        parts.append(_read_optional(readme))
    if advisory.exists():
        if parts:
            parts.append(PAGE_BREAK)
        parts.append(_read_optional(advisory))
    for b in breakthrough:
        if parts:
            parts.append(PAGE_BREAK)
        parts.append(_read_optional(b))
    if exploit.exists():
        if parts:
            parts.append(PAGE_BREAK)
        parts.append(_read_optional(exploit))
    return "".join(parts)


def _aggregate_root_bundle(root: Path) -> str:
    parts: list[str] = []
    for name in ("README.md", "ATTACK_CHAINS.md", "REMEDIATION_PLAN.md"):
        p = root / name
        if not p.exists():
            continue
        if parts:
            parts.append(PAGE_BREAK)
        parts.append(_read_optional(p))
    return "".join(parts)


def render_pdf(
    md: str,
    *,
    dst: Path,
    css: Optional[Path] = None,
    embed_resources: bool = True,
    stop_event: Optional[threading.Event] = None,
    template_id: Optional[str] = None,
) -> None:
    """Render ``md`` to ``dst`` (PDF).

    When ``template_id`` resolves to a known ``TemplateMeta`` we delegate
    to ``templates.render_pdf_with_template`` so the wrapper / CSS combo
    matches what the Export dialog produces. This is the path the Import
    dialog selects when the user picks a template up-front. Otherwise we
    keep the legacy flat ``DEFAULT_CSS`` route, no wrapper, no title
    page, for backward compatibility.
    """
    if template_id:
        try:
            from .templates import (
                TemplateContext,
                get_template,
                render_pdf_with_template,
            )
            tmpl = get_template(template_id)
            if tmpl is not None:
                ctx = TemplateContext(title=dst.stem)
                render_pdf_with_template(
                    md=md, template=tmpl, ctx=ctx, dst=dst, stop_event=stop_event
                )
                return
        except Exception:
            # Template missing / malformed → fall back to plain CSS
            # rendering instead of erroring the whole Build stage.
            pass

    pandoc = _which("pandoc")
    css_path: Path
    if css and css.exists():
        css_path = css
        cleanup_css = False
    else:
        css_path = dst.with_suffix(".style.css")
        css_path.write_text(DEFAULT_CSS, encoding="utf-8")
        cleanup_css = True

    html = dst.with_suffix(".html")
    cmd = [
        pandoc,
        "-f",
        "markdown",
        "-t",
        "html5",
        "--standalone",
        "--css",
        str(css_path),
        "-o",
        str(html),
    ]
    if embed_resources:
        cmd.append("--embed-resources")
    proc = _run_killable(cmd, stdin=md, timeout=180, stop_event=stop_event)
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc html: {proc.stderr.strip()[:300]}")
    # WeasyPrint replaces the wkhtmltopdf subprocess. ``base_url`` is
    # the HTML's parent so relative ``<link rel=stylesheet>`` and image
    # paths resolve identically to the pandoc-emitted layout.
    from .pdf_render import Cancelled as PdfCancelled, render_html_to_pdf
    try:
        render_html_to_pdf(
            html.read_text(encoding="utf-8"),
            dst,
            base_url=html.parent,
            stop_event=stop_event,
        )
    except PdfCancelled:
        raise Cancelled()
    except Exception as e:
        raise RuntimeError(f"weasyprint: {str(e)[:300]}")
    if cleanup_css:
        try:
            css_path.unlink()
        except FileNotFoundError:
            pass


def build_dossier(
    output_root: Path,
    *,
    progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    template_id: Optional[str] = None,
) -> BuildReport:
    """Rebuild PDFs for every subdirectory that contains a ``README.md``."""
    report = BuildReport()
    output_root = Path(output_root).resolve()
    if not output_root.is_dir():
        report.warnings.append(f"output root is not a directory: {output_root}")
        return report

    candidates = sorted(
        d
        for d in output_root.rglob("*")
        if d.is_dir() and (d / "README.md").exists()
    )

    total = len(candidates) + 1
    for i, folder in enumerate(candidates, 1):
        if stop_event is not None and stop_event.is_set():
            report.cancelled = True
            return report
        if progress:
            progress(i, total, str(folder.relative_to(output_root)))
        bundle = _bundle_for_folder(folder)
        if not bundle.strip():
            continue
        slug = folder.name + ".pdf"
        out_pdf = folder / slug
        try:
            render_pdf(
                bundle, dst=out_pdf, stop_event=stop_event, template_id=template_id
            )
            report.artefacts.append(out_pdf)
        except Cancelled:
            report.cancelled = True
            return report
        except Exception as e:
            report.warnings.append(f"{folder.relative_to(output_root)}: {e}")

    if progress:
        progress(total, total, "aggregated")
    if stop_event is not None and stop_event.is_set():
        report.cancelled = True
        return report
    agg = _aggregate_root_bundle(output_root)
    if agg.strip():
        out_pdf = output_root / (output_root.name + "_anonymized.pdf")
        try:
            render_pdf(
                agg, dst=out_pdf, stop_event=stop_event, template_id=template_id
            )
            report.artefacts.append(out_pdf)
        except Cancelled:
            report.cancelled = True
        except Exception as e:
            report.warnings.append(f"root aggregate: {e}")
    return report


def build_single_md(md_path: Path) -> Path:
    """Render a single markdown file to a sibling PDF."""
    md = md_path.read_text(encoding="utf-8")
    out = md_path.with_suffix(".pdf")
    render_pdf(md, dst=out)
    return out


__all__ = ["BuildReport", "build_dossier", "build_single_md", "render_pdf"]
