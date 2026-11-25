STYLE = """
* { font-family: "Segoe UI"; font-size: 13px; }
QWidget { background: #f7f7fc; color: #201a35; }
QFrame[panel="true"] { background: #ffffff; border: 1px solid #e9e5f2; border-radius: 16px; }
QFrame[panel="true"] QLabel, QFrame[panel="true"] QWidget[transparent="true"] { background: transparent; }
QFrame#meetHome { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ede4ff, stop:1 #f3f0ff); border-color: #e0d4f7; }
QLabel[role="title"] { font-size: 31px; font-weight: 700; }
QLabel[role="section"] { font-size: 18px; font-weight: 700; }
QLabel[role="eyebrow"] { font-size: 11px; font-weight: 700; letter-spacing: 1px; color: #6641bc; }
QLabel[role="muted"] { color: #645c78; }
QLabel[role="value"] { font-size: 26px; font-weight: 700; color: #39246f; }
QLabel[role="alert"] { color: #9a2847; font-weight: 700; }
QLabel[role="brand"] { color: #211942; font-size: 18px; font-weight: 700; }
QFrame#rail { background: #f0ebfb; border-right: 1px solid #e7e0f3; }
QFrame#rail QLabel { background: transparent; }
QPushButton { background: #fff; color: #352555; border: 1px solid #ded6ee; border-radius: 8px; padding: 8px 13px; font-weight: 600; }
QPushButton:hover { background: #f4edff; border-color: #a78bdc; }
QPushButton:focus { border: 2px solid #884ef0; padding: 7px 12px; }
QPushButton:disabled { color: #777083; border-color: #ece8f1; background: #f4f2f7; }
QPushButton[primary="true"] { background: #6634dc; color: white; border-color: #6634dc; }
QPushButton[primary="true"]:hover { background: #5428c3; }
QPushButton[recording="true"] { background: #9b2345; color: white; border-color: #9b2345; }
QPushButton[nav="true"] { background: transparent; border-color: transparent; text-align: left; }
QPushButton[nav="true"]:checked { background: #ded1ff; color: #43228a; border-color: #ded1ff; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QKeySequenceEdit, QTextBrowser {
  background: #fff; border: 1px solid #e0daec; border-radius: 8px; padding: 7px 9px; selection-background-color: #7043d4;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus { border-color: #8854e7; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e4d8fc; selection-color: #32214f; }
QListWidget { background: white; border: 1px solid #e9e5f2; border-radius: 8px; outline: 0; }
QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #f1edf6; }
QListWidget::item:selected { background: #ede4ff; color: #361e6e; }
QListWidget::item:hover { background: #f6f0ff; }
QCheckBox { background: transparent; spacing: 7px; }
QCheckBox::indicator { width: 14px; height: 14px; background: white; border: 1px solid #9b8aaf; border-radius: 4px; }
QCheckBox::indicator:checked { background: #6634dc; border: 1px solid #6634dc; image: url(:/qt-project.org/styles/commonstyle/images/checkbox_checked.png); }
QCheckBox::indicator:disabled { border-color: #d8d1e4; background: #efebf5; }
QProgressBar { background: #eeebf6; border: 0; min-height: 6px; max-height: 6px; border-radius: 3px; }
QProgressBar::chunk { background: #7752d7; border-radius: 3px; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #f4f0fa; width: 10px; }
QScrollBar::handle:vertical { background: #c7b4e8; border-radius: 4px; min-height: 23px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QDialog { background: #f7f7fc; }
"""
