"""Виджеты для страницы «Реактор»: агрессивные, тёмные, с живой пульсацией.

Здесь намеренно другой визуальный язык, чем на остальных страницах: красная
подсветка, мигание при опасности, тумблеры под «крышкой». Это единственная
часть приложения, где форма кричит о риске, — и это уместно ровно здесь.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QRectF, QTimer, Signal, QPointF
from PySide6.QtGui import (
    QColor, QPainter, QPen, QFont, QRadialGradient, QLinearGradient,
    QConicalGradient,
)
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

from . import theme

HAZARD = "#ff2d2d"
HAZARD_DIM = "#5a0e0e"
LIVE = "#ff5f5f"
STEEL = "#161a20"


class ReactorGauge(QWidget):
    """Круговой датчик с пульсацией и подсветкой по уровню.

    Чем выше значение, тем краснее и тем сильнее свечение. При превышении
    порога начинает мигать.
    """

    def __init__(self, caption: str = "", unit: str = "", maximum: float = 100.0,
                 danger_at: float = 85.0, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.unit = unit
        self.maximum = maximum
        self.danger_at = danger_at
        self.value = 0.0
        self._pulse = 0.0
        self._blink = False
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(50)

    def set_value(self, value: float):
        self.value = float(value or 0)
        self.update()

    def _animate(self):
        self._pulse = (self._pulse + 0.14) % (2 * 3.14159)
        if self.value >= self.danger_at:
            self._blink = not self._blink
        else:
            self._blink = False
        self.update()

    def _colour(self) -> QColor:
        ratio = min(1.0, self.value / self.maximum) if self.maximum else 0.0
        if ratio >= self.danger_at / self.maximum:
            return QColor(HAZARD)
        if ratio >= 0.6:
            return QColor(theme.WARN)
        return QColor(theme.ACCENT)

    def paintEvent(self, event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height() - 22) - 16
        rect = QRectF((self.width() - side) / 2, 8, side, side)
        centre = rect.center()
        colour = self._colour()

        # свечение под кольцом дышит в такт пульсу
        glow_alpha = int(40 + 30 * (math.sin(self._pulse) + 1) / 2)
        if self.value >= self.danger_at and not self._blink:
            glow_alpha = 10
        grad = QRadialGradient(centre, side / 1.6)
        c = QColor(colour); c.setAlpha(glow_alpha)
        grad.setColorAt(0, c)
        c2 = QColor(colour); c2.setAlpha(0)
        grad.setColorAt(1, c2)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawEllipse(rect.adjusted(-14, -14, 14, 14))

        # фоновое кольцо
        p.setPen(QPen(QColor(theme.BORDER), 10, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # заполнение
        ratio = max(0.0, min(1.0, self.value / self.maximum)) if self.maximum else 0.0
        if ratio > 0:
            pen = QPen(colour, 10, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 225 * 16, int(-270 * 16 * ratio))

        # число
        p.setPen(QColor(theme.TEXT) if not (self._blink and self.value >= self.danger_at)
                 else QColor(HAZARD))
        p.setFont(QFont("Consolas", int(side * 0.22), QFont.Bold))
        p.drawText(rect, Qt.AlignCenter, f"{self.value:.0f}{self.unit}")

        # подпись
        p.setPen(QColor(theme.TEXT_DIM))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(QRectF(0, rect.bottom() + 2, self.width(), 20),
                   Qt.AlignHCenter | Qt.AlignTop, self.caption.upper())
        p.end()


class PulseBar(QWidget):
    """Горизонтальная полоса-осциллограф: рисует историю значения 0..1."""

    def __init__(self, colour: str = HAZARD, parent=None):
        super().__init__(parent)
        self.colour = QColor(colour)
        self.points: list[float] = [0.0] * 120
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def push(self, value: float):
        self.points.append(max(0.0, min(1.0, value)))
        if len(self.points) > 120:
            self.points.pop(0)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        step = w / max(len(self.points) - 1, 1)

        path = [QPointF(i * step, h - 2 - v * (h - 6))
                for i, v in enumerate(self.points)]
        peak = max(self.points)
        colour = QColor(HAZARD if peak > 0.6 else theme.ACCENT)

        fill = QLinearGradient(0, 0, 0, h)
        c = QColor(colour); c.setAlpha(70); fill.setColorAt(0, c)
        c2 = QColor(colour); c2.setAlpha(0); fill.setColorAt(1, c2)
        poly = list(path) + [QPointF(w, h), QPointF(0, h)]
        p.setBrush(fill)
        p.setPen(Qt.NoPen)
        p.drawPolygon(*poly) if False else p.drawPolygon(poly)

        p.setPen(QPen(colour, 2))
        p.drawPolyline(path)
        p.end()


class HazardToggle(QWidget):
    """Тумблер под «крышкой»: чтобы включить, нужно откинуть предохранитель.

    Первый клик поднимает крышку, второй — щёлкает тумблером. Так случайно
    не включишь то, о чём потом пожалеешь.
    """

    toggled = Signal(bool)

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self.label = label
        self.on = False
        self.cover_open = False
        self.setFixedHeight(64)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_on(self, value: bool):
        self.on = bool(value)
        if self.on:
            self.cover_open = True
        self.update()

    def mousePressEvent(self, event):
        if not self.cover_open:
            self.cover_open = True
            self.update()
            return
        self.on = not self.on
        self.toggled.emit(self.on)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # корпус
        p.setBrush(QColor(STEEL))
        p.setPen(QPen(QColor(HAZARD_DIM), 2))
        p.drawRoundedRect(1, 1, w - 2, h - 2, 8, 8)

        track = QRectF(14, h / 2 - 13, 78, 26)
        if self.on:
            grad = QLinearGradient(track.topLeft(), track.bottomRight())
            grad.setColorAt(0, QColor(HAZARD))
            grad.setColorAt(1, QColor(HAZARD_DIM))
            p.setBrush(grad)
            p.setPen(QPen(QColor(LIVE), 1))
        else:
            p.setBrush(QColor("#0c0e12"))
            p.setPen(QPen(QColor(theme.BORDER_LIGHT), 1))
        p.drawRoundedRect(track, 13, 13)

        knob_x = track.right() - 24 if self.on else track.left() + 2
        p.setBrush(QColor("#e6edf3") if self.on else QColor(theme.TEXT_FAINT))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(knob_x, track.top() + 2, 22, 22))

        # надпись состояния
        p.setPen(QColor(HAZARD) if self.on else QColor(theme.TEXT_FAINT))
        p.setFont(QFont("Consolas", 11, QFont.Bold))
        p.drawText(QRectF(104, 0, w - 200, h), Qt.AlignVCenter | Qt.AlignLeft,
                   ("● " + self.label) if self.on else ("○ " + self.label))

        # «крышка» предохранителя
        if not self.cover_open:
            p.setBrush(QColor(0, 0, 0, 210))
            p.setPen(QPen(QColor(HAZARD), 2, Qt.DashLine))
            p.drawRoundedRect(1, 1, w - 2, h - 2, 8, 8)
            p.setPen(QColor(HAZARD))
            p.setFont(QFont("Segoe UI", 10, QFont.Bold))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "⚠  ОТКИНЬ КРЫШКУ  ⚠")
        p.end()


class WarningStrip(QLabel):
    """Мигающая полоса-предупреждение вверху страницы."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self._bright = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flip)
        self._timer.start(700)
        self._apply()

    def _flip(self):
        self._bright = not self._bright
        self._apply()

    def _apply(self):
        border = HAZARD if self._bright else HAZARD_DIM
        self.setStyleSheet(
            f"color: {LIVE}; background: {theme.rgba(HAZARD, 22)}; "
            f"border: 2px solid {border}; border-radius: 10px; padding: 10px; "
            "font-size: 13px; font-weight: 700;")


class CoreHeatmap(QWidget):
    """Живая карта ядер: каждый поток — своя ячейка, цвет по загрузке.

    Производительные ядра подписаны буквой P, экономичные — E. Ячейка,
    в которую сейчас упирается нагрузка, наливается красным и пульсирует.
    Это и есть «куча мигающих датчиков», только каждый показывает реальное
    ядро твоего процессора.
    """

    def __init__(self, topology=None, parent=None):
        super().__init__(parent)
        self.topology = topology
        self.loads: list[float] = []
        self.labels: list[str] = []
        self._blink = False
        if topology and topology.cores:
            best = max((c.efficiency for c in topology.cores), default=0)
            slots = {}
            for core in topology.cores:
                mark = "P" if (core.efficiency == best and topology.hybrid) else                        ("E" if topology.hybrid else "•")
                for thread in core.threads:
                    slots[thread] = mark
            self.labels = [slots.get(i, "•")
                           for i in range(topology.logical_count)]
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._flip)
        self._timer.start(400)

    def _flip(self):
        self._blink = not self._blink
        self.update()

    def set_loads(self, loads):
        self.loads = list(loads or [])
        if not self.labels and self.loads:
            self.labels = ["•"] * len(self.loads)
        self.update()

    def paintEvent(self, event):
        if not self.loads:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.loads)
        gap = 4
        cell_w = (self.width() - gap * (n - 1)) / n
        cell_h = self.height()

        for i, load in enumerate(self.loads):
            x = i * (cell_w + gap)
            rect = QRectF(x, 0, cell_w, cell_h)
            if load >= 85:
                base = QColor(HAZARD)
            elif load >= 60:
                base = QColor(theme.WARN)
            elif load >= 20:
                base = QColor(theme.ACCENT)
            else:
                base = QColor(theme.BORDER_LIGHT)
            fill = QColor(base)
            alpha = int(60 + 195 * min(1.0, load / 100))
            if load >= 85 and not self._blink:
                alpha = 60
            fill.setAlpha(alpha)
            p.setBrush(fill)
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawRoundedRect(rect, 5, 5)

            p.setPen(QColor(theme.TEXT) if load >= 40 else QColor(theme.TEXT_FAINT))
            p.setFont(QFont("Consolas", 9, QFont.Bold))
            label = self.labels[i] if i < len(self.labels) else "•"
            p.drawText(rect, Qt.AlignCenter, f"{label}\n{load:.0f}")
        p.end()
