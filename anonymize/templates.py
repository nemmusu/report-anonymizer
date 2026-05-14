"""Report template registry & renderer.

A *template* describes how anonymized text/markdown content gets wrapped into
a polished standalone HTML/PDF (cover page, header, footer, typography).

Templates live in two locations:

* **builtin**:  ``<repo>/templates/index.yml`` and ``<repo>/templates/<id>/``
  (shipped with the project).
* **user**:     ``~/.config/document-anonymizer/templates/index.yml`` plus
  ``<id>/`` subfolders. The user can add as many as they like; they appear in
  the export dialog alongside the built-ins.

A template entry contains:

  - ``id``          : machine identifier (folder name)
  - ``name``        : display name in the GUI
  - ``description`` : short blurb shown in the picker
  - ``builtin``     : ``true`` for shipped templates
  - ``wrapper``     : path (relative to the templates root) to the HTML
                      wrapper file. The wrapper is a Jinja-like template with
                      ``{{ title }}``, ``{{ subtitle }}``, ``{{ author }}``,
                      ``{{ engagement }}``, ``{{ date }}``, ``{{ classification }}``,
                      ``{{ footer }}``, ``{{ style }}`` and ``{{ body }}``
                      placeholders (simple ``str.replace``, no full Jinja
                      runtime needed).
  - ``style``       : path to the CSS file inlined into the wrapper.

Rendering pipeline:

  ``markdown`` --(pandoc -t html5 fragment)--> body HTML -->
  HTML wrapper with style + metadata --(WeasyPrint)--> PDF.

The PDF is written to the location requested by the caller. Template
discovery is read-only; user templates are added by writing the YAML file
manually (or via the GUI editor wired into the Settings dialog).
"""
from __future__ import annotations

import datetime as _dt
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ._paths import user_config_dir as _user_config_dir
from .builder import Cancelled, _run_killable, _which


CONFIG_DIR = _user_config_dir()
USER_TEMPLATES_DIR = CONFIG_DIR / "templates"
BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class TemplateMeta:
    """Metadata for a single export template."""

    id: str
    name: str
    description: str
    wrapper_path: Path
    style_path: Path
    builtin: bool = True
    source: str = "builtin"  # builtin | user

    @property
    def is_user(self) -> bool:
        return self.source == "user"


@dataclass
class TemplateContext:
    """Variables substituted into the wrapper HTML."""

    title: str = "Anonymized report"
    subtitle: str = ""
    author: str = ""
    engagement: str = ""
    date: str = field(default_factory=lambda: _dt.date.today().isoformat())
    classification: str = "ANONYMIZED · INTERNAL"
    footer: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.author,
            "engagement": self.engagement,
            "date": self.date,
            "classification": self.classification,
            "footer": self.footer,
        }


def _read_optional(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _load_index(root: Path, *, source: str) -> list[TemplateMeta]:
    idx = root / "index.yml"
    if not idx.exists():
        return []
    try:
        data = yaml.safe_load(idx.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[TemplateMeta] = []
    for raw in (data.get("templates") or []):
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or "").strip()
        if not tid:
            continue
        wrapper_rel = str(raw.get("wrapper") or f"{tid}/wrapper.html")
        style_rel = str(raw.get("style") or f"{tid}/style.css")
        wrapper = root / wrapper_rel
        style = root / style_rel
        if not wrapper.exists():
            continue
        out.append(
            TemplateMeta(
                id=tid,
                name=str(raw.get("name") or tid),
                description=str(raw.get("description") or ""),
                wrapper_path=wrapper,
                style_path=style,
                builtin=bool(raw.get("builtin", source == "builtin")),
                source=source,
            )
        )
    return out


def list_templates() -> list[TemplateMeta]:
    """Built-in + user templates merged (user wins on id collision)."""
    builtin = _load_index(BUILTIN_TEMPLATES_DIR, source="builtin")
    user = _load_index(USER_TEMPLATES_DIR, source="user")
    by_id: dict[str, TemplateMeta] = {t.id: t for t in builtin}
    for t in user:
        by_id[t.id] = t
    return [by_id[k] for k in sorted(by_id.keys())]


def get_template(template_id: str) -> Optional[TemplateMeta]:
    for t in list_templates():
        if t.id == template_id:
            return t
    return None


def add_user_template(
    template_id: str,
    *,
    name: str,
    description: str,
    wrapper_html: str,
    style_css: str,
) -> TemplateMeta:
    """Create a new user template under :data:`USER_TEMPLATES_DIR`.

    Both the ``index.yml`` entry and the ``<id>/wrapper.html`` /
    ``<id>/style.css`` files are written. Existing ``id`` is updated in place.
    """
    if not template_id or "/" in template_id or "\\" in template_id:
        raise ValueError(f"invalid template id: {template_id!r}")
    USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    tdir = USER_TEMPLATES_DIR / template_id
    tdir.mkdir(parents=True, exist_ok=True)
    wrapper = tdir / "wrapper.html"
    style = tdir / "style.css"
    wrapper.write_text(wrapper_html, encoding="utf-8")
    style.write_text(style_css, encoding="utf-8")

    idx_path = USER_TEMPLATES_DIR / "index.yml"
    if idx_path.exists():
        try:
            data = yaml.safe_load(idx_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    else:
        data = {}
    templates = list(data.get("templates") or [])
    templates = [t for t in templates if isinstance(t, dict) and t.get("id") != template_id]
    templates.append(
        {
            "id": template_id,
            "name": name,
            "description": description,
            "builtin": False,
            "wrapper": f"{template_id}/wrapper.html",
            "style": f"{template_id}/style.css",
        }
    )
    idx_path.write_text(
        yaml.safe_dump({"version": 1, "templates": templates}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return TemplateMeta(
        id=template_id,
        name=name,
        description=description,
        wrapper_path=wrapper,
        style_path=style,
        builtin=False,
        source="user",
    )


def delete_user_template(template_id: str) -> bool:
    """Remove a user template (built-ins cannot be deleted)."""
    idx_path = USER_TEMPLATES_DIR / "index.yml"
    if not idx_path.exists():
        return False
    try:
        data = yaml.safe_load(idx_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    templates = list(data.get("templates") or [])
    new = [t for t in templates if not (isinstance(t, dict) and t.get("id") == template_id)]
    if len(new) == len(templates):
        return False
    idx_path.write_text(
        yaml.safe_dump({"version": 1, "templates": new}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tdir = USER_TEMPLATES_DIR / template_id
    if tdir.exists():
        try:
            shutil.rmtree(tdir, ignore_errors=True)
        except Exception:
            pass
    return True


# ---- rendering --------------------------------------------------------------


def _markdown_to_html_fragment(
    md: str, *, stop_event: Optional[threading.Event] = None
) -> str:
    """Turn ``md`` into an HTML body fragment via pandoc (no <html> wrapper)."""
    pandoc = _which("pandoc")
    cmd = [pandoc, "-f", "markdown", "-t", "html5", "--wrap=none"]
    proc = _run_killable(cmd, stdin=md, timeout=180, stop_event=stop_event)
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc fragment: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _wrap_html(template: TemplateMeta, *, body_html: str, ctx: TemplateContext) -> str:
    wrapper = _read_optional(template.wrapper_path)
    if not wrapper:
        raise RuntimeError(f"template wrapper missing: {template.wrapper_path}")
    style = _read_optional(template.style_path)
    out = wrapper.replace("{{ style }}", style)
    out = out.replace("{{ body }}", body_html)
    for k, v in ctx.to_dict().items():
        out = out.replace("{{ " + k + " }}", str(v))
    return out


def _wrap_html_no_cover(template: TemplateMeta, *, body_html: str) -> str:
    """Minimal wrapper: template's CSS, no cover header / metadata.

    Used by render paths that don't have meaningful Engagement / Author
    / Date metadata to fill in (e.g. the PDF re-derive adapter that
    extracts text from a source PDF and re-renders it). Forcing the
    template's cover page on those flows produced a fake "Penetration
    Testing Report" cover with empty fields - confusing to operators.
    Stripping the wrapper to just the styled body keeps the visual
    upgrade (typography, headings, paged margins) without the lie.

    The injected ``<pre>`` rule guarantees the PDF doesn't overflow
    its page width when the template doesn't already pin
    ``white-space: pre-wrap`` (the modern + classic ones don't).

    The ``@page`` chrome reset suppresses the running header / page-
    number margin boxes that the new templates emit: those rely on
    ``string-set`` values populated from the cover elements, which
    are absent here, so without the reset every body page would draw
    an empty border-strip and an empty footer line. The reset also
    restores a sensible body margin on ``@page :first`` (the cover-
    enabled path resets it to 0 for full-bleed cover background).
    """
    style = _read_optional(template.style_path) or ""
    safe_pre = (
        "\n.content pre { white-space: pre-wrap; word-break: break-word; }\n"
    )
    no_chrome = (
        "\n@page {"
        "  @top-left { content: none; border: 0; padding: 0; }"
        "  @top-right { content: none; border: 0; padding: 0; }"
        "  @bottom-left { content: none; border: 0; padding: 0; }"
        "  @bottom-right { content: none; border: 0; padding: 0; }"
        "  @bottom-center { content: none; border: 0; padding: 0; }"
        "}"
        "@page :first {"
        "  margin: 22mm 20mm 22mm 20mm;"
        "  @top-left { content: none; }"
        "  @top-right { content: none; }"
        "  @bottom-left { content: none; }"
        "  @bottom-right { content: none; }"
        "  @bottom-center { content: none; }"
        "}\n"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{style}{safe_pre}{no_chrome}</style></head>"
        "<body><main class=\"content\">"
        f"{body_html}"
        "</main></body></html>"
    )


def _html_to_pdf(html: str, dst: Path, *, stop_event: Optional[threading.Event] = None) -> None:
    """Render the wrapped HTML document to a paginated PDF via WeasyPrint.

    Replaces the legacy wkhtmltopdf subprocess. ``base_url`` is the
    destination directory: WeasyPrint resolves any relative asset path
    in the wrapper (background images, font files) against it the same
    way wkhtmltopdf used to with ``--enable-local-file-access``.
    """
    from .pdf_render import Cancelled as PdfCancelled, render_html_to_pdf
    try:
        render_html_to_pdf(html, dst, base_url=dst.parent, stop_event=stop_event)
    except PdfCancelled:
        raise Cancelled()
    except Exception as e:
        raise RuntimeError(f"weasyprint: {str(e)[:300]}")


def render_pdf_with_template(
    *,
    md: str,
    template: TemplateMeta,
    ctx: TemplateContext,
    dst: Path,
    stop_event: Optional[threading.Event] = None,
    with_cover: bool = True,
) -> Path:
    """Render ``md`` to a templated PDF at ``dst`` and return ``dst``.

    The full pipeline:

      1. Pandoc converts the markdown to an HTML body fragment.
      2. The HTML body is inlined into the template wrapper alongside the CSS.
      3. WeasyPrint turns the standalone HTML document into a PDF.

    ``with_cover`` controls whether the template's cover page +
    metadata fields are emitted. The Export dialog passes ``True``
    (default) because the operator fills the metadata in. The PDF
    re-derive adapter passes ``False`` because it has no meaningful
    metadata to put on a cover and a fake one confuses operators.
    """
    body = _markdown_to_html_fragment(md, stop_event=stop_event)
    if with_cover:
        full_html = _wrap_html(template, body_html=body, ctx=ctx)
    else:
        full_html = _wrap_html_no_cover(template, body_html=body)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _html_to_pdf(full_html, dst, stop_event=stop_event)
    return dst


def render_html_with_template(
    *,
    md: str,
    template: TemplateMeta,
    ctx: TemplateContext,
    dst: Path,
    stop_event: Optional[threading.Event] = None,
) -> Path:
    """Same as :func:`render_pdf_with_template` but stops at standalone HTML."""
    body = _markdown_to_html_fragment(md, stop_event=stop_event)
    full_html = _wrap_html(template, body_html=body, ctx=ctx)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(full_html, encoding="utf-8")
    return dst


def export_files_to_pdf(
    files: Iterable[Path],
    *,
    template: TemplateMeta,
    ctx: TemplateContext,
    out_dir: Path,
    stop_event: Optional[threading.Event] = None,
) -> list[Path]:
    """Render each input file (txt/md/html) into ``out_dir/<name>.pdf``.

    For non-markdown inputs we wrap the content in a fenced ```` ```text ```` block
    so pandoc still produces sensible PDF output.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for f in files:
        if stop_event is not None and stop_event.is_set():
            raise Cancelled()
        f = Path(f)
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix.lower() not in {".md", ".markdown"}:
            text = "```text\n" + text + "\n```\n"
        ctx_local = TemplateContext(**{**ctx.to_dict(), "title": ctx.title or f.stem})
        dst = out_dir / (f.stem + ".pdf")
        render_pdf_with_template(
            md=text, template=template, ctx=ctx_local, dst=dst, stop_event=stop_event
        )
        results.append(dst)
    return results


__all__ = [
    "TemplateMeta",
    "TemplateContext",
    "USER_TEMPLATES_DIR",
    "BUILTIN_TEMPLATES_DIR",
    "list_templates",
    "get_template",
    "add_user_template",
    "delete_user_template",
    "render_pdf_with_template",
    "render_html_with_template",
    "export_files_to_pdf",
]
