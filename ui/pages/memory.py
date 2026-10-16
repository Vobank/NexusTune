"""Страница памяти: файл подкачки (виртуальная ОЗУ), очистка и RAM-диск."""
from __future__ import annotations

import psutil
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QComboBox,
    QGridLayout, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
)

from core import memory as mem
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, Sparkline,
)


class MemWorker(QThread):
    done = Signal(object)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            self.done.emit(self.func(*self.args))
        except Exception as exc:
            self.done.emit((False, str(exc)))


class PagefileWorker(QThread):
    """Читает настройки файла подкачки, не задерживая открытие страницы."""
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(mem.read_pagefile())
        except Exception:
            self.done.emit(None)


class MemoryPage(QWidget):
    """Управление виртуальной и физической памятью."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.worker = None
        # Чтение конфигурации файла подкачки идёт через WMI и занимает пару
        # секунд. Держать на них окно нельзя — читаем в фоне, а до ответа
        # показываем пустое состояние.
        self.state = mem.PagefileState(entries=[])
        self._pagefile_worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Память",
            "Файл подкачки — это и есть «виртуальная оперативка»: место на диске, "
            "куда Windows выгружает то, что не помещается в ОЗУ."))

        lay.addWidget(self._live_card())
        lay.addWidget(self._pagefile_card())
        lay.addWidget(self._clean_card())
        lay.addWidget(self._ramdisk_card())
        lay.addStretch()

        self.load_pagefile()

    # ------------------------------------------------------------ карточки

    def _live_card(self) -> Card:
        card = Card("Сейчас в системе")
        row = QHBoxLayout()
        row.setSpacing(30)

        m = self.ctx.machine
        self.kv_ram = KeyValue("Физическая память", f"{m.ram_gb:.0f} ГБ {m.ram_type}")
        self.kv_used = KeyValue("Занято", "—")
        self.kv_page = KeyValue("Файл подкачки", self.state.summary)
        self.kv_page_size = KeyValue("Выделено под подкачку",
                                     f"{self.state.current_mb} МБ")
        left = QVBoxLayout()
        left.addWidget(self.kv_ram)
        left.addWidget(self.kv_used)
        right = QVBoxLayout()
        right.addWidget(self.kv_page)
        right.addWidget(self.kv_page_size)
        row.addLayout(left, 1)
        row.addLayout(right, 1)
        card.add_layout(row)

        self.chart = Sparkline(120, theme.ACCENT_2)
        card.add(self.chart)
        return card

    def _pagefile_card(self) -> Card:
        m = self.ctx.machine
        free_gb = psutil.disk_usage("C:\\").free / (1024 ** 3)
        initial, maximum, why = mem.recommend_pagefile(m.ram_gb, free_gb)
        self._rec = (initial, maximum)

        card = Card("Виртуальная оперативная память (файл подкачки)")

        rec = Card(flat=True)
        head = QHBoxLayout()
        title = QLabel(f"Рекомендация для {m.ram_gb:.0f} ГБ ОЗУ: "
                       f"{initial} — {maximum} МБ")
        title.setStyleSheet(f"font-weight: 700; color: {theme.ACCENT}; font-size: 15px;")
        head.addWidget(title)
        head.addStretch()
        rec.add_layout(head)
        rec.add(dim_label(why))
        card.add(rec)

        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        form.addWidget(QLabel("Диск", objectName="Dim"), 0, 0)
        self.drive_box = QComboBox()
        for part in psutil.disk_partitions(all=False):
            if "fixed" in part.opts or part.fstype:
                self.drive_box.addItem(part.device.rstrip("\\"))
        if self.drive_box.count() == 0:
            self.drive_box.addItem("C:")
        form.addWidget(self.drive_box, 0, 1)

        form.addWidget(QLabel("Начальный размер, МБ", objectName="Dim"), 1, 0)
        self.initial_box = QSpinBox()
        self.initial_box.setRange(256, 131072)
        self.initial_box.setSingleStep(1024)
        self.initial_box.setValue(initial)
        form.addWidget(self.initial_box, 1, 1)

        form.addWidget(QLabel("Максимальный размер, МБ", objectName="Dim"), 2, 0)
        self.max_box = QSpinBox()
        self.max_box.setRange(256, 262144)
        self.max_box.setSingleStep(1024)
        self.max_box.setValue(maximum)
        form.addWidget(self.max_box, 2, 1)
        form.setColumnStretch(2, 1)
        card.add_layout(form)

        buttons = QHBoxLayout()
        apply_btn = primary_button("Применить размеры")
        apply_btn.clicked.connect(self._apply_pagefile)
        buttons.addWidget(apply_btn)

        rec_btn = ghost_button("Подставить рекомендованные")
        rec_btn.clicked.connect(self._fill_recommended)
        buttons.addWidget(rec_btn)

        auto_btn = ghost_button("Вернуть управление системе")
        auto_btn.clicked.connect(self._auto_pagefile)
        buttons.addWidget(auto_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.page_status = QLabel("")
        self.page_status.setObjectName("Dim")
        self.page_status.setWordWrap(True)
        card.add(self.page_status)

        card.add(dim_label(
            "Почему одинаковый начальный и максимальный размер лучше: Windows не тратит "
            "время на расширение файла в разгар игры, и файл не фрагментируется. "
            "Изменения вступают в силу после перезагрузки."))
        return card

    def _clean_card(self) -> Card:
        card = Card(
            "Очистка памяти",
            "Сбрасывает рабочие наборы процессов и очищает список ожидания — "
            "тот самый кэш, который занимает гигабайты и не отдаётся играм сразу.")

        row = QHBoxLayout()
        clean_btn = primary_button("🧹  Очистить память")
        clean_btn.clicked.connect(self._clean)
        row.addWidget(clean_btn)
        refresh_btn = ghost_button("Обновить список процессов")
        refresh_btn.clicked.connect(self._refresh_hogs)
        row.addWidget(refresh_btn)
        row.addStretch()
        if not is_admin():
            row.addWidget(Badge("список ожидания чистится только с правами админа",
                                theme.WARN))
        card.add_layout(row)

        self.clean_status = QLabel("")
        self.clean_status.setObjectName("Dim")
        self.clean_status.setWordWrap(True)
        card.add(self.clean_status)

        card.add(SectionTitle("Кто занимает больше всего"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Программа", "PID", "Память"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMaximumHeight(260)
        card.add(self.table)
        self._refresh_hogs()
        return card

    def _ramdisk_card(self) -> Card:
        available = mem.imdisk_available()
        card = Card(
            "RAM-диск",
            "Диск прямо в оперативной памяти: чтение в разы быстрее любого SSD. "
            "Полезен под кэш шейдеров, временные файлы и записи стримов.")

        if not available:
            card.add(dim_label(
                "Windows не умеет создавать RAM-диск сама — нужен драйвер ImDisk "
                "(бесплатный, открытый). Установи ImDisk Toolkit, и эта карточка оживёт."))
            note = QLabel("Важно: RAM-диск исчезает при выключении компьютера. "
                          "Не храни на нём ничего, что жалко потерять.")
            note.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
            note.setWordWrap(True)
            card.add(note)
            return card

        free_gb = psutil.virtual_memory().available / (1024 ** 3)
        suggest = int(min(free_gb * 0.25, 8) * 1024)

        form = QHBoxLayout()
        form.addWidget(QLabel("Буква", objectName="Dim"))
        self.letter_box = QComboBox()
        self.letter_box.addItems(mem.free_drive_letters() or ["R:"])
        form.addWidget(self.letter_box)

        form.addWidget(QLabel("Размер, МБ", objectName="Dim"))
        self.size_box = QSpinBox()
        self.size_box.setRange(64, int(free_gb * 1024))
        self.size_box.setSingleStep(512)
        self.size_box.setValue(max(suggest, 512))
        form.addWidget(self.size_box)
        form.addStretch()
        card.add_layout(form)

        buttons = QHBoxLayout()
        create_btn = primary_button("Создать RAM-диск")
        create_btn.clicked.connect(self._create_ramdisk)
        buttons.addWidget(create_btn)
        remove_btn = ghost_button("Отключить")
        remove_btn.clicked.connect(self._remove_ramdisk)
        buttons.addWidget(remove_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.ramdisk_status = QLabel("")
        self.ramdisk_status.setObjectName("Dim")
        self.ramdisk_status.setWordWrap(True)
        card.add(self.ramdisk_status)
        return card

    # ------------------------------------------------------------ действия

    def tick(self, reading: dict):
        percent = reading.get("ram_percent") or 0
        self.chart.push(percent)
        self.kv_used.set_value(
            f"{reading.get('ram_used_gb', 0)} ГБ ({percent:.0f}%)",
            theme.load_color(percent))

    def _fill_recommended(self):
        self.initial_box.setValue(self._rec[0])
        self.max_box.setValue(self._rec[1])

    def _apply_pagefile(self):
        initial = self.initial_box.value()
        maximum = self.max_box.value()
        if maximum < initial:
            self.page_status.setText("Максимальный размер не может быть меньше начального.")
            return
        drive = self.drive_box.currentText()
        self.page_status.setText("Применяю…")
        self.worker = MemWorker(mem.set_pagefile, drive, initial, maximum)
        self.worker.done.connect(self._on_pagefile_done)
        self.worker.start()

    def _auto_pagefile(self):
        self.page_status.setText("Возвращаю управление системе…")
        self.worker = MemWorker(mem.set_pagefile_automatic)
        self.worker.done.connect(self._on_pagefile_done)
        self.worker.start()

    def _on_pagefile_done(self, result):
        ok, msg = result
        self.page_status.setText(("✔  " if ok else "✖  ") + msg)
        self.load_pagefile()          # перечитываем в фоне

    def load_pagefile(self):
        """Читает настройки файла подкачки в фоне."""
        if self._pagefile_worker is not None and self._pagefile_worker.isRunning():
            return
        self.kv_page.set_value("читаю…")
        self._pagefile_worker = PagefileWorker()
        self._pagefile_worker.done.connect(self._on_pagefile_state)
        self._pagefile_worker.start()

    def _on_pagefile_state(self, state):
        if state is None:
            self.kv_page.set_value("прочитать не удалось")
            return
        self.state = state
        self.kv_page.set_value(state.summary)
        self.kv_page_size.set_value(f"{state.current_mb} МБ")

    def _clean(self):
        self.clean_status.setText("Чищу память…")
        self.worker = MemWorker(mem.clean_memory)
        self.worker.done.connect(self._on_cleaned)
        self.worker.start()

    def _on_cleaned(self, result):
        if not isinstance(result, dict):
            self.clean_status.setText("Не удалось очистить память.")
            return
        text = (f"Освобождено {result['freed_mb']} МБ. "
                f"Обработано процессов: {result['processes']}. "
                f"Занято памяти: {result['before_percent']:.0f}% → "
                f"{result['after_percent']:.0f}%.")
        if not result["standby_purged"]:
            text += (" Список ожидания не очищен — для этого нужны права администратора.")
        self.clean_status.setText(text)
        self._refresh_hogs()

    def _refresh_hogs(self):
        rows = mem.top_memory_hogs(10)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(row["name"]))
            self.table.setItem(i, 1, QTableWidgetItem(str(row["pid"])))
            item = QTableWidgetItem(f"{row['mb']} МБ")
            item.setForeground(Qt.white if row["mb"] < 1000 else Qt.red)
            self.table.setItem(i, 2, item)

    def _create_ramdisk(self):
        self.ramdisk_status.setText("Создаю RAM-диск…")
        self.worker = MemWorker(mem.create_ramdisk, self.letter_box.currentText(),
                                self.size_box.value())
        self.worker.done.connect(lambda r: self.ramdisk_status.setText(
            ("✔  " if r[0] else "✖  ") + r[1]))
        self.worker.start()

    def _remove_ramdisk(self):
        self.worker = MemWorker(mem.remove_ramdisk, self.letter_box.currentText())
        self.worker.done.connect(lambda r: self.ramdisk_status.setText(
            ("✔  " if r[0] else "✖  ") + r[1]))
        self.worker.start()
