"""Страница накопителей: здоровье по SMART и анализ занятого места."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QGridLayout,
)

from core import storage as storage_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)

_VERDICT_COLOR = {"ok": theme.OK, "warn": theme.WARN, "bad": theme.DANGER}


class DrivesWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(storage_core.read_drives())
        except Exception:
            self.done.emit([])


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(list)

    def __init__(self, analyzer, root: str, mode: str):
        super().__init__()
        self.analyzer = analyzer
        self.root = root
        self.mode = mode

    def run(self):
        try:
            if self.mode == "files":
                rows = self.analyzer.big_files(self.root, progress=self.progress.emit)
            else:
                rows = self.analyzer.top_folders(self.root, progress=self.progress.emit)
            self.done.emit(rows)
        except Exception:
            self.done.emit([])


class StoragePage(QWidget):
    """Здоровье дисков и анализ места."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.analyzer = storage_core.DiskAnalyzer()
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Накопители",
            "Здоровье дисков по данным SMART и ответ на вопрос "
            "«куда делось место»."))

        lay.addWidget(self._health_card())
        lay.addWidget(self._volumes_card())
        lay.addWidget(self._analyzer_card())
        lay.addStretch()

        self._load_drives()

    # ------------------------------------------------------------ здоровье

    def _health_card(self) -> Card:
        card = Card("Здоровье дисков",
                    "Те же счётчики, что показывают платные утилиты вроде "
                    "CrystalDiskInfo — ресурс записи, наработка и температура.")
        self.health_box = QVBoxLayout()
        self.health_box.setSpacing(10)
        card.add_layout(self.health_box)

        row = QHBoxLayout()
        refresh = ghost_button("Обновить")
        refresh.clicked.connect(self._load_drives)
        row.addWidget(refresh)
        row.addStretch()
        card.add_layout(row)
        return card

    def _load_drives(self):
        clear_layout(self.health_box)
        loading = QLabel("Читаю данные накопителей…")
        loading.setObjectName("Dim")
        self.health_box.addWidget(loading)

        worker = DrivesWorker()
        worker.done.connect(self._on_drives)
        self.workers.append(worker)
        worker.start()

    def _on_drives(self, drives: list):
        clear_layout(self.health_box)
        if not drives:
            self.health_box.addWidget(dim_label(
                "Windows не отдала данные о накопителях."))
            return

        for drive in drives:
            verdict, why = drive.verdict()
            colour = _VERDICT_COLOR.get(verdict, theme.TEXT_DIM)

            block = Card(flat=True)
            block.body.setSpacing(7)

            head = QHBoxLayout()
            title = QLabel(drive.name)
            title.setStyleSheet("font-size: 15px; font-weight: 700;")
            head.addWidget(title)
            head.addStretch()
            head.addWidget(Badge(f"{drive.media_type} · {drive.bus}", theme.ACCENT_2))
            head.addWidget(Badge(
                {"ok": "здоров", "warn": "внимание", "bad": "проблема"}[verdict], colour))
            block.add_layout(head)

            grid = QGridLayout()
            grid.setHorizontalSpacing(26)
            cells = [
                ("Объём", f"{drive.size_gb} ГБ", theme.TEXT),
                ("Температура",
                 f"{drive.temperature} °C" if drive.temperature else "нет данных",
                 theme.temp_color(drive.temperature) if drive.temperature else theme.TEXT_DIM),
                ("Наработка",
                 f"{drive.power_on_hours} ч ({drive.years_on} г)"
                 if drive.power_on_hours else "нет данных", theme.TEXT),
                ("Остаток ресурса", f"{drive.remaining_percent}%",
                 theme.OK if drive.remaining_percent > 50 else theme.WARN),
                ("Ошибки чтения", str(drive.read_errors),
                 theme.OK if not drive.read_errors else theme.DANGER),
                ("Ошибки записи", str(drive.write_errors),
                 theme.OK if not drive.write_errors else theme.DANGER),
            ]
            for index, (key, value, colour_value) in enumerate(cells):
                grid.addWidget(KeyValue(key, value, colour_value), index % 3, index // 3)
            block.add_layout(grid)

            note = QLabel(why)
            note.setObjectName("Faint")
            note.setWordWrap(True)
            block.add(note)
            self.health_box.addWidget(block)

    # ------------------------------------------------------------ разделы

    def _volumes_card(self) -> Card:
        card = Card("Разделы")
        for volume in storage_core.volumes():
            row = QHBoxLayout()
            name = QLabel(f"{volume['device']}  {volume['fstype']}")
            name.setStyleSheet("font-weight: 600;")
            name.setMinimumWidth(120)
            row.addWidget(name)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(volume["percent"]))
            bar.setTextVisible(False)
            colour = (theme.DANGER if volume["percent"] > 90 else
                      theme.WARN if volume["percent"] > 80 else theme.ACCENT)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {theme.SURFACE_2}; border: none; "
                f"border-radius: 5px; height: 10px; }} "
                f"QProgressBar::chunk {{ background: {colour}; border-radius: 5px; }}")
            row.addWidget(bar, 1)

            value = QLabel(f"{volume['free_gb']:.0f} ГБ свободно из {volume['total_gb']:.0f}")
            value.setStyleSheet(f"color: {colour}; font-weight: 600;")
            value.setAlignment(Qt.AlignRight)
            value.setMinimumWidth(220)
            row.addWidget(value)
            card.add_layout(row)
        return card

    # ------------------------------------------------------------ анализатор

    def _analyzer_card(self) -> Card:
        card = Card("Куда делось место",
                    "Обходит папки и показывает самые толстые. Ничего не удаляет — "
                    "только показывает, где искать.")

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Где искать:", objectName="Dim"))
        self.target_box = QComboBox()
        for name, path in storage_core.suggest_targets():
            self.target_box.addItem(name, path)
        row.addWidget(self.target_box, 1)

        self.folders_btn = primary_button("Толстые папки")
        self.folders_btn.clicked.connect(lambda: self._scan("folders"))
        row.addWidget(self.folders_btn)

        self.files_btn = ghost_button("Крупные файлы")
        self.files_btn.setToolTip("Файлы больше 500 МБ: забытые образы, архивы, видео.")
        self.files_btn.clicked.connect(lambda: self._scan("files"))
        row.addWidget(self.files_btn)

        self.cancel_btn = ghost_button("Стоп")
        self.cancel_btn.clicked.connect(self.analyzer.cancel)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.cancel_btn)
        card.add_layout(row)

        self.scan_bar = QProgressBar()
        self.scan_bar.setVisible(False)
        card.add(self.scan_bar)

        self.scan_status = QLabel("Обход диска занимает время — это нормально.")
        self.scan_status.setObjectName("Faint")
        self.scan_status.setWordWrap(True)
        card.add(self.scan_status)

        self.result_table = QTableWidget(0, 3)
        self.result_table.setHorizontalHeaderLabels(["Папка или файл", "Размер", "Файлов"])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.result_table.setMinimumHeight(320)
        self.result_table.itemDoubleClicked.connect(self._open_item)
        card.add(self.result_table)

        card.add(dim_label("Двойной щелчок открывает папку в проводнике."))
        return card

    def _scan(self, mode: str):
        root = self.target_box.currentData()
        if not root:
            return
        self.folders_btn.setEnabled(False)
        self.files_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.scan_bar.setVisible(True)
        self.scan_bar.setRange(0, 0)
        self.scan_status.setText(f"Обхожу {root}…")

        worker = ScanWorker(self.analyzer, root, mode)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_scanned)
        self.workers.append(worker)
        worker.start()

    def _on_progress(self, done: int, total: int, name: str):
        if total:
            self.scan_bar.setRange(0, total)
            self.scan_bar.setValue(done)
            self.scan_status.setText(f"[{done}/{total}] {name}")
        else:
            self.scan_status.setText(f"Проверено файлов: {done}…")

    def _on_scanned(self, rows: list):
        self.folders_btn.setEnabled(True)
        self.files_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.scan_bar.setVisible(False)

        self.result_table.setRowCount(len(rows))
        total_gb = 0.0
        for index, row in enumerate(rows):
            total_gb += row.gb
            name = QTableWidgetItem(row.name)
            name.setToolTip(row.path)
            name.setData(Qt.UserRole, row.path)
            self.result_table.setItem(index, 0, name)

            size = QTableWidgetItem(
                f"{row.gb:.2f} ГБ" if row.bytes >= 1024 ** 3 else f"{row.mb:.0f} МБ")
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            size.setForeground(QColor(
                theme.DANGER if row.gb > 20 else theme.WARN if row.gb > 5 else theme.ACCENT))
            self.result_table.setItem(index, 1, size)

            count = QTableWidgetItem(str(row.files))
            count.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.result_table.setItem(index, 2, count)

        self.scan_status.setText(
            f"Найдено записей: {len(rows)}, всего {total_gb:.1f} ГБ. "
            "Двойной щелчок открывает в проводнике."
            if rows else "Ничего не найдено или обход остановлен.")

    def _open_item(self, item):
        path = self.result_table.item(item.row(), 0).data(Qt.UserRole)
        if not path:
            return
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                os.startfile(os.path.dirname(path))
        except OSError as exc:
            self.scan_status.setText(f"Не удалось открыть: {exc}")

    def tick(self, reading: dict):
        pass
