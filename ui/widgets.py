"""Переиспользуемые виджеты интерфейса."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QRectF, Signal, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QLinearGradient, QPainterPath
from PySide6.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QSizePolicy,
    QPushButton, QGraphicsDropShadowEffect, QScrollArea, QCheckBox,
)

from . import theme


# ---------------------------------------------------------------- базовые блоки


class Card(QFrame):
    """Карточка с необязательным заголовком."""

    def __init__(self, title: str = "", subtitle: str = "", flat: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("CardFlat" if flat else "Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(12)
        if title:
            head = QVBoxLayout()
            head.setSpacing(2)
            lbl = QLabel(title)
            lbl.setObjectName("CardTitle")
            head.addWidget(lbl)
            if subtitle:
                sub = QLabel(subtitle)
                sub.setObjectName("Dim")
                sub.setWordWrap(True)
                head.addWidget(sub)
            self.body.addLayout(head)

    def add(self, widget):
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout):
        self.body.addLayout(layout)
        return layout


class Badge(QLabel):
    """Цветная метка-пилюля."""

    def __init__(self, text: str, color: str = theme.ACCENT, parent=None):
        super().__init__(text, parent)
        self.set_color(color)
        self.setAlignment(Qt.AlignCenter)
        # метка не должна растягиваться по высоте вместе с соседями
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def set_color(self, color: str):
        self.setStyleSheet(
            f"background: {color}22; color: {color}; border: 1px solid {color}66;"
            "border-radius: 9px; padding: 3px 10px; font-size: 11px; font-weight: 600;")


class SectionTitle(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setObjectName("SectionHeader")


def clear_layout(layout):
    """Полностью очищает layout.

    Важно снимать родителя сразу: deleteLater удаляет виджет только когда до него
    дойдёт очередь событий, а до этого он продолжает рисоваться поверх нового
    содержимого.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
            item.layout().deleteLater()


def dim_label(text: str, wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Dim")
    lbl.setWordWrap(wrap)
    return lbl


def scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    """Прокручиваемая страница: возвращает (область, layout содержимого)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    inner = QWidget()
    inner.setObjectName("Root")
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(30, 26, 30, 30)
    layout.setSpacing(18)
    area.setWidget(inner)
    return area, layout


def page_header(title: str, subtitle: str) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return box


# ---------------------------------------------------------------- индикаторы


class Gauge(QWidget):
    """Круговой индикатор с подписью."""

    def __init__(self, label: str = "", unit: str = "%", parent=None):
        super().__init__(parent)
        self.label = label
        self.unit = unit
        self.value = 0.0
        self.maximum = 100.0
        self.color = QColor(theme.ACCENT)
        self.caption = ""
        self.setMinimumSize(122, 146)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set_value(self, value, color: str | None = None, caption: str = ""):
        self.value = 0.0 if value is None else float(value)
        self.caption = caption
        if color:
            self.color = QColor(color)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        caption_band = 20                       # полоса под подпись, ниже кольца
        side = min(self.width(), self.height() - caption_band) - 12
        rect = QRectF((self.width() - side) / 2, 6, side, side)

        p.setPen(QPen(QColor(theme.BORDER), 9, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 225 * 16, -270 * 16)

        ratio = 0.0 if not self.maximum else max(0.0, min(1.0, self.value / self.maximum))
        if ratio > 0:
            p.setPen(QPen(self.color, 9, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 225 * 16, int(-270 * 16 * ratio))

        p.setPen(QColor(theme.TEXT))
        p.setFont(QFont("Segoe UI", 17, QFont.Bold))
        text = "—" if self.value is None else f"{self.value:.0f}"
        p.drawText(rect, Qt.AlignCenter, text + self.unit)

        p.setPen(QColor(theme.TEXT_DIM))
        p.setFont(QFont("Segoe UI", 8))
        caption = QRectF(0, rect.bottom() + 2, self.width(), caption_band - 2)
        metrics = p.fontMetrics()
        p.drawText(caption, Qt.AlignCenter,
                   metrics.elidedText(self.caption or self.label,
                                      Qt.ElideRight, int(self.width()) - 4))
        p.end()


class Sparkline(QWidget):
    """Компактный график последних значений."""

    def __init__(self, capacity: int = 90, color: str = theme.ACCENT, parent=None):
        super().__init__(parent)
        self.points = deque(maxlen=capacity)
        self.color = QColor(color)
        self.maximum = 100.0
        self.setMinimumHeight(52)

    def push(self, value: float):
        self.points.append(float(value or 0))
        self.update()

    def set_color(self, color: str):
        self.color = QColor(color)

    def paintEvent(self, event):
        if len(self.points) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        step = w / max(len(self.points) - 1, 1)
        top = 4

        path = QPainterPath()
        fill = QPainterPath()
        for i, value in enumerate(self.points):
            ratio = max(0.0, min(1.0, value / self.maximum))
            x = i * step
            y = h - 2 - ratio * (h - top - 2)
            if i == 0:
                path.moveTo(x, y)
                fill.moveTo(x, h)
                fill.lineTo(x, y)
            else:
                path.lineTo(x, y)
                fill.lineTo(x, y)
        fill.lineTo(w, h)
        fill.closeSubpath()

        grad = QLinearGradient(QPointF(0, 0), QPointF(0, h))
        c = QColor(self.color)
        c.setAlpha(90)
        grad.setColorAt(0, c)
        c2 = QColor(self.color)
        c2.setAlpha(0)
        grad.setColorAt(1, c2)
        p.fillPath(fill, grad)

        p.setPen(QPen(self.color, 2))
        p.drawPath(path)
        p.end()


class StatTile(Card):
    """Плитка: большое число, подпись и график."""

    def __init__(self, title: str, unit: str = "%", with_chart: bool = True, parent=None):
        super().__init__(flat=True, parent=parent)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(4)
        self.unit = unit

        top = QHBoxLayout()
        self.name = QLabel(title)
        self.name.setObjectName("Dim")
        self.extra = QLabel("")
        self.extra.setObjectName("Faint")
        self.extra.setAlignment(Qt.AlignRight)
        top.addWidget(self.name)
        top.addStretch()
        top.addWidget(self.extra)
        self.body.addLayout(top)

        self.value = QLabel("—")
        self.value.setObjectName("BigNumber")
        self.body.addWidget(self.value)

        self.chart = Sparkline() if with_chart else None
        if self.chart:
            self.body.addWidget(self.chart)

    def update_value(self, value, color: str | None = None, extra: str = ""):
        if value is None:
            self.value.setText("—")
        else:
            self.value.setText(f"{value:.0f}{self.unit}")
            if self.chart:
                self.chart.push(value)
        if color:
            self.value.setStyleSheet(f"color: {color};")
            if self.chart:
                self.chart.set_color(color)
        self.extra.setText(extra)


class KeyValue(QWidget):
    """Строка «параметр — значение»."""

    def __init__(self, key: str, value: str, value_color: str = theme.TEXT, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        k = QLabel(key)
        k.setObjectName("Dim")
        self.v = QLabel(value)
        self.v.setStyleSheet(f"color: {value_color}; font-weight: 600;")
        self.v.setAlignment(Qt.AlignRight)
        self.v.setWordWrap(True)
        lay.addWidget(k, 1)
        lay.addWidget(self.v, 1)

    def set_value(self, text: str, color: str | None = None):
        self.v.setText(text)
        if color:
            self.v.setStyleSheet(f"color: {color}; font-weight: 600;")


class ToggleRow(QFrame):
    """Строка твика: название, описание, объяснение «зачем» и переключатель."""

    toggled = Signal(str, bool)

    def __init__(self, tweak_id: str, title: str, desc: str, why: str,
                 risk: str = "низкий", reboot: bool = False, parent=None,
                 expert: bool = False, warn: str = ""):
        super().__init__(parent)
        self.setObjectName("CardFlat")
        self.tweak_id = tweak_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        self.check = QCheckBox(title)
        self.check.setStyleSheet("font-weight: 600; font-size: 13px;")
        top.addWidget(self.check, 1)

        risk_color = {"низкий": theme.OK, "средний": theme.WARN,
                      "высокий": theme.DANGER}.get(risk, theme.WARN)
        if expert:
            top.addWidget(Badge("эксперт", theme.ACCENT_2))
        if risk != "нулевой":
            top.addWidget(Badge(f"риск: {risk}", risk_color))
        if reboot:
            top.addWidget(Badge("перезагрузка", theme.TEXT_FAINT))
        lay.addLayout(top)

        d = QLabel(desc)
        d.setObjectName("Dim")
        d.setWordWrap(True)
        lay.addWidget(d)

        w = QLabel("→ " + why)
        w.setObjectName("Faint")
        w.setWordWrap(True)
        lay.addWidget(w)

        if warn:
            warn_lbl = QLabel("⚠ " + warn)
            warn_lbl.setWordWrap(True)
            warn_lbl.setStyleSheet(
                f"color: {theme.DANGER}; font-size: 11px; font-weight: 600;")
            lay.addWidget(warn_lbl)

        self.check.toggled.connect(lambda state: self.toggled.emit(self.tweak_id, state))

    def set_state(self, checked: bool):
        self.check.blockSignals(True)
        self.check.setChecked(checked)
        self.check.blockSignals(False)

    def is_checked(self) -> bool:
        return self.check.isChecked()


class Collapsible(QFrame):
    """Сворачиваемая секция: заголовок-кнопка и скрываемое содержимое."""

    def __init__(self, title: str, subtitle: str = "", opened: bool = False,
                 accent: str = theme.ACCENT, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.head = QPushButton()
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setCheckable(True)
        self.head.setChecked(opened)
        self.head.setStyleSheet(
            "QPushButton { background: transparent; border: none; text-align: left; "
            "padding: 14px 18px; font-size: 15px; font-weight: 600; }"
            f"QPushButton:hover {{ color: {accent}; }}")
        self.head.clicked.connect(self._toggle)
        root.addWidget(self.head)

        self._title = title
        self._count = 0
        self._accent = accent

        self.holder = QWidget()
        self.body = QVBoxLayout(self.holder)
        self.body.setContentsMargins(18, 0, 18, 16)
        self.body.setSpacing(10)
        root.addWidget(self.holder)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Dim")
            sub.setWordWrap(True)
            self.body.addWidget(sub)

        self.holder.setVisible(opened)
        self._sync_head()

    def add(self, widget):
        self.body.addWidget(widget)
        self._count += 1
        self._sync_head()
        return widget

    def _toggle(self):
        self.holder.setVisible(self.head.isChecked())
        self._sync_head()

    def _sync_head(self):
        arrow = "▾" if self.head.isChecked() else "▸"
        suffix = f"   ·   {self._count}" if self._count else ""
        self.head.setText(f"{arrow}   {self._title}{suffix}")

    def set_open(self, opened: bool):
        self.head.setChecked(opened)
        self._toggle()


def glow(widget: QWidget, color: str = theme.ACCENT, radius: int = 26):
    """Мягкое свечение вокруг виджета."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(radius)
    effect.setOffset(0, 0)
    c = QColor(color)
    c.setAlpha(120)
    effect.setColor(c)
    widget.setGraphicsEffect(effect)
    return effect


def primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("Primary")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setMinimumHeight(40)
    return btn


def ghost_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("Ghost")
    btn.setCursor(Qt.PointingHandCursor)
    return btn
