"""A compact, professional light stylesheet.

One flat QSS - clean and readable. Light only (no dark mode). The log
("terminal") and inputs share the light background. ``combobox-popup: 0``
makes every dropdown open as a list BELOW the box rather than a native
popup hovering over the current selection.
"""
from __future__ import annotations

_PAL = dict(
    bg="#f4f5f7", panel="#ffffff", text="#1b1f24", dim="#6b7280",
    border="#d6dae0", accent="#2563eb", accent_fg="#ffffff",
    log_bg="#fbfbfd", log_fg="#1b1f24",
    ok="#16a34a", fail="#dc2626", run="#b45309",
)

MONO = "JetBrains Mono, Menlo, Consolas, 'DejaVu Sans Mono', monospace"


def palette() -> dict[str, str]:
    return _PAL


def qss() -> str:
    c = _PAL
    return f"""
    QWidget {{ background: {c['bg']}; color: {c['text']};
        font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; font-size: 13px; }}
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QGroupBox {{ background: {c['panel']}; border: 1px solid {c['border']};
        border-radius: 8px; margin-top: 14px; padding: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
        color: {c['dim']}; font-weight: 600; }}
    QLabel#hint {{ color: {c['dim']}; }}
    QLabel#derived {{ color: {c['dim']}; font-family: {MONO}; font-size: 12px; }}
    QComboBox, QLineEdit, QSpinBox {{ background: {c['panel']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px; padding: 4px 8px; min-height: 22px; }}
    QComboBox:focus, QLineEdit:focus {{ border: 1px solid {c['accent']}; }}
    /* Read-only fields (e.g. CPP/M1P/M1OF derived from the layer file). */
    QLineEdit[readOnly="true"] {{ background: {c['bg']}; color: {c['dim']};
        border: 1px dashed {c['border']}; }}
    /* List-style popup that drops BELOW the box (not a native overlay). */
    QComboBox {{ combobox-popup: 0; }}
    QComboBox QAbstractItemView {{ background: {c['panel']}; color: {c['text']};
        border: 1px solid {c['border']}; selection-background-color: {c['accent']};
        selection-color: {c['accent_fg']}; outline: 0; }}
    QListWidget, QTableWidget {{ background: {c['panel']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px; }}
    QTableWidget {{ gridline-color: {c['border']}; }}
    QHeaderView::section {{ background: {c['bg']}; color: {c['dim']};
        border: none; border-bottom: 1px solid {c['border']}; padding: 4px 6px; font-weight: 600; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 6px; }}
    QTabBar::tab {{ background: {c['bg']}; color: {c['dim']}; padding: 5px 12px;
        border: 1px solid {c['border']}; border-bottom: none;
        border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background: {c['panel']}; color: {c['text']}; font-weight: 600; }}
    QPushButton {{ background: {c['panel']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px; padding: 6px 12px; font-weight: 600; }}
    QPushButton:hover {{ border: 1px solid {c['accent']}; }}
    QPushButton:disabled {{ color: {c['dim']}; border-color: {c['border']}; }}
    QPushButton#primary {{ background: {c['accent']}; color: {c['accent_fg']}; border: none; }}
    QPushButton#primary:disabled {{ background: {c['border']}; color: {c['dim']}; }}
    QPushButton#danger {{ color: {c['fail']}; }}
    /* The log "terminal" — light, consistent with the background. */
    QPlainTextEdit {{ background: {c['log_bg']}; color: {c['log_fg']};
        font-family: {MONO}; font-size: 12px; border: 1px solid {c['border']};
        border-radius: 6px; selection-background-color: {c['accent']};
        selection-color: {c['accent_fg']}; }}
    QScrollArea {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 6px; }}
    QLabel#badge {{ font-weight: 700; padding: 2px 10px; border-radius: 6px; }}
    QLabel#badge[state="ok"]   {{ background: {c['ok']};   color: #ffffff; }}
    QLabel#badge[state="fail"] {{ background: {c['fail']}; color: #ffffff; }}
    QLabel#badge[state="run"]  {{ background: {c['run']};  color: #ffffff; }}
    QLabel#badge[state="idle"] {{ background: {c['border']}; color: {c['dim']}; }}
    QStatusBar {{ background: {c['panel']}; color: {c['dim']}; border-top: 1px solid {c['border']}; }}
    QStatusBar QLabel {{ color: {c['dim']}; font-family: {MONO}; font-size: 12px; }}
    """
