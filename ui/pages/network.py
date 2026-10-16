"""Страница сети: качество канала, пинг до игровых серверов и советы."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QSpinBox, QAbstractItemView,
)
from PySide6.QtGui import QColor

from core import network as net
from core import netwatch
from core.context import AppContext
from core.util import is_admin, log

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, Sparkline, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)

_QUALITY_COLOR = {
    "отлично": theme.OK,
    "хорошо": theme.OK,
    "терпимо": theme.WARN,
    "нестабильно": theme.WARN,
    "высокая задержка": theme.DANGER,
    "потери пакетов": theme.DANGER,
    "не отвечает": theme.TEXT_FAINT,
}


class PingWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)

    def __init__(self, count: int):
        super().__init__()
        self.count = count

    def run(self):
        try:
            self.done.emit(net.ping_all(count=self.count, progress=self.progress.emit))
        except Exception:
            self.done.emit([])


class AdapterWorker(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(net.active_adapter())
        except Exception:
            self.done.emit(None)


class ActionWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            ok, message = self.func()
        except Exception as exc:
            ok, message = False, str(exc)
        self.done.emit(ok, message)


class NetworkPage(QWidget):
    """Проверка канала и сетевые действия."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.adapter = None
        self.results: list = []
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Сеть",
            "В онлайн-играх мешает не столько высокий пинг, сколько его разброс. "
            "Здесь видно и то, и другое, и потери пакетов."))

        lay.addWidget(self._adapter_card())
        lay.addWidget(self._ping_card())
        lay.addWidget(self._connections_card())
        lay.addWidget(self._advice_card())
        lay.addWidget(self._actions_card())
        lay.addStretch()

        self._load_adapter()

    # ------------------------------------------------------------ соединения

    def _connections_card(self) -> Card:
        """Кто и куда сейчас стучится из компьютера."""
        card = Card("Кто выходит в интернет",
                    "Диспетчер задач показывает общую скорость и всё. Здесь видно, "
                    "какая программа с каким сервером разговаривает прямо сейчас.")

        row = QHBoxLayout()
        row.setSpacing(14)
        self.speed_down = KeyValue("Приём", "—")
        self.speed_up = KeyValue("Отдача", "—")
        row.addWidget(self.speed_down, 1)
        row.addWidget(self.speed_up, 1)
        row.addStretch(2)
        refresh = ghost_button("Обновить список")
        refresh.clicked.connect(self.refresh_connections)
        row.addWidget(refresh)
        card.add_layout(row)

        self.conn_summary = QLabel("Читаю соединения…")
        self.conn_summary.setObjectName("Dim")
        self.conn_summary.setWordWrap(True)
        card.add(self.conn_summary)

        self.conn_body = QVBoxLayout()
        self.conn_body.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self.conn_body)
        card.add(holder)

        card.add(dim_label(
            "Скорость показана суммарная по адаптеру. Разбивки по программам "
            "Windows обычным приложениям не даёт — для этого нужен драйвер "
            "уровня ядра, и приложение не станет делать вид, что умеет это."))

        self._bandwidth = netwatch.Bandwidth()
        self._conn_tick = 0
        self.refresh_connections()
        return card

    def refresh_connections(self):
        try:
            groups = netwatch.read_connections()
        except Exception as exc:
            log(f"Соединения не прочитались: {exc}")
            return
        self.conn_summary.setText(netwatch.summary(groups))
        clear_layout(self.conn_body)

        for group in groups[:12]:
            block = QWidget()
            bl = QVBoxLayout(block)
            bl.setContentsMargins(12, 9, 12, 9)
            bl.setSpacing(2)
            block.setStyleSheet(
                f"background: {theme.SURFACE_2}; border: 1px solid {theme.BORDER}; "
                "border-radius: 10px;")

            top = QHBoxLayout()
            name = QLabel(group.process)
            name.setStyleSheet("font-weight: 600; font-size: 13px; "
                               "background: transparent;")
            top.addWidget(name)
            if group.known:
                hint = QLabel("— " + group.known)
                hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px; "
                                   "background: transparent;")
                top.addWidget(hint)
            top.addStretch()
            top.addWidget(Badge(f"{group.count} соединений", theme.ACCENT))
            bl.addLayout(top)

            detail = QLabel(f"{', '.join(group.services)}   ·   "
                            f"{', '.join(group.remotes[:3])}"
                            + (f"  и ещё {len(group.remotes) - 3}"
                               if len(group.remotes) > 3 else ""))
            detail.setObjectName("Faint")
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px; "
                                 "background: transparent;")
            bl.addWidget(detail)
            self.conn_body.addWidget(block)

    # ------------------------------------------------------------ карточки

    def _adapter_card(self) -> Card:
        card = Card("Подключение")
        self.kv_adapter = KeyValue("Адаптер", "читаю…")
        self.kv_speed = KeyValue("Скорость канала", "—", theme.ACCENT)
        self.kv_kind = KeyValue("Тип подключения", "—")
        self.kv_ip = KeyValue("Локальный адрес", "—")
        for widget in (self.kv_adapter, self.kv_speed, self.kv_kind, self.kv_ip):
            card.add(widget)
        return card

    def _ping_card(self) -> Card:
        card = Card("Проверка серверов",
                    "Пингуем игровые сервисы напрямую системной утилитой. "
                    "Epic, EA и PlayStation здесь не показаны: они намеренно "
                    "не отвечают на пинг, и это не значит, что игра не работает.")

        row = QHBoxLayout()
        self.ping_btn = primary_button("▶  Проверить связь")
        self.ping_btn.clicked.connect(self.run_ping)
        row.addWidget(self.ping_btn)

        row.addWidget(QLabel("Пакетов:", objectName="Dim"))
        self.count_box = QSpinBox()
        self.count_box.setRange(4, 50)
        self.count_box.setValue(10)
        row.addWidget(self.count_box)
        row.addStretch()

        self.summary_badge = Badge("не проверялось", theme.TEXT_FAINT)
        row.addWidget(self.summary_badge)
        card.add_layout(row)

        self.ping_bar = QProgressBar()
        self.ping_bar.setVisible(False)
        card.add(self.ping_bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Сервис", "Задержка", "Разброс", "Потери", "Оценка"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(260)
        card.add(self.table)

        self.detail = QLabel("")
        self.detail.setObjectName("Faint")
        self.detail.setWordWrap(True)
        card.add(self.detail)
        return card

    def _advice_card(self) -> Card:
        card = Card("Что с этим делать")
        self.advice_box = QVBoxLayout()
        card.add_layout(self.advice_box)
        hint = QLabel("Запусти проверку — советы появятся по её результатам.")
        hint.setObjectName("Dim")
        self.advice_box.addWidget(hint)
        return card

    def _actions_card(self) -> Card:
        card = Card("Сетевые действия")
        row = QHBoxLayout()
        row.setSpacing(10)

        dns_btn = ghost_button("Очистить кэш DNS")
        dns_btn.setToolTip("Помогает, когда сайты и игровые серверы не открываются, "
                           "хотя интернет работает.")
        dns_btn.clicked.connect(lambda: self._run_action(net.flush_dns))
        row.addWidget(dns_btn)

        reset_btn = ghost_button("Сбросить сетевой стек")
        reset_btn.setToolTip("Крайняя мера при странных сетевых сбоях. "
                             "Потребуется перезагрузка.")
        reset_btn.clicked.connect(lambda: self._run_action(net.reset_network_stack))
        reset_btn.setEnabled(is_admin())
        row.addWidget(reset_btn)
        row.addStretch()
        card.add_layout(row)

        self.action_status = QLabel("")
        self.action_status.setObjectName("Dim")
        self.action_status.setWordWrap(True)
        card.add(self.action_status)

        card.add(dim_label(
            "Твики «Отключить алгоритм Нейгла», «Снять резерв канала под QoS» и "
            "«Снять сетевой троттлинг» живут на странице оптимизации — они убирают "
            "лишние миллисекунды уже на стороне Windows."))
        return card

    # ------------------------------------------------------------ данные

    def _load_adapter(self):
        worker = AdapterWorker()
        worker.done.connect(self._on_adapter)
        self.workers.append(worker)
        worker.start()

    def _on_adapter(self, adapter):
        self.adapter = adapter
        if not adapter:
            self.kv_adapter.set_value("не найден", theme.WARN)
            return
        self.kv_adapter.set_value(adapter.name)
        self.kv_speed.set_value(adapter.speed or "—")
        self.kv_kind.set_value(
            "Wi-Fi — на кабеле пинг ровнее" if adapter.wifi else "Кабель Ethernet",
            theme.WARN if adapter.wifi else theme.OK)
        self.kv_ip.set_value(adapter.ipv4 or "—")

    def run_ping(self):
        self.ping_btn.setEnabled(False)
        self.ping_btn.setText("Проверяю…")
        self.ping_bar.setVisible(True)
        self.ping_bar.setRange(0, len(net.DEFAULT_TARGETS))
        worker = PingWorker(self.count_box.value())
        worker.progress.connect(lambda done, total: self.ping_bar.setValue(done))
        worker.done.connect(self._on_results)
        self.workers.append(worker)
        worker.start()

    def _on_results(self, results: list):
        self.results = results
        self.ping_btn.setEnabled(True)
        self.ping_btn.setText("▶  Проверить связь")
        self.ping_bar.setVisible(False)

        self.table.setRowCount(len(results))
        for index, item in enumerate(results):
            quality, why = item.quality
            colour = _QUALITY_COLOR.get(quality, theme.TEXT_DIM)

            self.table.setItem(index, 0, QTableWidgetItem(item.name))
            for column, text in enumerate((
                    f"{item.avg_ms:.0f} мс" if item.ok else "—",
                    f"{item.jitter_ms:.1f} мс" if item.ok else "—",
                    f"{item.loss_percent:.0f}%" if item.ok else "—"), start=1):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(index, column, cell)
            verdict = QTableWidgetItem(quality)
            verdict.setForeground(QColor(colour))
            verdict.setToolTip(why)
            self.table.setItem(index, 4, verdict)

        answered = [r for r in results if r.ok]
        if answered:
            average = sum(r.avg_ms for r in answered) / len(answered)
            jitter = sum(r.jitter_ms for r in answered) / len(answered)
            loss = sum(r.loss_percent for r in answered) / len(answered)
            colour = (theme.OK if average < 60 and jitter < 12 and loss < 1
                      else theme.WARN if average < 120 and loss < 5 else theme.DANGER)
            self.summary_badge.setText(
                f"средняя задержка {average:.0f} мс · разброс {jitter:.1f} мс")
            self.summary_badge.set_color(colour)
            self.detail.setText(
                f"Ответили {len(answered)} из {len(results)} сервисов. "
                "Разброс важнее средней задержки: ровные 60 мс играются лучше, "
                "чем скачущие от 20 до 90.")
        else:
            self.summary_badge.setText("сервисы не отвечают")
            self.summary_badge.set_color(theme.DANGER)

        clear_layout(self.advice_box)
        for tip in net.network_advice(self.adapter, results):
            label = QLabel("•  " + tip)
            label.setObjectName("Dim")
            label.setWordWrap(True)
            self.advice_box.addWidget(label)

    def _run_action(self, func):
        self.action_status.setText("Выполняю…")
        worker = ActionWorker(func)
        worker.done.connect(lambda ok, message: self.action_status.setText(
            ("✔  " if ok else "✖  ") + message))
        self.workers.append(worker)
        worker.start()

    def tick(self, reading: dict):
        """Живая скорость канала обновляется вместе с датчиками."""
        speed = None
        try:
            speed = self._bandwidth.read()
        except Exception:
            return
        if speed is None:
            return
        down, up = speed
        self.speed_down.set_value(f"{down:.2f} Мбит/с",
                                  theme.ACCENT if down > 0.1 else theme.TEXT_DIM)
        self.speed_up.set_value(f"{up:.2f} Мбит/с",
                                theme.ACCENT_2 if up > 0.1 else theme.TEXT_DIM)

        # список соединений перечитываем раз в ~15 секунд, чаще незачем
        self._conn_tick += 1
        if self._conn_tick >= 12:
            self._conn_tick = 0
            self.refresh_connections()
