"""Color palettes, font selection, and the global desktop-app stylesheet."""

from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QFont, QFontDatabase

from rejoiner_colors import (BG, CARD, CARD2, FIELD, FIELD_HOVER, BORDER, BORDER2,
                      ACCENT, ACCENT_MID, ACCENT_SOFT, ACCENT_DARK, TEXT, TEXT2,
                      MUTED, GOOD, WARN, BAD, SIDEBAR, SWITCH_OFF)

SEP           = "#232328"
SCROLL_THUMB  = "#3A3A42"
SWITCH_ON     = ACCENT

KILL_BG = "#2D1017"
KILL_BG_HOVER = "#3D1520"
ON_ACCENT = "#FFFFFF"


def _palette(background, surface, border, field, accent, hover, danger,
             text, muted):
    accent_color = QColor(accent)
    accent_mid = accent_color.lighter(115).name()
    accent_soft = accent_color.lighter(155).name()
    surface_color = QColor(surface)
    return {
        "BG": background,
        "CARD": surface,
        "CARD2": surface_color.lighter(112).name(),
        "FIELD": field,
        "FIELD_HOVER": QColor(field).lighter(120).name(),
        "BORDER": border,
        "BORDER2": QColor(border).lighter(125).name(),
        "ACCENT": accent,
        "ACCENT_MID": accent_mid,
        "ACCENT_SOFT": accent_soft,
        "ACCENT_DARK": hover,
        "TEXT": text,
        "TEXT2": QColor(text).darker(108).name(),
        "MUTED": muted,
        "GOOD": "#34C759",
        "WARN": "#FF9F0A",
        "BAD": danger,
        "SIDEBAR": QColor(background).darker(112).name(),
        "SWITCH_OFF": "#4A4A50",
        "SWITCH_ON": accent,
        "SEP": QColor(border).darker(110).name(),
        "SCROLL_THUMB": QColor(border).lighter(135).name(),
        "NAV_ACTIVE": accent,
        "NAV_HOVER": surface,
        "SUBTLE": background,
        "PILL_FILL": surface,
        "TRACK": background,
        "KILL_BG": "#2D1017",
        "KILL_BG_HOVER": "#3D1520",
        "ON_ACCENT": "#FFFFFF",
    }


THEMES = {
    "Blue": _palette("#0B1220", "#172338", "#293A53", "#101A2B",
                     "#3B82F6", "#2563EB", "#EF4444", "#F8FAFC", "#A7B3C7"),
    "Purple": _palette("#130D20", "#211632", "#3A2A50", "#171025",
                       "#A78BFA", "#8B5CF6", "#FB7185", "#FAF7FF", "#C4B5D8"),
    "Emerald": _palette("#071A15", "#10291F", "#214737", "#0B211A",
                        "#34D399", "#10B981", "#FB7185", "#F0FDF4", "#A7CBB7"),
    "Sunset": _palette("#1D100D", "#321B12", "#573322", "#28150F",
                       "#FB923C", "#F97316", "#FB7185", "#FFF7ED", "#E7BFA5"),
    "Ocean": _palette("#071720", "#102733", "#254453", "#0B1D27",
                      "#38BDF8", "#0EA5E9", "#FB7185", "#F0F9FF", "#A8C9D8"),
    "Rose": _palette("#1D1018", "#301A27", "#4B2B3D", "#25131E",
                     "#F472B6", "#EC4899", "#FB7185", "#FFF1F7", "#DDB2C5"),
    "Lavender": _palette("#151326", "#24213A", "#3C3858", "#1B1930",
                         "#C4B5FD", "#A78BFA", "#FB7185", "#F7F5FF", "#C0BDD8"),
    "Graphite": _palette("#111315", "#202326", "#373C40", "#191C1F",
                         "#A3E635", "#84CC16", "#FB7185", "#F4F4F5", "#B2B6B9"),
}


def set_theme(name: str, phase: float = 0.0) -> str:
    """Apply a named palette to the stylesheet and custom-painted widgets."""
    global BG, CARD, CARD2, FIELD, FIELD_HOVER, BORDER, BORDER2, ACCENT
    global ACCENT_MID, ACCENT_SOFT, ACCENT_DARK, TEXT, TEXT2, MUTED, GOOD
    global WARN, BAD, SIDEBAR, SWITCH_OFF, SWITCH_ON, SEP, SCROLL_THUMB

    if name not in THEMES:
        name = "Graphite"
    palette = THEMES[name]
    globals().update(palette)

    import rejoiner_colors
    rejoiner_colors.__dict__.update(palette)
    for module_name in ("rejoiner_widgets", "roblox_rejoiner"):
        module = sys.modules.get(module_name)
        if module is not None:
            module.__dict__.update(
                (key, value)
                for key, value in palette.items()
                if key in module.__dict__
            )
    return name


def pick_fonts() -> tuple[str, str]:
    """Return display and text families, preferring the native system UI font."""
    have = set(QFontDatabase.families())
    def first(cands, default):
        for c in cands:
            if c in have:
                return c
        return default
    display = first(["Segoe UI Variable Display", "Segoe UI", "Inter"], "Segoe UI")
    text = first(["Segoe UI Variable Text", "Segoe UI", "Inter"], "Segoe UI")
    return display, text


def lerp_qcolor(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red()   + (b.red()   - a.red())   * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue()  + (b.blue()  - a.blue())  * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def build_stylesheet(text_family: str) -> str:
    return f"""
    * {{
        font-family: "{text_family}";
        color: {TEXT};
        outline: none;
    }}
    QToolTip {{
        background: {CARD2}; color: {TEXT}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 6px 9px;
    }}

    /* Window, page, and divider surfaces. */
    QFrame#Root      {{ background: {BG}; border: none; }}
    QFrame#Sidebar   {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
    QFrame#TopBar    {{ background: {SIDEBAR}; }}
    QFrame#TopBarDivider {{ background: {BORDER}; border: none; }}
    QFrame#Sep       {{ background: {SEP}; max-height: 1px; border: none; }}
    QFrame#VSep      {{ background: {SEP}; max-width: 1px; border: none; }}

    /* Settings cards and inset panels. */
    QFrame#Card      {{ background: {CARD}; border-radius: 5px;
                        border: 1px solid {BORDER}; }}
    QFrame#Inset     {{ background: {CARD2}; border-radius: 5px;
                        border: 1px solid {BORDER}; }}

    /* Headings, labels, and status text. */
    QLabel#H1        {{ color: {TEXT}; font-size: 26px; font-weight: 700; }}
    QLabel#H2        {{ color: {TEXT}; font-size: 17px; font-weight: 600; }}
    QLabel#Section   {{ color: {MUTED}; font-size: 11px; font-weight: 700; }}
    QLabel#Muted     {{ color: {MUTED}; font-size: 13px; }}
    QLabel#RowTitle  {{ color: {TEXT}; font-size: 14px; font-weight: 500; }}
    QLabel#RowSub    {{ color: {MUTED}; font-size: 12px; }}
    QLabel#Stat      {{ color: {TEXT}; font-size: 22px; font-weight: 700; }}
    QLabel#StatLabel {{ color: {MUTED}; font-size: 11px; font-weight: 600; }}

    /* Text inputs and selection colors. */
    QLineEdit, QPlainTextEdit, QTextEdit {{
        background: {FIELD}; border: 1px solid {BORDER}; border-radius: 4px;
        padding: 8px 11px; color: {TEXT}; font-size: 13px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
    QLineEdit::placeholder {{ color: {MUTED}; }}

    /* Primary actions, secondary actions, and links. */
    QPushButton#Primary {{
        background: {ACCENT};
        color: {ON_ACCENT}; border: none; border-radius: 4px;
        padding: 11px 18px; font-size: 14px; font-weight: 600;
    }}
    QPushButton#Primary:hover    {{ background: {ACCENT_MID}; }}
    QPushButton#Primary:pressed  {{ background: {ACCENT_DARK}; }}
    QPushButton#Primary:disabled {{ background: {CARD2}; color: {MUTED}; }}

    QPushButton#Ghost {{
        background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};
        border-radius: 4px; padding: 10px 16px; font-size: 13px; font-weight: 500;
    }}
    QPushButton#Ghost:hover   {{ background: {FIELD_HOVER}; border-color: {BORDER2}; }}
    QPushButton#Ghost:pressed {{ background: {CARD2}; }}

    QFrame#HeaderRule {{ background: {SEP}; border: none; }}
    QFrame#SideRule   {{ background: {SEP}; border: none; }}

    QPushButton#Help {{
        background: {FIELD}; color: {MUTED}; border: 1px solid {BORDER};
        border-radius: 4px; font-size: 12px; font-weight: 700; padding: 0;
    }}
    QPushButton#Help:hover {{
        color: {TEXT}; border-color: {BORDER2}; background: {FIELD_HOVER};
    }}

    QPushButton#Danger {{
        background: {BAD};
        color: {ON_ACCENT}; border: none; border-radius: 4px;
        padding: 11px 18px; font-size: 14px; font-weight: 600;
    }}
    QPushButton#Danger:hover   {{ background: {BAD}; }}
    QPushButton#Danger:disabled {{ background: {CARD2}; color: {MUTED}; }}

    QPushButton#Link {{
        background: transparent; color: {ACCENT_SOFT}; border: none;
        font-size: 13px; padding: 4px;
    }}
    QPushButton#Link:hover {{ color: {ACCENT_MID}; }}

    /* Compact per-account stop button. */
    QPushButton#KillBtn {{
        background: {KILL_BG}; color: {BAD}; border: 1px solid {BORDER2};
        border-radius: 4px; padding: 6px 16px; font-size: 13px; font-weight: 600;
        min-width: 40px;
    }}
    QPushButton#KillBtn:hover {{
        background: {KILL_BG_HOVER}; border-color: {BAD}; color: {TEXT};
    }}
    QPushButton#KillBtn:pressed {{ background: {KILL_BG}; }}

    /* Inline countdown shown beside an account status. */
    QLabel#TimerBadge {{
        color: {ACCENT_SOFT}; background: {CARD2};
        border-radius: 4px; padding: 2px 8px; font-size: 12px; font-weight: 600;
    }}

    QPlainTextEdit#ActivityLog {{
        background: {CARD2}; border: 1px solid {BORDER};
        border-radius: 4px; padding: 10px; color: {TEXT}; font-size: 12px;
        font-family: "Cascadia Mono", "Consolas", monospace;
    }}

    /* Page scrolling and scrollbar appearance. */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 4px 2px 4px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {SCROLL_THUMB}; border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {BORDER2}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

    /* Theme and page-selection combo boxes. */
    QComboBox {{
        background: {FIELD}; border: 1px solid {BORDER}; border-radius: 4px;
        padding: 7px 11px; color: {TEXT}; font-size: 13px;
    }}
    QComboBox#PageSelector {{
        min-height: 36px; padding-left: 12px; font-size: 14px;
        font-weight: 600;
    }}
    QComboBox:hover {{ border-color: {BORDER2}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {CARD2}; border: 1px solid {BORDER}; border-radius: 4px;
        selection-background-color: {ACCENT}; padding: 4px; outline: none;
    }}

    QListWidget#GameResults {{
        background: {CARD2}; border: 1px solid {BORDER}; border-radius: 4px;
        color: {TEXT}; outline: none;
    }}
    QListWidget#GameResults::item {{
        min-height: 56px; padding: 4px 8px;
        border-bottom: 1px solid {BORDER};
    }}
    QListWidget#GameResults::item:hover {{ background: {FIELD_HOVER}; }}
    QListWidget#GameResults::item:selected {{
        background: {ACCENT}; color: {ON_ACCENT};
    }}

    QCheckBox {{ color: {TEXT}; font-size: 13px; spacing: 8px; }}
    """
