"""Фирменный знак приложения.

Знак — кремневый скол с искрой. Кремень высекает искру ударом: программа
делает то же самое с железом. Рисуется кодом, без внешних картинок, поэтому
одинаково выглядит в окне, в трее, на заставке и в файле .ico.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import (
    QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

APP_TITLE = "КРЕМЕНЬ"
APP_SLUG = "Kremen"
TAGLINE = "центр управления железом"

# Цвета знака держим отдельно от темы: знак не должен меняться вместе с ней.
STONE_DARK = "#182030"
STONE_EDGE = "#3a4760"
SPARK_HOT = "#8ef0ff"
SPARK_CORE = "#00d0ff"
SPARK_TAIL = "#7b5cff"

# Скол камня в долях от стороны знака: пятигранник с острой верхушкой.
_SHARD = [
    (0.50, 0.03), (0.90, 0.30), (0.78, 0.80), (0.36, 0.97), (0.10, 0.52),
]
# Линия разлома, вдоль которой идёт свет.
_FRACTURE = [(0.50, 0.03), (0.44, 0.44), (0.66, 0.52), (0.36, 0.97)]


def _path(points, x: float, y: float, size: float, close: bool = True) -> QPainterPath:
    path = QPainterPath()
    for index, (px, py) in enumerate(points):
        point = QPointF(x + px * size, y + py * size)
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    if close:
        path.closeSubpath()
    return path


def draw_mark(painter: QPainter, x: float, y: float, size: float) -> None:
    """Рисует знак в квадрате (x, y, size, size)."""
    painter.setRenderHint(QPainter.Antialiasing)

    shard = _path(_SHARD, x, y, size)
    stone = QLinearGradient(QPointF(x, y), QPointF(x + size, y + size))
    stone.setColorAt(0.0, QColor(STONE_EDGE))
    stone.setColorAt(0.55, QColor(STONE_DARK))
    stone.setColorAt(1.0, QColor("#0d121c"))
    painter.fillPath(shard, stone)

    # Правая грань ловит свет — она светлее остального камня.
    facet = _path([(0.50, 0.03), (0.90, 0.30), (0.78, 0.80), (0.66, 0.52), (0.44, 0.44)],
                  x, y, size)
    lit = QLinearGradient(QPointF(x + size * 0.4, y), QPointF(x + size, y + size))
    lit.setColorAt(0.0, QColor(60, 78, 110, 210))
    lit.setColorAt(1.0, QColor(24, 32, 48, 120))
    painter.fillPath(facet, lit)

    painter.setPen(QPen(QColor(STONE_EDGE), max(1.0, size * 0.025)))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(shard)

    # Разлом светится: это и есть высеченная искра.
    glow = QLinearGradient(QPointF(x + size * 0.5, y), QPointF(x + size * 0.4, y + size))
    glow.setColorAt(0.0, QColor(SPARK_HOT))
    glow.setColorAt(0.5, QColor(SPARK_CORE))
    glow.setColorAt(1.0, QColor(SPARK_TAIL))
    painter.setPen(QPen(glow, max(1.2, size * 0.055), Qt.SolidLine, Qt.RoundCap,
                        Qt.RoundJoin))
    painter.drawPath(_path(_FRACTURE, x, y, size, close=False))

    # Искры, отлетающие от верхней грани.
    if size >= 28:
        painter.setPen(QPen(QColor(SPARK_HOT), max(1.0, size * 0.03), Qt.SolidLine,
                            Qt.RoundCap))
        for (x1, y1, x2, y2) in ((0.62, 0.10, 0.80, 0.02),
                                 (0.70, 0.20, 0.94, 0.14),
                                 (0.58, 0.05, 0.66, -0.04)):
            painter.drawLine(QPointF(x + x1 * size, y + y1 * size),
                             QPointF(x + x2 * size, y + y2 * size))


def mark_pixmap(size: int) -> QPixmap:
    """Знак в виде картинки с прозрачным фоном."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    inset = size * 0.06
    draw_mark(painter, inset, inset, size - inset * 2)
    painter.end()
    return pix


def app_icon() -> QIcon:
    """Иконка приложения для окна и значка у часов."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(mark_pixmap(size))
    return icon


def draw_wordmark(painter: QPainter, rect: QRectF, mark_size: float,
                  title_color: str, sub_color: str, subtitle: str = TAGLINE) -> None:
    """Знак и название рядом — для заставки."""
    painter.setRenderHint(QPainter.Antialiasing)
    top = rect.top() + (rect.height() - mark_size) / 2
    draw_mark(painter, rect.left(), top, mark_size)

    text_left = rect.left() + mark_size * 1.28
    text_rect = QRectF(text_left, rect.top(), rect.width() - mark_size * 1.28,
                       rect.height())

    font = QFont("Segoe UI", int(mark_size * 0.42), QFont.Bold)
    font.setLetterSpacing(QFont.AbsoluteSpacing, mark_size * 0.06)
    painter.setFont(font)
    painter.setPen(QColor(title_color))
    painter.drawText(text_rect.adjusted(0, 0, 0, -mark_size * 0.42),
                     Qt.AlignVCenter | Qt.AlignLeft, APP_TITLE)

    small = QFont("Segoe UI", max(7, int(mark_size * 0.15)))
    small.setLetterSpacing(QFont.AbsoluteSpacing, mark_size * 0.03)
    painter.setFont(small)
    painter.setPen(QColor(sub_color))
    painter.drawText(text_rect.adjusted(2, mark_size * 0.52, 0, 0),
                     Qt.AlignVCenter | Qt.AlignLeft, subtitle)
