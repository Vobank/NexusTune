"""Командная палитра: Ctrl+K — и до любого места приложения одна строка.

Идея взята из редакторов кода, но здесь она уместнее: страниц много, а нужное
действие обычно знаешь по названию, а не по тому, на какой оно вкладке.
Поиск работает по русскому названию, по описанию и по латинской раскладке —
если забыл переключить язык, «repybwf» всё равно найдёт «Кузницу».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QLabel, QWidget,
)

from . import theme

# Раскладка: что напечатается латиницей, если забыл переключить язык
_LAYOUT = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.`",
    "йцукенгшщзхъфывапролджэячсмитьбюё")


@dataclass
class Command:
    """Одно действие в палитре."""
    title: str
    hint: str
    run: Callable[[], None]
    group: str = ""
    keywords: str = ""

    @property
    def haystack(self) -> str:
        return f"{self.title} {self.hint} {self.group} {self.keywords}".lower()


def _score(command: Command, query: str) -> int:
    """Насколько команда подходит запросу. 0 — не подходит."""
    if not query:
        return 1
    hay = command.haystack
    title = command.title.lower()
    best = 0
    for variant in {query, query.translate(_LAYOUT)}:
        if not variant:
            continue
        if title.startswith(variant):
            best = max(best, 100)
        elif variant in title:
            best = max(best, 70)
        elif variant in hay:
            best = max(best, 40)
        else:
            # все слова запроса встречаются где-то — слабое, но совпадение
            words = [w for w in variant.split() if w]
            if words and all(w in hay for w in words):
                best = max(best, 20)
    return best


class CommandPalette(QDialog):
    """Всплывающее окно поиска по действиям."""

    def __init__(self, commands: list[Command], parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.commands = commands
        self.filtered: list[Command] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        frame = QWidget()
        frame.setObjectName("PaletteFrame")
        frame.setStyleSheet(
            f"#PaletteFrame {{ background: {theme.SURFACE}; "
            f"border: 1px solid {theme.ACCENT}66; border-radius: 14px; }}")
        root.addWidget(frame)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Куда идём? Название страницы или действия…")
        self.input.setStyleSheet(
            f"QLineEdit {{ background: {theme.SURFACE_2}; border: 1px solid "
            f"{theme.BORDER_LIGHT}; border-radius: 10px; padding: 12px 14px; "
            "font-size: 15px; }")
        self.input.textChanged.connect(self._refilter)
        self.input.installEventFilter(self)
        lay.addWidget(self.input)

        self.list = QListWidget()
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            f"QListWidget::item {{ padding: 9px 12px; border-radius: 9px; "
            f"color: {theme.TEXT}; }}"
            f"QListWidget::item:selected {{ background: {theme.rgba(theme.ACCENT, 34)}; "
            f"color: {theme.TEXT}; }}")
        self.list.itemActivated.connect(self._run_item)
        self.list.itemClicked.connect(self._run_item)
        lay.addWidget(self.list)

        footer = QLabel("↑ ↓ — выбор     Enter — выполнить     Esc — закрыть")
        footer.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        footer.setAlignment(Qt.AlignCenter)
        lay.addWidget(footer)

        self.setFixedWidth(620)
        self.setFixedHeight(440)
        self._refilter("")

    # ------------------------------------------------------------ поиск

    def _refilter(self, text: str):
        query = text.strip().lower()
        scored = [(s, c) for c in self.commands if (s := _score(c, query))]
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))
        self.filtered = [c for _s, c in scored]

        self.list.clear()
        for command in self.filtered[:60]:
            label = command.title
            if command.group:
                label = f"{command.group}  ·  {command.title}"
            item = QListWidgetItem(f"{label}\n{command.hint}" if command.hint else label)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _run_item(self, item: QListWidgetItem):
        index = self.list.row(item)
        if 0 <= index < len(self.filtered):
            command = self.filtered[index]
            self.accept()
            command.run()

    # ------------------------------------------------------------ клавиши

    def eventFilter(self, obj, event):
        """Стрелки должны листать список, даже когда курсор в поле ввода."""
        if obj is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow()
                step = 1 if key == Qt.Key_Down else -1
                new = max(0, min(self.list.count() - 1, row + step))
                self.list.setCurrentRow(new)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                item = self.list.currentItem()
                if item:
                    self._run_item(item)
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def show_centered(self, parent):
        """Показывает палитру по центру верхней трети окна."""
        geo = parent.geometry()
        x = geo.center().x() - self.width() // 2
        y = geo.top() + max(80, geo.height() // 6)
        self.move(x, y)
        self.input.clear()
        self.input.setFocus()
        self.show()
