"""Format adapter registry.

Each adapter implements :class:`FormatAdapter` and exposes a stable set of
extensions / MIME types. The registry resolves a path to the right adapter via
extension first, then content-sniff (python-magic) as fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .base import FormatAdapter, NullAdapter, Segment, WriteReport
from .text_adapter import TextAdapter

try:  # heavy deps imported lazily so the engine can boot without them
    from .docx_adapter import DocxAdapter
except Exception:  # pragma: no cover - optional dep
    DocxAdapter = None  # type: ignore[assignment]
try:
    from .doc_legacy_adapter import DocLegacyAdapter
except Exception:  # pragma: no cover
    DocLegacyAdapter = None  # type: ignore[assignment]
try:
    from .xlsx_adapter import XlsxAdapter
except Exception:  # pragma: no cover
    XlsxAdapter = None  # type: ignore[assignment]
try:
    from .pptx_adapter import PptxAdapter
except Exception:  # pragma: no cover
    PptxAdapter = None  # type: ignore[assignment]
try:
    from .odt_adapter import OdtAdapter
except Exception:  # pragma: no cover
    OdtAdapter = None  # type: ignore[assignment]
try:
    from .rtf_adapter import RtfAdapter
except Exception:  # pragma: no cover
    RtfAdapter = None  # type: ignore[assignment]
try:
    from .pdf_inplace_adapter import PdfInplaceAdapter
except Exception:  # pragma: no cover
    PdfInplaceAdapter = None  # type: ignore[assignment]
try:
    from .pdf_rederive_adapter import PdfRederiveAdapter
except Exception:  # pragma: no cover
    PdfRederiveAdapter = None  # type: ignore[assignment]


_DEFAULT_PDF_STRATEGY = "inplace"  # may be overridden via set_pdf_strategy()
_DEFAULT_EXPORT_TEMPLATE_ID: Optional[str] = None


def set_pdf_strategy(strategy: str) -> None:
    """Set the default PDF strategy (``inplace`` or ``rederive``)."""
    global _DEFAULT_PDF_STRATEGY
    if strategy not in {"inplace", "rederive"}:
        raise ValueError(f"unknown pdf strategy: {strategy}")
    _DEFAULT_PDF_STRATEGY = strategy


def set_export_template(template_id: Optional[str]) -> None:
    """Default template id used by the rederive adapter when re-rendering.

    ``None`` disables templating (legacy ``DEFAULT_CSS`` look). Mirrors
    :func:`set_pdf_strategy`, the pipeline writes both at the start of
    each run from the project's user-facing choices.
    """
    global _DEFAULT_EXPORT_TEMPLATE_ID
    _DEFAULT_EXPORT_TEMPLATE_ID = template_id or None


def get_export_template() -> Optional[str]:
    return _DEFAULT_EXPORT_TEMPLATE_ID


def _ext(path: Path) -> str:
    return path.suffix.lower()


def get_adapter(path: Path, *, pdf_strategy: Optional[str] = None) -> FormatAdapter:
    """Resolve the right adapter for ``path``.

    ``pdf_strategy`` overrides the default for PDFs (``"inplace"`` or
    ``"rederive"``).
    """
    ext = _ext(path)
    pdf_strategy = pdf_strategy or _DEFAULT_PDF_STRATEGY

    if ext == ".docx" and DocxAdapter is not None:
        return DocxAdapter()
    if ext == ".doc" and DocLegacyAdapter is not None:
        return DocLegacyAdapter()
    if ext == ".xlsx" and XlsxAdapter is not None:
        return XlsxAdapter()
    if ext == ".pptx" and PptxAdapter is not None:
        return PptxAdapter()
    if ext == ".odt" and OdtAdapter is not None:
        return OdtAdapter()
    if ext == ".rtf" and RtfAdapter is not None:
        return RtfAdapter()
    if ext == ".pdf":
        if pdf_strategy == "rederive" and PdfRederiveAdapter is not None:
            return PdfRederiveAdapter()
        if PdfInplaceAdapter is not None:
            return PdfInplaceAdapter()
    if ext in TextAdapter.EXTENSIONS:
        return TextAdapter()

    return _content_sniff(path) or NullAdapter()


def _content_sniff(path: Path) -> Optional[FormatAdapter]:
    # ``python-magic`` (libmagic ctypes wrapper) is unstable on some
    # Windows Python builds: ``import magic`` has been observed to
    # raise ``Windows fatal exception: access violation`` in
    # ``magic.compat`` on the python-3.10/3.11 wheels we use on the
    # GitHub Actions ``windows-latest`` runner, which crashes the
    # whole interpreter (not catchable from Python). Skip the
    # magic-based sniff entirely on Windows and fall through to the
    # null-byte heuristic.
    magic = None
    if os.name != "nt":
        try:
            import magic as _magic_module  # noqa: F401
            magic = _magic_module
        except Exception:
            magic = None
    if magic is None:
        try:
            head = path.read_bytes()[:8192]
        except Exception:
            return None
        if b"\x00" in head:
            return None
        return TextAdapter()
    try:
        mime = magic.from_file(str(path), mime=True)
    except Exception:
        return None
    if mime.startswith("text/") or mime in TextAdapter.MIMES:
        return TextAdapter()
    return None


__all__ = [
    "FormatAdapter",
    "NullAdapter",
    "Segment",
    "WriteReport",
    "TextAdapter",
    "DocxAdapter",
    "DocLegacyAdapter",
    "XlsxAdapter",
    "PptxAdapter",
    "OdtAdapter",
    "RtfAdapter",
    "PdfInplaceAdapter",
    "PdfRederiveAdapter",
    "get_adapter",
    "set_pdf_strategy",
    "set_export_template",
    "get_export_template",
]
