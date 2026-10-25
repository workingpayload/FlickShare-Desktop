"""Dark Qt stylesheet for the FlickShare desktop app."""

BG = "#0F1117"
SURFACE = "#171A23"
SURFACE_2 = "#1F2330"
BORDER = "#2A2F3D"
TEXT = "#E7E9EE"
MUTED = "#9AA3B2"
ACCENT = "#7C5CFF"
ACCENT_2 = "#22D3EE"
SUCCESS = "#34D399"
DANGER = "#F87171"
WARNING = "#FBBF24"

STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Root {{ background: {BG}; }}
QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QLabel {{ background: transparent; }}
QLabel#Title {{ font-size: 18pt; font-weight: 700; }}
QLabel#CardTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Endpoint {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12pt; font-weight: 600; }}
QLabel#Headline {{ font-size: 13pt; font-weight: 600; }}
QLabel#Pill {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 2px 9px;
    color: {MUTED};
    font-size: 9pt;
}}
QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #5B6270; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: none;
    color: white;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {ACCENT}; }}
QPushButton#Danger {{ color: {DANGER}; }}
QToolButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px;
}}
QToolButton:hover {{ background: {SURFACE_2}; }}
QProgressBar {{
    background: {SURFACE_2};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
}}
QTreeWidget {{
    background: {SURFACE};
    border: none;
    alternate-background-color: #1A1E28;
    outline: 0;
}}
QTreeWidget::item {{ padding: 6px 4px; border: none; }}
QTreeWidget::item:selected {{ background: #2B2550; color: {TEXT}; }}
QTreeWidget::item:hover {{ background: {SURFACE_2}; }}
QListWidget#Devices {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    outline: 0;
    padding: 4px;
}}
QListWidget#Devices::item {{ padding: 7px 8px; border-radius: 8px; }}
QListWidget#Devices::item:selected {{ background: #2B2550; color: {TEXT}; }}
QListWidget#Devices::item:hover {{ background: {SURFACE_2}; }}
QHeaderView::section {{
    background: {SURFACE};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-weight: 600;
}}
QLineEdit, QSpinBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 5px;
    border: 1px solid {BORDER};
    background: {SURFACE_2};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox:disabled {{ color: #5B6270; }}
QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {SURFACE_2}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
"""
