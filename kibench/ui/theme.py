"""Look and feel: Fusion style plus one stylesheet driven by a small set of colour tokens (light and dark)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette


@dataclass(frozen=True)
class Tokens:
    bg: str            # window background
    surface: str       # panels, inputs, cards
    raised: str        # hover, alternate rows, assistant bubble
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    accent_soft: str   # user bubble, selection
    warn: str
    danger: str
    ok: str


LIGHT = Tokens(bg="#f3f4f6", surface="#ffffff", raised="#f0f2f5", border="#dcdfe4", text="#1f2328",
               muted="#6b7280", accent="#2f6fed", accent_text="#ffffff", accent_soft="#e3ecfd",
               warn="#b7791f", danger="#c53030", ok="#2f855a")
DARK = Tokens(bg="#1b1c1f", surface="#25272b", raised="#2e3035", border="#3a3d43", text="#e2e4e8",
              muted="#8f949c", accent="#4d8bff", accent_text="#ffffff", accent_soft="#263a5e",
              warn="#e0a43a", danger="#f07070", ok="#5cc48a")

_current = LIGHT


def tokens() -> Tokens:
    return _current


def _stylesheet(t: Tokens) -> str:
    return f"""
* {{ outline: none; }}
QMainWindow, QDialog {{ background: {t.bg}; }}
QWidget {{ color: {t.text}; font-size: 10pt; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 4px 6px; }}

QToolBar {{ background: {t.surface}; border: none; border-bottom: 1px solid {t.border}; padding: 4px 8px; spacing: 4px; }}
QToolBar QToolButton {{ padding: 5px 10px; border-radius: 6px; border: 1px solid transparent; }}
QToolBar QToolButton:hover {{ background: {t.raised}; }}
QToolBar QToolButton:checked {{ background: {t.accent_soft}; color: {t.accent}; }}
QLabel#projectLabel {{ color: {t.muted}; padding-left: 6px; }}
QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.muted}; }}

QSplitter::handle {{ background: {t.bg}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
QWidget#column {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {t.accent}; }}
QLineEdit#searchBox {{ font-size: 11pt; padding: 8px 10px; border-radius: 8px; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border};
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}

QPushButton {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 12px; }}
QPushButton:hover {{ background: {t.raised}; border-color: {t.muted}; }}
QPushButton:pressed {{ background: {t.accent_soft}; }}
QPushButton:checked {{ background: {t.accent_soft}; color: {t.accent}; border-color: {t.accent}; font-weight: 600; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.raised}; }}
QPushButton[primary="true"] {{ background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent};
    font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {t.accent}; border-color: {t.text}; }}
QPushButton[primary="true"]:disabled {{ background: {t.raised}; color: {t.muted}; border-color: {t.border}; }}
QPushButton[flat="true"] {{ background: transparent; border: 1px solid transparent; color: {t.accent}; padding: 3px 8px; }}
QPushButton[flat="true"]:hover {{ background: {t.accent_soft}; }}

QCheckBox {{ spacing: 6px; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 8px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.muted}; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t.border}; top: -1px; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 7px 14px; color: {t.muted}; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.accent}; border-bottom: 2px solid {t.accent}; font-weight: 600; }}

QTableView, QTreeView, QTreeWidget, QListView {{ background: {t.surface}; alternate-background-color: {t.raised};
    border: 1px solid {t.border}; border-radius: 8px; gridline-color: transparent;
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QTableView::item, QTreeView::item {{ padding: 2px 6px; border: none; }}
QHeaderView::section {{ background: {t.surface}; color: {t.muted}; border: none; border-bottom: 1px solid {t.border};
    padding: 6px; font-weight: 600; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {t.border}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QLabel#muted {{ color: {t.muted}; }}
QLabel#sectionTitle {{ font-weight: 700; font-size: 11pt; }}
QLabel#bubbleUser {{ background: {t.accent_soft}; border-radius: 12px; padding: 8px 12px; }}
QLabel#bubbleAssistant {{ background: {t.raised}; border-radius: 12px; padding: 8px 12px; }}
QLabel#toolNote {{ color: {t.muted}; font-size: 9pt; padding: 0 4px; }}
QLabel#errorNote {{ color: {t.danger}; font-size: 9pt; padding: 2px 4px; }}
QFrame#card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame#card:hover {{ border-color: {t.accent}; }}
QFrame#composer {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame#composer QPlainTextEdit {{ border: none; background: transparent; padding: 4px; }}
QFrame#partHeader {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
"""


def apply(app) -> Tokens:
    """Fusion + palette + stylesheet, following the OS light/dark setting."""
    global _current
    app.setStyle("Fusion")
    try:
        dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        dark = app.palette().color(QPalette.Window).lightness() < 128
    forced = os.environ.get("KIBENCH_THEME", "").lower()      # dark | light overrides the OS setting
    if not forced:
        try:
            from ..config import Settings
            forced = Settings.load().theme
        except Exception:
            pass
    if forced in ("dark", "light"):
        dark = forced == "dark"
    t = DARK if dark else LIGHT
    _current = t
    pal = QPalette()
    for role, col in ((QPalette.Window, t.bg), (QPalette.WindowText, t.text), (QPalette.Base, t.surface),
                      (QPalette.AlternateBase, t.raised), (QPalette.Text, t.text), (QPalette.Button, t.surface),
                      (QPalette.ButtonText, t.text), (QPalette.Highlight, t.accent),
                      (QPalette.HighlightedText, t.accent_text), (QPalette.ToolTipBase, t.surface),
                      (QPalette.ToolTipText, t.text), (QPalette.Link, t.accent), (QPalette.PlaceholderText, t.muted),
                      (QPalette.Mid, t.border), (QPalette.Midlight, t.raised)):
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t.muted))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t.muted))
    app.setPalette(pal)
    app.setStyleSheet(_stylesheet(t))
    return t
