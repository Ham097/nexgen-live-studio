from html import escape
from itertools import groupby
import re
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QListWidgetItem,
                              QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .analysis import LEMMAS, REVIEW, RULES, TOKEN

DATA = Qt.ItemDataRole.UserRole


def label(text="", role=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty("role", role)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setProperty("primary", True)
    widget.clicked.connect(callback)
    return widget


def line(placeholder="", text=""):
    widget = QLineEdit(text)
    widget.setPlaceholderText(placeholder)
    if placeholder:
        widget.setAccessibleName(placeholder)
    return widget


def combo(items):
    widget = QComboBox()
    widget.addItems(items)
    return widget


def column(layout=None, margins=0, spacing=13):
    panel = QWidget()
    layout = layout or QVBoxLayout()
    panel.setLayout(layout)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return panel, layout


def row(*widgets):
    panel, layout = column(QHBoxLayout(), spacing=8)
    panel.setProperty("transparent", True)
    for widget in widgets:
        if widget is None:
            layout.addStretch()
        else:
            layout.addWidget(widget)
    return panel


def card(*widgets):
    frame = QFrame()
    frame.setProperty("panel", True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 15)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    return frame


def scroll(widget):
    view = QScrollArea()
    view.setWidgetResizable(True)
    view.setWidget(widget)
    return view


def heading(kicker, title, note):
    panel, contents = column(spacing=5)
    for text, role in ((kicker, "eyebrow"), (title, "title"), (note, "muted")):
        if text:
            contents.addWidget(label(text, role, wrap=True))
    return panel


def listings(widget, entries, preserve=True, follow=False):
    widget.setWordWrap(True)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    selected = selected_data(widget) if preserve else None
    bar = widget.verticalScrollBar()
    previous = bar.value()
    bottom = previous >= bar.maximum() - 6
    old = widget.blockSignals(True)
    widget.clear()
    for title, data in entries:
        item = QListWidgetItem(title)
        item.setData(DATA, data)
        widget.addItem(item)
        if data == selected:
            widget.setCurrentItem(item)
    if follow and bottom:
        widget.scrollToBottom()
    else:
        bar.setValue(previous)
    widget.blockSignals(old)


def selected_data(widget):
    item = widget.currentItem()
    return item.data(DATA) if item else None


def checked(text, value=False):
    widget = QCheckBox(text)
    widget.setChecked(bool(value))
    return widget


def form_dialog(parent, title, fields, accept="Save"):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(470)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(22, 18, 22, 18)
    layout.addWidget(label(title, "section"))
    form = QFormLayout()
    form.setSpacing(10)
    for name, field in fields:
        form.addRow(name, field) if name else form.addRow(field)
    fields_panel = QWidget()
    fields_panel.setLayout(form)
    if len(fields) > 8:
        view = scroll(fields_panel)
        view.setMinimumHeight(400)
        layout.addWidget(view)
    else:
        layout.addWidget(fields_panel)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(accept)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog


def plain(text="", placeholder=""):
    result = QPlainTextEdit(text)
    result.setPlaceholderText(placeholder)
    result.setMaximumHeight(86)
    return result


def seconds(milliseconds):
    whole = int(milliseconds // 1000)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def value(raw):
    return "—" if raw is None else str(raw)


def turn_details(turn):
    features = turn["features"]
    vocab, grammar, review = (features.get(key, {}) for key in ("vocabulary", "grammar", "review"))
    status = " · ".join([turn["evidence"].title(), *turn["flags"]])
    if turn["excluded"]:
        status += " · Excluded"
    text = annotated_text(turn["text"], vocab, grammar, review if turn["error_count"] is None else [])
    detail = [f"<p style='color:#6641bc'><b>{turn['speaker'].title()} · {seconds(turn['start_ms'])}</b> · {escape(status)}</p>",
              f"<p style='font-size:17px'>{text}</p>"]
    for title, payload, color in (
        ("Vocabulary", ", ".join(vocab), "#315d55"),
        ("Grammar candidates", ", ".join(grammar), "#5736a3"),
        ("Confirmed constructions", ", ".join(turn["confirmed_grammar"]), "#315d55"),
        ("Check", ", ".join(review) if turn["error_count"] is None else f'{turn["error_count"]} confirmed error(s)', "#952843"),
        ("Original recognition", turn["original"] if turn["verified"] is not None else "", "#645c78"),
        ("Teaching correction", turn["teaching"], "#5736a3"),
    ):
        if payload:
            detail.append(f"<p style='color:{color}'><b>{title}</b><br>{escape(payload)}</p>")
    return "".join(detail)


def annotated_text(text, vocab, grammar, review):
    normalized = text.lower().replace("’", "'")
    styles = [0] * len(text)
    for word in TOKEN.finditer(normalized):
        if LEMMAS.get(word.group(), word.group()) in vocab:
            styles[word.start():word.end()] = [1] * len(word.group())
    for rank, rules, keys in ((2, RULES, grammar), (3, REVIEW, review)):
        for key in keys:
            for match in re.finditer(rules[key], normalized):
                styles[match.start():match.end()] = [rank] * len(match.group())
    colors = ("", "#277361", "#5c2eb1", "#a3294b")
    pieces = []
    for rank, chars in groupby(zip(styles, text), key=lambda pair: pair[0]):
        content = escape("".join(char for _, char in chars)).replace("\n", "<br>")
        pieces.append(f'<span style="color:{colors[rank]};font-weight:600">{content}</span>' if rank else content)
    return "".join(pieces)


class Trend(QWidget):
    def __init__(self):
        super().__init__()
        self.values = []
        self.setMinimumHeight(90)
        self.setAccessibleName("Comparable speaking pace trend")

    def set_values(self, values):
        self.values = values[-12:]
        self.setAccessibleDescription(", ".join(map(str, self.values)))
        self.update()

    def paintEvent(self, event):
        paint = QPainter(self)
        paint.setRenderHint(QPainter.RenderHint.Antialiasing)
        if len(self.values) < 2:
            paint.setPen(QColor("#645c78"))
            paint.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Two comparable lessons needed")
            return
        from PySide6.QtCore import QPointF
        low, high = min(self.values), max(self.values)
        span = max(1, high - low)
        height, width = self.height() - 30, self.width() - 46
        points = [QPointF(23 + width * index / (len(self.values) - 1),
                          15 + height * (.5 if low == high else (high - number) / span))
                  for index, number in enumerate(self.values)]
        paint.setPen(QPen(QColor("#d9cfed"), 1))
        paint.drawLine(18, self.height() - 8, self.width() - 18, self.height() - 8)
        paint.setPen(QPen(QColor("#7138f4"), 3))
        for before, after in zip(points, points[1:]):
            paint.drawLine(before, after)
        paint.setBrush(QColor("#7138f4"))
        for point in points:
            paint.drawEllipse(point, 3.5, 3.5)
