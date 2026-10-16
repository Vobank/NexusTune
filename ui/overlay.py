"""Игровой оверлей: живые кадры, задержка и температуры поверх игры.

Оверлей показывает то, ради чего в него смотрят в бою: сколько кадров прямо
сейчас, насколько ровно они идут (нижний 1%), какова задержка до сервера,
как греется и грузится железо и сколько компьютер тянет из розетки.

Кадры берутся из RivaTuner (если он запущен). Пинг меряется раз в несколько
секунд в отдельном потоке, чтобы не дёргать интерфейс. Остальное приходит
из общего опроса датчиков.
"""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QPoint, QThread, Signal, QRectF, QPointF
from PySide6.QtGui import QMouseEvent, QColor, QPainter, QPen, QFont, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGridLayout,
)

from core.fps import FpsReader
from core.network import ping

from . import theme
from .brand import mark_pixmap


class PingWorker(QThread):
    """Меряет задержку до игрового сервера, не мешая интерфейсу."""
    measured = Signal(float, float, float)      # средняя, джиттер, потери

    def __init__(self, host: str = "1.1.1.1"):
        super().__init__()
        self.host = host
        self._running = True

    def run(self):
        while self._running:
            try:
                r = ping(self.host, count=4, timeout_ms=1000)
                self.measured.emit(r.avg_ms, r.jitter_ms, r.loss_percent)
            except Exception:
                pass
            for _ in range(30):                 # ~3 секунды с быстрым выходом
                if not self._running:
                    break
                self.msleep(100)

    def stop(self):
        self._running = False
        self.wait(2000)


class MiniGraph(QWidget):
    """Узкая полоса истории кадров под главным числом."""

    def __init__(self, capacity: int = 120, parent=None):
        super().__init__(parent)
        self.points = deque(maxlen=capacity)
        self.setFixedHeight(30)
        self.target = 60.0

    def push(self, value: float, target: float):
        self.target = target or 60.0
        self.points.append(float(value or 0))
        self.update()

    def paintEvent(self, event):
        if len(self.points) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ceiling = max(self.target * 1.5, max(self.points), 1)
        step = w / max(len(self.points) - 1, 1)

        # линия цели (частота монитора) — тонкий ориентир
        ty = h - 2 - (self.target / ceiling) * (h - 4)
        p.setPen(QPen(QColor(theme.TEXT_FAINT), 1, Qt.DashLine))
        p.drawLine(0, int(ty), w, int(ty))

        path_pts = []
        for i, value in enumerate(self.points):
            x = i * step
            y = h - 2 - (min(value, ceiling) / ceiling) * (h - 4)
            path_pts.append(QPointF(x, y))
        p.setPen(QPen(QColor(theme.ACCENT), 1.6))
        p.drawPolyline(path_pts)
        p.end()


class Stat(QWidget):
    """Компактная ячейка: подпись сверху, значение снизу."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.cap = QLabel(caption)
        self.cap.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                               "font-weight: 700; letter-spacing: 1px;")
        self.val = QLabel("—")
        self.val.setStyleSheet("font-size: 15px; font-weight: 800;")
        lay.addWidget(self.cap)
        lay.addWidget(self.val)

    def set(self, text: str, color: str = theme.TEXT):
        self.val.setText(text)
        self.val.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: 800;")


class Overlay(QWidget):
    """Полупрозрачная панель поверх игры. Перетаскивается за любое место.

    Поверх игр в оконном и безрамочном режиме рисуется как надо. В настоящем
    полноэкранном режиме Windows не даёт рисовать чужие окна поверх — это
    ограничение системы; переключи игру в «оконный без рамки».
    """

    def __init__(self, parent=None, ping_host: str = "1.1.1.1"):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_from: QPoint | None = None
        self._compact = False

        self._fps = FpsReader()
        self._fps_samples: deque = deque(maxlen=600)
        self._target = 60.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.frame = QWidget()
        self.frame.setObjectName("OverlayFrame")
        self.frame.setStyleSheet(
            f"#OverlayFrame {{ background: {theme.rgba(theme.BG, 232)}; "
            f"border: 1px solid {theme.BORDER_LIGHT}; border-radius: 14px; }}")
        root.addWidget(self.frame)

        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(14, 11, 14, 12)
        lay.setSpacing(8)

        lay.addLayout(self._build_head())
        lay.addLayout(self._build_fps_block())
        self.graph = MiniGraph()
        lay.addWidget(self.graph)
        lay.addWidget(self._divider())
        lay.addLayout(self._build_grid())

        self.hint = QPushButton("⚠ RivaTuner не запущен — нажми, чтобы запустить")
        self.hint.setCursor(Qt.PointingHandCursor)
        self.hint.setStyleSheet(
            f"QPushButton {{ color: {theme.WARN}; background: transparent; "
            "border: none; font-size: 10px; text-align: left; padding: 0; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}")
        self.hint.clicked.connect(self._launch_rtss)
        lay.addWidget(self.hint)
        self._rtss_recheck = 0

        self.setFixedWidth(248)

        self.ping_worker = PingWorker(ping_host)
        self.ping_worker.measured.connect(self._on_ping)
        self.ping_worker.start()

    # ------------------------------------------------------------ построение

    def _build_head(self) -> QHBoxLayout:
        head = QHBoxLayout()
        head.setSpacing(7)
        mark = QLabel()
        mark.setPixmap(mark_pixmap(16))
        mark.setFixedSize(16, 16)
        head.addWidget(mark)
        title = QLabel("КРЕМЕНЬ")
        title.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px; "
                            "font-weight: 800; letter-spacing: 2px;")
        head.addWidget(title)
        head.addStretch()

        self.compact_btn = QPushButton("▾")
        self.close_btn = QPushButton("✕")
        for b in (self.compact_btn, self.close_btn):
            b.setFixedSize(18, 18)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                f"background: transparent; border: none; color: {theme.TEXT_FAINT}; "
                "font-size: 11px; font-weight: 700;")
            head.addWidget(b)
        self.compact_btn.clicked.connect(self._toggle_compact)
        self.close_btn.clicked.connect(self.hide)
        return head

    def _build_fps_block(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.fps_value = QLabel("—")
        self.fps_value.setStyleSheet(f"color: {theme.TEXT}; font-size: 44px; "
                                     "font-weight: 900;")
        row.addWidget(self.fps_value)

        side = QVBoxLayout()
        side.setSpacing(0)
        fps_unit = QLabel("FPS")
        fps_unit.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px; "
                               "font-weight: 800; letter-spacing: 2px;")
        side.addWidget(fps_unit)
        self.fps_low = QLabel("1% —")
        self.fps_low.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px; "
                                   "font-weight: 700;")
        side.addWidget(self.fps_low)
        self.frametime = QLabel("— мс")
        self.frametime.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        side.addWidget(self.frametime)
        side.addStretch()
        row.addLayout(side)
        row.addStretch()
        return row

    def _build_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        self.stats: dict[str, Stat] = {}
        cells = [
            ("gpu", "GPU"), ("gpu_t", "GPU °C"), ("gpu_w", "GPU ВТ"),
            ("cpu", "CPU"), ("cpu_t", "CPU °C"), ("ram", "ОЗУ"),
            ("ping", "ПИНГ"), ("jitter", "РАЗБРОС"), ("wall", "ВСЕГО ВТ"),
        ]
        for index, (key, caption) in enumerate(cells):
            stat = Stat(caption)
            self.stats[key] = stat
            grid.addWidget(stat, index // 3, index % 3)
        return grid

    def _divider(self) -> QWidget:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        return line

    # ------------------------------------------------------------ данные

    def tick(self, reading: dict):
        self._poll_fps()

        cpu = reading.get("cpu_load") or 0
        self.stats["cpu"].set(f"{cpu:.0f}%", theme.load_color(cpu))
        cpu_t = reading.get("cpu_temp")
        self.stats["cpu_t"].set("—" if cpu_t is None else f"{cpu_t}°",
                                theme.temp_color(cpu_t or 0))

        gpu = reading.get("gpu_load")
        self.stats["gpu"].set("—" if gpu is None else f"{gpu:.0f}%",
                              theme.load_color(gpu or 0))
        gpu_t = reading.get("gpu_temp")
        self.stats["gpu_t"].set("—" if gpu_t is None else f"{gpu_t}°",
                                theme.temp_color(gpu_t or 0))
        gpu_w = reading.get("gpu_power")
        self.stats["gpu_w"].set("—" if gpu_w is None else f"{gpu_w:.0f}", theme.ACCENT)

        ram = reading.get("ram_percent") or 0
        self.stats["ram"].set(f"{reading.get('ram_used_gb', 0):.0f}Г",
                              theme.load_color(ram))
        wall = reading.get("wall_w")
        self.stats["wall"].set("—" if wall is None else f"{wall:.0f}", theme.ACCENT_2)

    def _launch_rtss(self):
        from core.fps import launch_rtss
        ok, msg = launch_rtss()
        self.hint.setText(("✓ " if ok else "⚠ ") + msg[:60])

    def _poll_fps(self):
        # состояние RTSS перепроверяем не каждый кадр, а раз в пару секунд
        self._rtss_recheck += 1
        if self._rtss_recheck >= 4:
            self._rtss_recheck = 0
            self._fps._checked = False
        if not self._fps._checked:
            self._fps.check()

        stats = self._fps.read_active()
        if not stats or stats.fps <= 0:
            self.fps_value.setText("—")
            self.hint.setVisible(True)
            from core.fps import rtss_installed, rtss_running
            if not rtss_installed():
                self.hint.setText("RivaTuner не установлен — идёт с MSI Afterburner")
            elif not rtss_running():
                self.hint.setText("⚠ RivaTuner не запущен — нажми, чтобы запустить")
            else:
                self.hint.setText("RivaTuner на связи — FPS появится, когда зайдёшь в игру")
            return

        self.hint.setVisible(False)
        self._fps_samples.append(stats.fps)
        color = theme.fps_color(int(stats.fps), int(self._target))
        self.fps_value.setText(f"{stats.fps:.0f}")
        self.fps_value.setStyleSheet(f"color: {color}; font-size: 44px; "
                                     "font-weight: 900;")
        if stats.frame_time_ms:
            self.frametime.setText(f"{stats.frame_time_ms:.1f} мс")

        low = self._one_percent_low()
        self.fps_low.setText(f"1% {low:.0f}" if low else "1% —")
        if low:
            self.fps_low.setStyleSheet(
                f"color: {theme.fps_color(int(low), int(self._target))}; "
                "font-size: 12px; font-weight: 700;")
        self.graph.push(stats.fps, self._target)

    def _one_percent_low(self) -> float:
        """Нижний 1% кадров за последние сотни замеров."""
        if len(self._fps_samples) < 20:
            return 0.0
        ordered = sorted(self._fps_samples)
        index = max(0, int(len(ordered) * 0.01))
        return round(ordered[index], 0)

    def _on_ping(self, avg: float, jitter: float, loss: float):
        if loss >= 100 or avg <= 0:
            self.stats["ping"].set("нет", theme.DANGER)
            self.stats["jitter"].set("—")
            return
        ping_color = (theme.OK if avg < 40 else theme.WARN if avg < 90
                      else theme.DANGER)
        suffix = "" if loss == 0 else f" !{loss:.0f}%"
        self.stats["ping"].set(f"{avg:.0f}{suffix}", ping_color)
        jit_color = theme.OK if jitter < 8 else theme.WARN if jitter < 20 else theme.DANGER
        self.stats["jitter"].set(f"{jitter:.0f}", jit_color)

    def set_target(self, hz: float):
        self._target = hz or 60.0

    # ------------------------------------------------------------ вид

    def _toggle_compact(self):
        """Свернуть до кадров и задержки — для тех, кому мешает всё остальное."""
        self._compact = not self._compact
        self.graph.setVisible(not self._compact)
        for key, stat in self.stats.items():
            stat.setVisible(not self._compact or key in ("gpu", "gpu_t", "ping"))
        self.compact_btn.setText("▸" if self._compact else "▾")
        self.adjustSize()
        self.setFixedWidth(248)

    def close(self):
        try:
            self.ping_worker.stop()
        except Exception:
            pass
        super().close()

    # перетаскивание за любое место
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_from and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_from = None
