"""Страница процессов: приоритеты, привязка к ядрам, правила и умный баланс."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QCheckBox, QAbstractItemView, QSizePolicy,
)

from core.cpuinfo import affinity_presets, mask_to_text
from core.context import AppContext
from core.processes import (
    PRIORITIES, PRIORITY_NAMES, ProcessManager, ProcessRule, SmartBalancer,
)
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)


class CoreMap(QWidget):
    """Наглядная карта ядер процессора."""

    def __init__(self, manager: ProcessManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.loads: list[float] = []
        # первый вызов возвращает нагрузку с момента загрузки Windows — прогреваем
        try:
            import psutil
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_loads(self, loads: list[float]):
        self.loads = loads
        self.update()

    def paintEvent(self, event):
        topology = self.manager.topology
        if not topology.cores:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        best = max(c.efficiency for c in topology.cores)
        count = topology.logical_count or 1
        gap = 4
        width = max((self.width() - gap * (count - 1)) / count, 6)

        for thread in range(count):
            core = next((c for c in topology.cores if thread in c.threads), None)
            fast = core is not None and core.efficiency == best
            load = self.loads[thread] if thread < len(self.loads) else 0.0

            x = thread * (width + gap)
            base = QColor(theme.SURFACE_2)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(base))
            painter.drawRoundedRect(x, 4, width, 42, 4, 4)

            filled = max(2.0, 42 * min(load, 100) / 100.0)
            colour = QColor(theme.ACCENT if fast else theme.ACCENT_2)
            if load >= 85:
                colour = QColor(theme.DANGER)
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(x, 4 + (42 - filled), width, filled, 4, 4)

            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.drawText(int(x), 50, int(width), 14, Qt.AlignCenter, str(thread))
            painter.setPen(QColor(theme.ACCENT if fast else theme.ACCENT_2))
            painter.drawText(int(x), 62, int(width), 12, Qt.AlignCenter,
                             "P" if fast else "E")
        painter.end()


class ListWorker(QThread):
    done = Signal(list)

    def __init__(self, manager: ProcessManager):
        super().__init__()
        self.manager = manager

    def run(self):
        try:
            self.done.emit(self.manager.list_processes(70))
        except Exception:
            self.done.emit([])


class ActionWorker(QThread):
    done = Signal(object)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            self.done.emit(self.func(*self.args))
        except Exception as exc:
            self.done.emit((0, str(exc)))


class ProcessesPage(QWidget):
    """Управление процессами и ядрами."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.manager = ProcessManager(ctx)
        self.balancer = SmartBalancer(self.manager, ctx)
        ctx.balancer = self.balancer
        self.rows = []
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Процессы и ядра",
            "Windows раскидывает программы по ядрам сама и иногда ошибается — "
            "отдаёт игре медленные ядра, а браузеру быстрые. Здесь это чинится "
            "и запоминается навсегда."))

        lay.addWidget(self._cores_card())
        lay.addWidget(self._balance_card())
        lay.addWidget(self._table_card())
        lay.addWidget(self._rules_card())
        lay.addStretch()

        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_cores)
        self.timer.start(2000)

        if bool(self.ctx.config.get("smart_balance")):
            self.balancer.start()

    # ------------------------------------------------------------ карточки

    def _cores_card(self) -> Card:
        topology = self.manager.topology
        card = Card("Карта ядер", topology.describe())

        self.core_map = CoreMap(self.manager)
        card.add(self.core_map)

        if topology.hybrid:
            legend = QHBoxLayout()
            legend.setSpacing(8)
            legend.addWidget(Badge(
                f"P — производительные, {topology.performance_threads} "
                + topology._plural(topology.performance_threads,
                                   "поток", "потока", "потоков"), theme.ACCENT))
            legend.addWidget(Badge(
                f"E — энергоэффективные, {topology.efficient_threads} "
                + topology._plural(topology.efficient_threads,
                                   "поток", "потока", "потоков"), theme.ACCENT_2))
            legend.addStretch()
            card.add_layout(legend)

        row = QHBoxLayout()
        row.setSpacing(10)
        to_e = primary_button("Согнать фон на E-ядра")
        to_e.setToolTip("Браузер, музыка и мессенджеры уходят на медленные ядра, "
                        "быстрые остаются игре.")
        to_e.clicked.connect(self._background_to_e)
        to_e.setEnabled(topology.hybrid)
        row.addWidget(to_e)

        release = ghost_button("Снять все привязки")
        release.clicked.connect(self._release_cores)
        row.addWidget(release)

        apply_rules = ghost_button("Применить мои правила")
        apply_rules.clicked.connect(self._apply_rules)
        row.addWidget(apply_rules)
        row.addStretch()
        if not is_admin():
            row.addWidget(Badge("часть процессов недоступна без прав админа", theme.WARN))
        card.add_layout(row)

        self.core_status = QLabel("")
        self.core_status.setObjectName("Dim")
        self.core_status.setWordWrap(True)
        card.add(self.core_status)
        return card

    def _balance_card(self) -> Card:
        card = Card(
            "Умный баланс",
            "Пока идёт игра, приложение само придерживает прожорливый фон: "
            "понижает ему приоритет и уводит на медленные ядра. Программы не "
            "закрываются, а после выхода из игры всё возвращается обратно.")

        self.balance_box = QCheckBox("Включить умный баланс во время игры")
        self.balance_box.setChecked(bool(self.ctx.config.get("smart_balance", False)))
        self.balance_box.toggled.connect(self._toggle_balance)
        card.add(self.balance_box)

        self.balance_status = QLabel("")
        self.balance_status.setObjectName("Faint")
        self.balance_status.setWordWrap(True)
        card.add(self.balance_status)
        self._refresh_balance_status()
        return card

    def _table_card(self) -> Card:
        card = Card("Запущенные программы",
                    "Отсортированы по нагрузке. Выбери строку, чтобы задать "
                    "приоритет и ядра.")

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Программа", "ЦП", "Память", "Приоритет", "Ядра"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(330)
        self.table.itemSelectionChanged.connect(self._on_selection)
        card.add(self.table)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Приоритет:", objectName="Dim"))
        self.priority_box = QComboBox()
        for name, value, why in PRIORITIES:
            self.priority_box.addItem(name, value)
            self.priority_box.setItemData(self.priority_box.count() - 1, why, Qt.ToolTipRole)
        self.priority_box.setCurrentIndex(2)
        controls.addWidget(self.priority_box)

        controls.addWidget(QLabel("Ядра:", objectName="Dim"))
        self.affinity_box = QComboBox()
        for name, why, mask in affinity_presets(self.manager.topology):
            self.affinity_box.addItem(name, mask)
            self.affinity_box.setItemData(self.affinity_box.count() - 1, why, Qt.ToolTipRole)
        controls.addWidget(self.affinity_box)

        apply_btn = primary_button("Применить")
        apply_btn.clicked.connect(self._apply_to_selected)
        controls.addWidget(apply_btn)

        remember = ghost_button("Запомнить навсегда")
        remember.setToolTip("Правило будет применяться при каждом запуске программы.")
        remember.clicked.connect(self._remember_selected)
        controls.addWidget(remember)

        refresh = ghost_button("Обновить")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        controls.addStretch()
        card.add_layout(controls)

        self.table_status = QLabel("Выбери программу в списке.")
        self.table_status.setObjectName("Faint")
        self.table_status.setWordWrap(True)
        card.add(self.table_status)
        return card

    def _rules_card(self) -> Card:
        card = Card("Мои правила",
                    "Правила применяются автоматически: программа запускается — "
                    "настройки на месте.")
        self.rules_box = QVBoxLayout()
        card.add_layout(self.rules_box)
        self._refresh_rules()
        return card

    # ------------------------------------------------------------ данные

    def refresh(self):
        worker = ListWorker(self.manager)
        worker.done.connect(self._fill_table)
        self.workers.append(worker)
        worker.start()

    def _fill_table(self, rows: list):
        self.rows = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            name = QTableWidgetItem(row.name + ("  🔒" if row.protected else "")
                                    + ("  ★" if row.has_rule else ""))
            if row.protected:
                name.setForeground(QColor(theme.TEXT_FAINT))
            self.table.setItem(index, 0, name)

            cpu = QTableWidgetItem(f"{row.cpu:.1f}%")
            cpu.setForeground(QColor(theme.DANGER if row.cpu > 25 else
                                     theme.WARN if row.cpu > 8 else theme.TEXT_DIM))
            self.table.setItem(index, 1, cpu)

            self.table.setItem(index, 2, QTableWidgetItem(f"{row.memory_mb} МБ"))
            self.table.setItem(index, 3, QTableWidgetItem(row.priority_name))
            self.table.setItem(index, 4, QTableWidgetItem(
                mask_to_text(row.affinity, self.manager.logical)))

    def _refresh_cores(self):
        try:
            import psutil
            self.core_map.set_loads(psutil.cpu_percent(interval=None, percpu=True))
        except Exception:
            pass
        self._refresh_balance_status()

    def _refresh_balance_status(self):
        if not self.balancer.running:
            self.balance_status.setText("Выключен.")
            return
        watcher = getattr(self.ctx, "watcher", None)
        if watcher and watcher.current:
            self.balance_status.setText(
                f"Игра «{watcher.current.game}» в работе. "
                f"Придержано процессов: {self.balancer.held_count}. "
                + (self.balancer.last_action or ""))
        else:
            self.balance_status.setText(
                "Включён и ждёт запуска игры. Пока игры нет, ничего не трогается.")

    def _refresh_rules(self):
        clear_layout(self.rules_box)
        rules = self.manager.load_rules()
        if not rules:
            hint = QLabel("Правил пока нет. Выбери программу выше и нажми "
                          "«Запомнить навсегда».")
            hint.setObjectName("Dim")
            hint.setWordWrap(True)
            self.rules_box.addWidget(hint)
            return

        for rule in rules:
            row = QHBoxLayout()
            title = QLabel(rule.process)
            title.setStyleSheet("font-weight: 600;")
            row.addWidget(title, 1)
            if rule.priority is not None:
                row.addWidget(Badge(PRIORITY_NAMES.get(rule.priority, "?"), theme.ACCENT))
            if rule.affinity is not None:
                row.addWidget(Badge(
                    mask_to_text(rule.affinity, self.manager.logical), theme.ACCENT_2))
            delete = ghost_button("Убрать")
            delete.clicked.connect(lambda _c=False, r=rule: self._delete_rule(r))
            row.addWidget(delete)
            self.rules_box.addLayout(row)

    # ------------------------------------------------------------ действия

    def _selected_row(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def _on_selection(self):
        row = self._selected_row()
        if not row:
            return
        if row.protected:
            self.table_status.setText(
                f"{row.name} — системный процесс. Такие приложение не трогает: "
                "от них зависят ввод, звук и рабочий стол.")
            return
        self.table_status.setText(
            f"{row.name}: сейчас приоритет «{row.priority_name}», "
            f"{mask_to_text(row.affinity, self.manager.logical)}.")

    def _apply_to_selected(self):
        row = self._selected_row()
        if not row:
            self.table_status.setText("Сначала выбери программу в списке.")
            return
        priority = self.priority_box.currentData()
        mask = self.affinity_box.currentData()
        ok_priority, message = self.manager.set_priority(row.pid, priority)
        ok_affinity, message2 = self.manager.set_affinity(row.pid, mask)
        self.table_status.setText(message if not ok_priority else message2)
        self.refresh()

    def _remember_selected(self):
        row = self._selected_row()
        if not row:
            self.table_status.setText("Сначала выбери программу в списке.")
            return
        if row.protected:
            self.table_status.setText("Для системных процессов правила не создаются.")
            return
        rule = ProcessRule(
            process=row.name,
            priority=self.priority_box.currentData(),
            affinity=self.affinity_box.currentData(),
        )
        self.manager.add_rule(rule)
        self.manager.apply_to_name(rule.process, rule.priority, rule.affinity)
        self._refresh_rules()
        self.refresh()
        self.table_status.setText(
            f"Правило для {row.name} сохранено — оно применится и при следующем запуске.")

    def _delete_rule(self, rule: ProcessRule):
        self.manager.remove_rule(rule.process)
        self._refresh_rules()
        self.refresh()

    def _background_to_e(self):
        worker = ActionWorker(self.manager.background_to_efficient_cores)
        worker.done.connect(lambda r: (self.core_status.setText(r[1]), self.refresh()))
        self.workers.append(worker)
        worker.start()

    def _release_cores(self):
        worker = ActionWorker(self.manager.release_all_cores)
        worker.done.connect(lambda r: (self.core_status.setText(r[1]), self.refresh()))
        self.workers.append(worker)
        worker.start()

    def _apply_rules(self):
        worker = ActionWorker(self.manager.apply_all_rules)
        worker.done.connect(lambda count: self.core_status.setText(
            f"Правила применены к {count} процессам."))
        self.workers.append(worker)
        worker.start()

    def _toggle_balance(self, checked: bool):
        self.ctx.config["smart_balance"] = checked
        self.ctx.save()
        if checked:
            self.balancer.start()
        else:
            self.balancer.stop()
        self._refresh_balance_status()

    def tick(self, reading: dict):
        pass
