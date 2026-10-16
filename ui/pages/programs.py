"""Страница «Программы»: список установленного и удаление в один клик.

Умная замена «Программам и компонентам»: видно размер, дату установки и
издателя, всё сортируется, лишнее удаляется прямо отсюда. Драйверы и среды
выполнения помечены и защищены от случайного удаления.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QCheckBox,
    QMessageBox,
)

from core import uninstall as uninstall_core
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, page_header, scroll_page, primary_button, ghost_button, dim_label,
)


class ListWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(uninstall_core.list_programs())
        except Exception:
            self.done.emit([])


class UninstallWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, program, silent: bool):
        super().__init__()
        self.program = program
        self.silent = silent

    def run(self):
        try:
            ok, msg = uninstall_core.uninstall(self.program, self.silent)
            self.done.emit(ok, msg)
        except Exception as exc:
            self.done.emit(False, str(exc))


class ProgramsPage(QWidget):
    """Список установленных программ и удаление."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.programs: list = []
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Программы",
            "Всё, что установлено на компьютере — с размером и датой. "
            "Лишнее удаляется прямо отсюда, драйверы и системные пакеты защищены."))

        lay.addWidget(self._controls_card())
        lay.addWidget(self._table_card())
        lay.addStretch()

        self._reload()

    # ------------------------------------------------------------ управление

    def _controls_card(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)

        self.summary = QLabel("Читаю список…")
        self.summary.setObjectName("Dim")
        row.addWidget(self.summary)
        row.addStretch()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или издателю…")
        self.search.setFixedWidth(260)
        self.search.textChanged.connect(self._apply_filter)
        row.addWidget(self.search)

        self.sort = QComboBox()
        self.sort.addItems(["По размеру", "По названию", "По дате"])
        self.sort.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.sort)

        self.hide_system = QCheckBox("Скрыть системные")
        self.hide_system.setChecked(True)
        self.hide_system.toggled.connect(self._apply_filter)
        row.addWidget(self.hide_system)

        refresh = ghost_button("Обновить")
        refresh.clicked.connect(self._reload)
        row.addWidget(refresh)
        card.add_layout(row)

        if not is_admin():
            card.add(dim_label(
                "Часть деинсталляторов попросит права администратора отдельным "
                "окном — это нормально. Для тихого удаления без вопросов запусти "
                "приложение от имени администратора."))
        return card

    def _table_card(self) -> Card:
        card = Card()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Программа", "Версия", "Размер", "Установлена", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(520)
        card.add(self.table)
        return card

    # ------------------------------------------------------------ данные

    def _reload(self):
        self.summary.setText("Читаю список…")
        worker = ListWorker()
        worker.done.connect(self._on_listed)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_listed(self, programs: list):
        self.programs = programs
        s = uninstall_core.summary(programs)
        self.summary.setText(
            f"Всего {s['total']} · обычных {s['regular']} · "
            f"системных {s['system']} · вместе занимают {s['size_text']}")
        self._apply_filter()

    def _apply_filter(self):
        text = self.search.text().strip().lower()
        rows = list(self.programs)
        if self.hide_system.isChecked():
            rows = [p for p in rows if not p.system_like]
        if text:
            rows = [p for p in rows
                    if text in p.name.lower() or text in p.publisher.lower()]

        mode = self.sort.currentIndex()
        if mode == 0:
            rows.sort(key=lambda p: p.size_bytes, reverse=True)
        elif mode == 1:
            rows.sort(key=lambda p: p.name.lower())
        else:
            rows.sort(key=lambda p: p.install_date or "", reverse=True)

        self._fill(rows)

    def _fill(self, rows: list):
        self.table.setRowCount(0)
        for program in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)

            name_cell = QWidget()
            nl = QVBoxLayout(name_cell)
            nl.setContentsMargins(6, 6, 6, 6)
            nl.setSpacing(2)
            top = QHBoxLayout()
            top.setSpacing(8)
            title = QLabel(program.name)
            title.setStyleSheet("font-weight: 600; font-size: 13px;")
            top.addWidget(title)
            if program.system_like:
                top.addWidget(Badge("система", theme.TEXT_FAINT))
            top.addStretch()
            nl.addLayout(top)
            if program.publisher:
                pub = QLabel(program.publisher)
                pub.setObjectName("Faint")
                nl.addWidget(pub)
            self.table.setCellWidget(r, 0, name_cell)

            self._text_cell(r, 1, program.version or "—", theme.TEXT_DIM)
            self._text_cell(r, 2, program.size_text, theme.TEXT)
            self._text_cell(r, 3, program.date_text, theme.TEXT_DIM)

            btn = ghost_button("Удалить")
            btn.setEnabled(not program.system_like)
            if program.system_like:
                btn.setToolTip("Системный компонент или драйвер — защищён от удаления.")
            btn.clicked.connect(lambda _c=False, p=program: self._confirm(p))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 4, 6, 4)
            hl.addWidget(btn)
            self.table.setCellWidget(r, 4, holder)
            self.table.setRowHeight(r, 56)

    def _text_cell(self, row: int, col: int, text: str, color: str):
        item = QTableWidgetItem(text)
        item.setForeground(Qt.gray if color == theme.TEXT_DIM else Qt.white)
        if col in (2, 3):
            item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------ удаление

    def _confirm(self, program):
        box = QMessageBox(self)
        box.setWindowTitle("Удаление программы")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Удалить «{program.name}»?")
        details = f"Издатель: {program.publisher or 'неизвестен'}\n" \
                  f"Размер: {program.size_text}"
        if program.can_silent:
            details += "\n\nПрограмма умеет удаляться тихо — без лишних окон."
        else:
            details += ("\n\nОткроется обычный мастер удаления — дальше пройди "
                        "его шаги сам.")
        box.setInformativeText(details)
        yes = box.addButton("Удалить", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._run_uninstall(program)

    def _run_uninstall(self, program):
        self.summary.setText(f"Удаляю «{program.name}»…")
        worker = UninstallWorker(program, silent=program.can_silent)
        worker.done.connect(lambda ok, msg: self._after_uninstall(ok, msg))
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _after_uninstall(self, ok: bool, message: str):
        box = QMessageBox(self)
        box.setWindowTitle("Готово" if ok else "Не удалось")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText(message)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if ok:
            self._reload()
