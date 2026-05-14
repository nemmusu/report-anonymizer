"""Inline SVG icons (Lucide-style) plus loaders for visual assets.

Why inline? So the GUI is fully self-contained and Pyinstaller/onefile builds
don't need to ship dozens of .svg files separately.

Each icon function returns a :class:`QIcon` rendered at the requested size
and tinted to the requested color (defaults to the theme's text color).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import PALETTE


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


# Lucide-style 24x24 line icons (stroke="currentColor", stroke-width="2",
# stroke-linecap="round", stroke-linejoin="round").
_ICONS: dict[str, str] = {
    "play": '<polygon points="6 4 20 12 6 20 6 4"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "pause": '<line x1="10" y1="4" x2="10" y2="20"/><line x1="14" y1="4" x2="14" y2="20"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-6.7-3"/><path d="M21 22v-7h-7"/><path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 6.7 3"/><path d="M3 2v7h7"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "server": '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="2" x2="9" y2="4"/><line x1="15" y1="2" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="22"/><line x1="15" y1="20" x2="15" y2="22"/><line x1="20" y1="9" x2="22" y2="9"/><line x1="20" y1="14" x2="22" y2="14"/><line x1="2" y1="9" x2="4" y2="9"/><line x1="2" y1="14" x2="4" y2="14"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "info": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/>',
    "menu": '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
    "diff": '<path d="M12 3v18"/><path d="M5 8l7-5 7 5"/><path d="M5 16l7 5 7-5"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/><polyline points="12 7 12 12 15 14"/>',
    "key": '<circle cx="8" cy="15" r="4"/><line x1="10.85" y1="12.15" x2="22" y2="1"/><line x1="18" y1="5" x2="22" y2="9"/><line x1="15" y1="8" x2="19" y2="12"/>',
    "wand": '<path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8.5 8.5l-1.4-1.4"/><path d="M22 22l-3.5-3.5"/><path d="M19 7l-1.4-1.4"/><path d="M9 9l11 11"/><path d="M2 16l3 3"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "zoom-in": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/>',
    "zoom-out": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    # Image-redaction tools (Flameshot-style toolbar). Solid filled
    # square for "blackout", concentric soft circles for "blur",
    # 3x3 cell grid for "pixelate", uppercase A in a box for the
    # "text_overlay" tool. Filled shapes use fill="currentColor"
    # plus stroke so they tint with the same colour rule the rest
    # of the icon set already follows.
    "blackout": '<rect x="4" y="4" width="16" height="16" rx="1" fill="currentColor"/>',
    "blur": '<circle cx="12" cy="12" r="9" fill="none" opacity="0.4"/><circle cx="12" cy="12" r="6" fill="none" opacity="0.7"/><circle cx="12" cy="12" r="3" fill="currentColor"/>',
    "pixelate": '<rect x="4" y="4" width="5" height="5" fill="currentColor"/><rect x="10" y="4" width="5" height="5" fill="none"/><rect x="16" y="4" width="4" height="5" fill="currentColor"/><rect x="4" y="10" width="5" height="5" fill="none"/><rect x="10" y="10" width="5" height="5" fill="currentColor"/><rect x="16" y="10" width="4" height="5" fill="none"/><rect x="4" y="16" width="5" height="4" fill="currentColor"/><rect x="10" y="16" width="5" height="4" fill="none"/><rect x="16" y="16" width="4" height="4" fill="currentColor"/>',
    "text-overlay": '<rect x="3" y="6" width="18" height="12" rx="1" fill="none"/><polyline points="8 16 12 8 16 16"/><line x1="9.5" y1="13" x2="14.5" y2="13"/>',
    # Selection tool (mouse pointer). Used to switch the editor out
    # of any drawing mode so the operator can move / resize an
    # existing rect.
    "cursor": '<polygon points="3 3 12 21 14 13 22 11 3 3"/>',
    # Undo / redo (curved arrow pair). Distinct from "refresh" so
    # they read as "step back" / "step forward".
    "undo": '<path d="M3 7h11a6 6 0 1 1 0 12H7"/><polyline points="7 3 3 7 7 11"/>',
    "redo": '<path d="M21 7H10a6 6 0 1 0 0 12h7"/><polyline points="17 3 21 7 17 11"/>',
}


def _wrap_svg(path_d: str, color: str) -> bytes:
    # ``color="..."`` makes ``currentColor`` resolve, so any icon
    # element that opts into a fill can write ``fill="currentColor"``
    # and pick up the active theme tint without a per-element string
    # template. Stroke-only icons (the bulk) ignore it and keep the
    # default ``fill="none"``.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'color="{color}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{path_d}</svg>'
    ).encode("utf-8")


def render_svg_pixmap(name_or_svg: str, *, size: int = 16, color: Optional[str] = None) -> QPixmap:
    if name_or_svg in _ICONS:
        svg = _wrap_svg(_ICONS[name_or_svg], color or PALETTE["text"])
    else:
        svg = name_or_svg.encode("utf-8") if isinstance(name_or_svg, str) else name_or_svg
    renderer = QSvgRenderer(QByteArray(svg))
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    renderer.render(p)
    p.end()
    return QPixmap.fromImage(img)


def render_svg_rect_pixmap(svg: str, *, width: int, height: int) -> QPixmap:
    """Render an SVG into a *rectangular* pixmap, sized exactly to
    ``width`` × ``height``.  The plain :func:`render_svg_pixmap`
    forces a square buffer so a 16:9 hero SVG ends up centred in
    the top half of a square image with empty space below.
    Wide-aspect compositions (welcome hero, banners) should use
    this helper instead.

    The SVG is rendered through ``QSvgRenderer.render`` which
    preserves the SVG's own ``viewBox`` aspect ratio when its
    aspect mismatches the destination, Qt fits the longest side
    inside the rectangle and centres the rest.  Pass ``width`` /
    ``height`` matching the SVG aspect for a tight fit.
    """
    payload = svg.encode("utf-8") if isinstance(svg, str) else svg
    renderer = QSvgRenderer(QByteArray(payload))
    img = QImage(int(width), int(height), QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    renderer.render(p)
    p.end()
    return QPixmap.fromImage(img)


def icon(name: str, *, size: int = 16, color: Optional[str] = None) -> QIcon:
    """Return a :class:`QIcon` rendered from one of the bundled icons."""
    pix = render_svg_pixmap(name, size=size, color=color)
    qi = QIcon(pix)
    qi.addPixmap(render_svg_pixmap(name, size=size * 2, color=color))
    return qi


def icon_disabled(name: str, *, size: int = 16) -> QIcon:
    return icon(name, size=size, color=PALETTE["text_dim"])


def icon_accent(name: str, *, size: int = 16) -> QIcon:
    return icon(name, size=size, color=PALETTE["accent"])


def icon_ok(name: str, *, size: int = 16) -> QIcon:
    return icon(name, size=size, color=PALETTE["ok"])


def icon_err(name: str, *, size: int = 16) -> QIcon:
    return icon(name, size=size, color=PALETTE["err"])


def icon_warn(name: str, *, size: int = 16) -> QIcon:
    return icon(name, size=size, color=PALETTE["warn"])


# ---- App icon / splash / hero ---------------------------------------------


def _app_icon_svg(size: int = 256) -> bytes:
    p = PALETTE
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="{size}" height="{size}">'
        f'<defs>'
        f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{p["accent"]}"/>'
        f'<stop offset="1" stop-color="{p["accent_dim"]}"/>'
        f'</linearGradient></defs>'
        f'<rect x="8" y="8" width="240" height="240" rx="48" fill="{p["surface"]}" stroke="url(#g)" stroke-width="6"/>'
        f'<g transform="translate(128 128)">'
        f'<path d="M-56 -36 L-16 -36 L16 0 L-16 36 L-56 36 Z" fill="url(#g)"/>'
        f'<path d="M16 -36 L56 -36 L56 36 L16 36 L-16 0 Z" fill="{p["accent_glow"]}" opacity="0.85"/>'
        f'<text x="0" y="6" text-anchor="middle" font-family="Inter,sans-serif" font-size="28" '
        f'font-weight="700" fill="{p["text_strong"]}">A*</text>'
        f'</g>'
        f'</svg>'
    ).encode("utf-8")


def app_icon(size: int = 256) -> QIcon:
    asset = ASSETS_DIR / "app_icon.svg"
    if asset.exists():
        try:
            return QIcon(QPixmap(str(asset)))
        except Exception:
            pass
    pix = render_svg_pixmap(_app_icon_svg(size).decode("utf-8"), size=size)
    qi = QIcon(pix)
    for s in (16, 32, 48, 64, 128, 256):
        qi.addPixmap(render_svg_pixmap(_app_icon_svg(s).decode("utf-8"), size=s))
    return qi


def splash_pixmap(width: int = 480, height: int = 280) -> QPixmap:
    asset = ASSETS_DIR / "splash.png"
    if asset.exists():
        return QPixmap(str(asset))
    p = PALETTE
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 280" width="{width}" height="{height}">'
        f'<rect width="480" height="280" fill="{p["bg"]}"/>'
        f'<rect x="0" y="0" width="480" height="280" fill="url(#g)" opacity="0.15"/>'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{p["accent"]}"/><stop offset="1" stop-color="{p["accent_dim"]}"/>'
        f'</linearGradient></defs>'
        f'<g transform="translate(40 90)">'
        f'<rect x="0" y="0" width="64" height="64" rx="14" fill="{p["accent"]}"/>'
        f'<text x="32" y="42" text-anchor="middle" font-family="Inter,sans-serif" font-size="28" '
        f'font-weight="800" fill="{p["text_strong"]}">A*</text>'
        f'</g>'
        f'<text x="120" y="120" font-family="Inter,sans-serif" font-size="22" font-weight="700" fill="{p["text_strong"]}">document anonymizer</text>'
        f'<text x="120" y="146" font-family="Inter,sans-serif" font-size="13" fill="{p["text_dim"]}">production · local LLM · privacy first</text>'
        f'<text x="40" y="248" font-family="Inter,sans-serif" font-size="11" fill="{p["text_dim"]}">loading…</text>'
        f'</svg>'
    )
    # Use the rect-aware renderer so the 480x280 viewBox isn't
    # squashed into a 480x480 square buffer (which is what
    # ``render_svg_pixmap`` does, its ``size`` arg is square-only).
    return render_svg_rect_pixmap(svg, width=width, height=height)


def welcome_hero_pixmap(width: int = 1024, height: int = 480) -> QPixmap:
    """Welcome view hero illustration.

    The SVG viewBox is ``1024 × 480`` (≈ 21:10), wide enough to read
    as a banner, tight enough to feel proportionate when embedded
    inside the welcome view's column layout.  We render through
    :func:`render_svg_rect_pixmap` so the destination QPixmap matches
    the actual viewBox aspect; the previous square-buffer renderer
    centred this 16:9-ish composition in the top half and left an
    empty band below, which read as 'too narrow / not natural'.
    """
    asset = ASSETS_DIR / "hero.png"
    if asset.exists():
        return QPixmap(str(asset))
    p = PALETTE
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 480">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{p["surface"]}"/><stop offset="1" stop-color="{p["bg"]}"/>'
        f'</linearGradient></defs>'
        f'<rect width="1024" height="480" fill="url(#bg)"/>'
        f'<text x="512" y="80" text-anchor="middle" font-family="Inter,sans-serif" font-size="32" '
        f'font-weight="700" fill="{p["text_strong"]}">Anonymize. Preserve structure.</text>'
        f'<text x="512" y="112" text-anchor="middle" font-family="Inter,sans-serif" font-size="15" '
        f'fill="{p["text_dim"]}">Drop a file or a folder to begin · everything stays local</text>'
        # "Before" document mock-up
        f'<g transform="translate(140 170)" opacity="0.9">'
        f'<rect x="0" y="0" width="320" height="220" rx="14" fill="{p["surface_hi"]}" stroke="{p["border_strong"]}"/>'
        f'<rect x="24" y="28" width="272" height="14" rx="4" fill="{p["text_dim"]}"/>'
        f'<rect x="24" y="58" width="220" height="10" rx="3" fill="{p["text_dim"]}" opacity="0.6"/>'
        f'<rect x="24" y="78" width="252" height="10" rx="3" fill="{p["text_dim"]}" opacity="0.6"/>'
        f'<rect x="24" y="98" width="200" height="10" rx="3" fill="{p["text_dim"]}" opacity="0.6"/>'
        f'<rect x="24" y="138" width="272" height="48" rx="6" fill="{p["err"]}" opacity="0.45"/>'
        f'</g>'
        # "After" document mock-up
        f'<g transform="translate(564 170)">'
        f'<rect x="0" y="0" width="320" height="220" rx="14" fill="{p["surface_hi"]}" stroke="{p["accent"]}" stroke-width="2"/>'
        f'<rect x="24" y="28" width="272" height="14" rx="4" fill="{p["text_strong"]}"/>'
        f'<rect x="24" y="58" width="220" height="10" rx="3" fill="{p["text"]}" opacity="0.7"/>'
        f'<rect x="24" y="78" width="252" height="10" rx="3" fill="{p["text"]}" opacity="0.7"/>'
        f'<rect x="24" y="98" width="200" height="10" rx="3" fill="{p["text"]}" opacity="0.7"/>'
        f'<rect x="24" y="138" width="272" height="48" rx="6" fill="{p["ok"]}" opacity="0.55"/>'
        f'</g>'
        # Arrow between the two mock-ups
        f'<g transform="translate(472 270)">'
        f'<path d="M0 10 L80 10" stroke="{p["accent"]}" stroke-width="3" stroke-linecap="round"/>'
        f'<polygon points="80 0,100 10,80 20" fill="{p["accent"]}"/>'
        f'</g>'
        f'</svg>'
    )
    return render_svg_rect_pixmap(svg, width=width, height=height)


def empty_state_pixmap(kind: str, *, width: int = 280, height: int = 180) -> QPixmap:
    asset = ASSETS_DIR / f"empty_{kind}.png"
    if asset.exists():
        return QPixmap(str(asset))
    p = PALETTE
    icons = {
        "review": "list",
        "diff": "diff",
        "verifier": "shield",
        "downloads": "download",
        "scan": "search",
    }
    iname = icons.get(kind, "info")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 280 180" width="{width}" height="{height}">'
        f'<rect width="280" height="180" fill="transparent"/>'
        f'<g transform="translate(110 50)" stroke="{p["text_dim"]}" stroke-width="2" fill="none" '
        f'stroke-linecap="round" stroke-linejoin="round" opacity="0.55">'
        f'<svg viewBox="0 0 24 24" width="60" height="60">{_ICONS[iname]}</svg>'
        f'</g>'
        f'</svg>'
    )
    return render_svg_pixmap(svg, size=width)


__all__ = [
    "icon",
    "icon_disabled",
    "icon_accent",
    "icon_ok",
    "icon_err",
    "icon_warn",
    "render_svg_pixmap",
    "app_icon",
    "splash_pixmap",
    "welcome_hero_pixmap",
    "render_svg_rect_pixmap",
    "empty_state_pixmap",
    "ASSETS_DIR",
]
