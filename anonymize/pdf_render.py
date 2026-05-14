"""HTML -> PDF rendering helper (WeasyPrint).

Replaces the legacy ``wkhtmltopdf`` subprocess invocation with a pure
Python pipeline on top of WeasyPrint. WeasyPrint is fed the standalone
HTML5 produced by pandoc and emits a paginated PDF using Cairo + Pango
(system libs that ship on any modern desktop Linux).

Why the swap matters:

* No more system binary dependency at runtime.
* No QtWebKit-frozen-2014 CSS bugs.
* Bundling for AppImage becomes feasible (WeasyPrint has no Qt5
  conflict with our PySide6 GUI).

Cooperative cancellation: WeasyPrint runs in-process and is synchronous,
so we cannot kill mid-render the way the wkhtmltopdf ``Popen`` allowed.
Mitigation: WeasyPrint typically renders a 10-page report in under
2 s, the worker loops still check ``stop_event`` between files, so a
Stop click takes at most one render's worth of latency to be honoured
(same de facto behaviour as the previous ``_run_killable`` wrapper,
which also had to wait for the final write to flush).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Union


# WeasyPrint logs every CSS warning by default; in production those
# warnings are noise (e.g. "unknown property -webkit-...") since pandoc
# emits broadly compatible HTML. Pin its loggers to ERROR so the apply
# stage stays quiet, devs can re-enable via WEASYPRINT_LOG=DEBUG.
import os

_WP_LOG_LEVEL = os.environ.get("WEASYPRINT_LOG", "ERROR").upper()
for _name in ("weasyprint", "weasyprint.progress", "fontTools",
              "fontTools.ttLib", "fontTools.subset"):
    logging.getLogger(_name).setLevel(_WP_LOG_LEVEL)


class Cancelled(Exception):
    """Raised when ``stop_event`` was set before / after a render."""


def render_html_to_pdf(
    html: str,
    dst: Path,
    *,
    base_url: Optional[Union[str, Path]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Render the standalone HTML document ``html`` to a paginated
    PDF at ``dst``.

    ``base_url`` controls how relative resources (images, fonts) are
    resolved. Pass the source HTML's directory when the wrapper
    references local assets; ``--embed-resources`` HTML emitted by
    pandoc is already self-contained so the default ``None`` is fine.
    """
    if stop_event is not None and stop_event.is_set():
        raise Cancelled()

    # Imported lazily so module import doesn't pay the WeasyPrint
    # init cost for code paths that don't render PDFs.
    from weasyprint import HTML

    dst.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(base_url) if base_url else None).write_pdf(
        target=str(dst)
    )

    if stop_event is not None and stop_event.is_set():
        # Render finished but Stop was pressed, still raise so the
        # caller treats the artefact as "should be discarded".
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
        raise Cancelled()


__all__ = ["render_html_to_pdf", "Cancelled"]
