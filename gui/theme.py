"""Dark QSS theme for the document-anonymizer-production GUI.

Polished, JetBrains-inspired palette with single accent (electric blue).
Add a new color by appending to ``PALETTE`` then referring to it in :func:`qss`.
"""
from __future__ import annotations

PALETTE: dict[str, str] = {
    # canvas
    "bg": "#16181d",
    "bg_alt": "#1a1d23",
    "surface": "#22262e",
    "surface_alt": "#1d2026",
    "surface_hi": "#2a2f37",
    "border": "#2f343d",
    "border_strong": "#454c57",
    # text
    "text": "#e6e8eb",
    "text_dim": "#9aa0a6",
    "text_strong": "#f5f6f7",
    "text_link": "#7eb8ff",
    # accents
    "accent": "#4f8cc9",
    "accent_dim": "#3b6c9e",
    "accent_glow": "#5da4ec",
    # semantic
    "ok": "#3fb950",
    "warn": "#f5a623",
    "err": "#f85149",
    "info": "#58a6ff",
    # form fields
    "bg_input": "#15181d",
    # sidebar
    "sidebar_bg": "#13151a",
    "sidebar_active": "#22262e",
    "sidebar_indicator": "#4f8cc9",
    # toast
    "toast_bg": "#262a32",
    "toast_border": "#3a3f48",
}


SPACING = {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24,
    "xxl": 32,
}


def qss() -> str:
    p = PALETTE
    return f"""
    QWidget {{
        background: {p['bg']};
        color: {p['text']};
        font-family: "Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {p['bg']}; }}
    /* The blanket ``QWidget {{ background: bg }}`` rule above
       cascades into every QLabel, so label text lands on a dark
       block painted *on top of* its parent surface, visible as
       a "black banner" under headings, descriptions and the
       welcome drop-zone copy.  Force every label transparent by
       default so it inherits its parent's actual surface;
       label-as-pill widgets (Badge*, PipelineStatus, …) reset
       their own background where they need one. */
    QLabel {{ background: transparent; }}

    QToolTip {{
        background: {p['surface']};
        color: {p['text']};
        border: 1px solid {p['border']};
        padding: 4px 6px;
    }}

    QMenuBar {{
        background: {p['bg_alt']};
        border-bottom: 1px solid {p['border']};
        padding: 2px 6px;
    }}
    QMenuBar::item {{
        background: transparent; padding: 4px 10px; border-radius: 4px;
    }}
    QMenuBar::item:selected {{ background: {p['surface']}; }}
    QMenu {{
        background: {p['surface']};
        border: 1px solid {p['border']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p['accent_dim']}; color: {p['text_strong']}; }}

    QStatusBar {{
        background: {p['bg_alt']};
        border-top: 1px solid {p['border']};
        padding: 2px 8px;
    }}
    QStatusBar::item {{ border: none; }}

    QDockWidget::title {{
        background: {p['surface_alt']};
        padding: 6px 8px;
        border: 1px solid {p['border']};
        border-bottom: none;
    }}
    QDockWidget {{ titlebar-close-icon: none; titlebar-normal-icon: none; }}

    /* ---- sidebar ---- */
    QFrame#Sidebar {{
        background: {p['sidebar_bg']};
        border-right: 1px solid {p['border']};
    }}
    QToolButton#SidebarButton {{
        background: transparent; border: none; color: {p['text_dim']};
        padding: 10px 12px; text-align: left; min-height: 36px; border-radius: 6px;
    }}
    QToolButton#SidebarButton:hover {{
        background: {p['surface']}; color: {p['text']};
    }}
    QToolButton#SidebarButton:checked {{
        background: {p['sidebar_active']}; color: {p['text_strong']};
        border-left: 3px solid {p['sidebar_indicator']};
    }}
    QLabel#SidebarHeader {{ color: {p['text_dim']}; padding: 12px; font-size: 11px; letter-spacing: 1px; }}

    QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 4px; top: -1px; background: {p['surface']}; }}
    QTabBar::tab {{
        background: {p['surface_alt']}; color: {p['text_dim']};
        padding: 6px 14px; border: 1px solid {p['border']}; border-bottom: none;
        margin-right: 2px;
        border-top-left-radius: 4px; border-top-right-radius: 4px;
    }}
    QTabBar::tab:selected {{ background: {p['surface']}; color: {p['text_strong']}; }}
    QTabBar::tab:hover {{ color: {p['text']}; }}

    QPushButton {{
        background: {p['surface']};
        border: 1px solid {p['border_strong']};
        padding: 7px 14px;
        border-radius: 5px;
        color: {p['text']};
        min-height: 18px;
    }}
    QPushButton:hover {{ background: {p['surface_hi']}; border-color: {p['accent_dim']}; }}
    QPushButton:pressed {{ background: {p['accent_dim']}; }}
    QPushButton:checked {{
        background: {p['accent']};
        border-color: {p['accent_glow']};
        color: {p['text_strong']};
        font-weight: 600;
    }}
    QPushButton:checked:hover {{
        background: {p['accent_glow']};
        border-color: {p['accent_glow']};
    }}
    QPushButton:disabled {{ color: {p['text_dim']}; border-color: {p['border']}; }}
    QPushButton#PrimaryButton {{
        background: {p['accent']}; border-color: {p['accent']}; color: {p['text_strong']};
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover {{ background: {p['accent_glow']}; }}
    QPushButton#PrimaryButton:checked {{
        background: {p['accent_glow']}; border-color: {p['text_strong']};
    }}
    QPushButton#DangerButton {{
        background: {p['err']}; border-color: {p['err']}; color: {p['text_strong']}; font-weight: 600;
    }}
    QPushButton#DangerButton:hover {{ background: #ff6b66; }}
    QPushButton#DangerButton:checked {{
        background: #ff6b66; border-color: {p['text_strong']};
    }}
    QPushButton#GhostButton {{
        background: transparent; border: 1px solid {p['border_strong']}; color: {p['text']};
    }}
    QPushButton#LinkButton {{
        background: transparent; border: none; color: {p['text_link']};
        text-decoration: underline; padding: 0;
    }}

    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {p['bg_input']};
        border: 1px solid {p['border']};
        border-radius: 5px;
        padding: 5px 7px;
        selection-background-color: {p['accent_dim']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {p['accent']}; }}

    QComboBox::drop-down {{ border: none; padding-right: 6px; }}
    QComboBox QAbstractItemView {{
        background: {p['surface']}; border: 1px solid {p['border']};
        selection-background-color: {p['accent_dim']};
    }}

    QHeaderView::section {{
        background: {p['surface_alt']};
        color: {p['text']};
        padding: 5px 9px;
        border: none;
        border-right: 1px solid {p['border']};
        border-bottom: 1px solid {p['border']};
    }}
    QTreeView, QTableView, QListView {{
        background: {p['surface']};
        alternate-background-color: {p['surface_alt']};
        selection-background-color: {p['accent_dim']};
        gridline-color: {p['border']};
    }}
    QTreeView::item:hover, QTableView::item:hover {{ background: {p['surface_hi']}; }}

    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 3px;
        border: 1px solid {p['border_strong']};
        background: {p['bg_input']};
    }}
    QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}
    QCheckBox::indicator:indeterminate {{ background: {p['accent_dim']}; border-color: {p['accent_dim']}; }}

    QScrollBar:vertical {{
        background: {p['bg']}; width: 12px; margin: 0; border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {p['border_strong']}; border-radius: 4px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p['accent_dim']}; }}
    QScrollBar:horizontal {{ background: {p['bg']}; height: 12px; }}
    QScrollBar::handle:horizontal {{
        background: {p['border_strong']}; border-radius: 4px; min-width: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

    QProgressBar {{
        background: {p['surface_alt']}; border: 1px solid {p['border']};
        border-radius: 5px; text-align: center; height: 18px;
        color: {p['text_dim']};
    }}
    QProgressBar::chunk {{ background: {p['accent']}; border-radius: 4px; }}
    /* Inside cards, blend the trough with the card surface so the bar
       doesn't look like a black hole sitting on a slightly darker page. */
    QFrame#Card QProgressBar {{
        background: {p['bg_alt']}; border: 1px solid {p['border']};
    }}

    /* ---- Pipeline progress card ---- */
    QFrame#PipelineProgressCard {{
        background: {p['surface']};
        border: 1px solid {p['border']};
        border-radius: 10px;
    }}
    QFrame#PipelineDivider {{
        background: {p['border']};
        border: none;
    }}
    QLabel#PipelineStatus {{
        color: {p['text']};
        font-size: 13px;
        padding-top: 2px;
    }}
    QProgressBar#PipelineProgress {{
        background: {p['surface_alt']};
        border: 1px solid {p['border']};
        border-radius: 6px;
        text-align: center;
        color: {p['text_dim']};
        font-size: 11px;
        font-weight: 600;
        min-height: 22px;
    }}
    QProgressBar#PipelineProgress::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {p['accent_dim']}, stop:1 {p['accent_glow']});
        border-radius: 5px;
    }}
    /* The stepper itself is purely visual, kill any inherited frame
       look so chips appear flat on the card surface. */
    QWidget#StageStrip {{ background: transparent; }}
    QFrame#StageChip QLabel {{ background: transparent; }}

    QGroupBox {{
        border: 1px solid {p['border']}; border-radius: 6px;
        margin-top: 14px; padding: 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; subcontrol-position: top left;
        padding: 0 8px; color: {p['text_dim']};
    }}

    /* ---- semantic blocks ---- */
    QFrame#Card {{
        background: {p['surface']};
        border: 1px solid {p['border']};
        border-radius: 8px;
    }}
    QFrame#PresetCard {{
        background: {p['surface']};
        border: 1px solid {p['border']};
        border-radius: 8px;
    }}
    QFrame#PresetCard[selected="true"] {{ border-color: {p['accent']}; }}
    /* Title strip on each preset card.  We deliberately keep it
       on the same surface as the body, adding a slightly lighter
       tint (``surface_hi``) reads as a *darker* block under the
       cascading ``QWidget {{ background }}`` rule on some
       displays where 8 brightness points are below the
       discriminable threshold.  A 1-px hair-thin divider on the
       bottom is enough hierarchy without painting bands. */
    QFrame#PresetCardHead {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {p['border']};
    }}
    QFrame#Toast {{
        background: {p['toast_bg']};
        border: 1px solid {p['toast_border']};
        border-radius: 8px;
    }}
    QFrame#ToastInfo  {{ border-left: 4px solid {p['info']}; }}
    QFrame#ToastOk    {{ border-left: 4px solid {p['ok']}; }}
    QFrame#ToastWarn  {{ border-left: 4px solid {p['warn']}; }}
    QFrame#ToastErr   {{ border-left: 4px solid {p['err']}; }}

    /* ---- typographic ---- */
    QLabel#H1 {{ font-size: 22px; font-weight: 600; color: {p['text_strong']}; }}
    QLabel#H2 {{ font-size: 15px; font-weight: 600; color: {p['text_strong']}; }}
    QLabel#H3 {{ font-size: 13px; font-weight: 600; color: {p['text_strong']}; }}
    QLabel#Caption {{ color: {p['text_dim']}; font-size: 11px; letter-spacing: 0.5px; }}
    QLabel#Muted {{ color: {p['text_dim']}; }}

    /* ---- badges ---- */
    QLabel#Badge {{
        background: {p['accent_dim']}; color: {p['text_strong']};
        padding: 2px 8px; border-radius: 9px; font-size: 11px;
    }}
    QLabel#BadgeOk    {{ background: {p['ok']}; color: {p['bg']}; padding: 2px 8px; border-radius: 9px; font-size: 11px; }}
    QLabel#BadgeWarn  {{ background: {p['warn']}; color: {p['bg']}; padding: 2px 8px; border-radius: 9px; font-size: 11px; }}
    QLabel#BadgeErr   {{ background: {p['err']}; color: {p['text_strong']}; padding: 2px 8px; border-radius: 9px; font-size: 11px; }}
    QLabel#BadgeMuted {{ background: {p['surface_hi']}; color: {p['text_dim']}; padding: 2px 8px; border-radius: 9px; font-size: 11px; border: 1px solid {p['border_strong']}; }}
    """


__all__ = ["PALETTE", "SPACING", "qss"]
