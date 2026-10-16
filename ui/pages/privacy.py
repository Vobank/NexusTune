"""Страница «Приватность»: следы работы и безвозвратное удаление."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QMessageBox, QComboBox,
)

from core import privacy as privacy_core
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, page_header, scroll_page, primary_button, ghost_button, dim_label,
)


class ScanWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(privacy_core.scan_traces())
        except Exception:
            self.done.emit([])


class CleanWorker(QThread):
    done = Signal(int, int)

    def __init__(self, keys: list[str]):
        super().__init__()
        self.keys = keys

    def run(self):
        try:
            done, mb = privacy_core.clean_traces(self.keys)
            self.done.emit(done, mb)
        except Exception:
            self.done.emit(0, 0)


class WipeWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, drive: str):
        super().__init__()
        self.drive = drive

    def run(self):
        try:
            ok, msg = privacy_core.wipe_free_space(self.drive)
            self.done.emit(ok, msg)
        except Exception as exc:
            self.done.emit(False, str(exc))


class PlanWorker(QThread):
    """Готовит план затирания: определение типа носителя идёт через WMI."""
    done = Signal(object)

    def __init__(self, drive: str):
        super().__init__()
        self.drive = drive

    def run(self):
        try:
            self.done.emit(privacy_core.plan_free_space_wipe(self.drive))
        except Exception:
            self.done.emit(None)


class PrivacyPage(QWidget):
    """Чистка следов и затирание свободного места."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: dict[str, QCheckBox] = {}
        self.items: list = []
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Приватность",
            "Windows запоминает больше, чем кажется. Здесь можно стереть следы "
            "работы и сделать так, чтобы удалённые файлы уже нельзя было вернуть."))

        lay.addWidget(self._traces_card())
        lay.addWidget(self._wipe_card())
        lay.addStretch()

        self._scan()

    # ------------------------------------------------------------ следы

    def _traces_card(self) -> Card:
        card = Card("Следы работы",
                    "Каждая строка — отдельный вид истории. Сами файлы, пароли и "
                    "настройки это не трогает: стираются только записи о том, что ты делал.")
        self.traces_box = QVBoxLayout()
        self.traces_box.setSpacing(8)
        card.add_layout(self.traces_box)
        self.traces_box.addWidget(dim_label("Считаю объём следов…"))

        row = QHBoxLayout()
        self.trace_total = QLabel("")
        self.trace_total.setObjectName("Faint")
        row.addWidget(self.trace_total)
        row.addStretch()
        self.clean_btn = primary_button("Стереть отмеченное")
        self.clean_btn.clicked.connect(self._clean)
        row.addWidget(self.clean_btn)
        card.add_layout(row)
        return card

    def _scan(self):
        worker = ScanWorker()
        worker.done.connect(self._on_scanned)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_scanned(self, items: list):
        self.items = items
        while self.traces_box.count():
            item = self.traces_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows.clear()

        total = 0
        for it in items:
            total += it.size_bytes
            block = QWidget()
            bl = QVBoxLayout(block)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(2)
            top = QHBoxLayout()
            check = QCheckBox(it.title)
            check.setChecked(it.default)
            check.setStyleSheet("font-weight: 600; font-size: 13px;")
            top.addWidget(check)
            top.addStretch()
            top.addWidget(Badge(it.size_text, theme.ACCENT if it.size_bytes else theme.TEXT_FAINT))
            bl.addLayout(top)
            why = QLabel(it.why)
            why.setObjectName("Faint")
            why.setWordWrap(True)
            bl.addWidget(why)
            self.traces_box.addWidget(block)
            self.rows[it.key] = check

        self.trace_total.setText(f"Всего следов на диске: примерно "
                                 f"{privacy_core.human_bytes(total)}")

    def _clean(self):
        keys = [k for k, c in self.rows.items() if c.isChecked()]
        if not keys:
            return
        self.clean_btn.setEnabled(False)
        self.clean_btn.setText("Стираю…")
        worker = CleanWorker(keys)
        worker.done.connect(self._after_clean)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _after_clean(self, done: int, mb: int):
        self.clean_btn.setEnabled(True)
        self.clean_btn.setText("Стереть отмеченное")
        QMessageBox.information(
            self, "Следы стёрты",
            f"Очищено категорий: {done}. Освобождено около {mb} МБ.\n\n"
            "История действий, недавние файлы и миниатюры сброшены.")
        self._scan()

    # ------------------------------------------------------------ затирание

    def _wipe_card(self) -> Card:
        card = Card("Безвозвратное удаление",
                    "Когда ты удаляешь файл, Windows лишь помечает место свободным — "
                    "сам файл лежит на диске и восстанавливается бесплатной программой. "
                    "Затирание заполняет свободное место мусором, и возвращать уже нечего.")

        row = QHBoxLayout()
        row.addWidget(QLabel("Диск:"))
        self.drive = QComboBox()
        self.drive.addItems(["C", "D", "E"])
        self.drive.currentTextChanged.connect(self._refresh_plan)
        row.addWidget(self.drive)
        row.addStretch()
        card.add_layout(row)

        self.plan_label = QLabel("")
        self.plan_label.setObjectName("Dim")
        self.plan_label.setWordWrap(True)
        card.add(self.plan_label)

        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet(
            f"color: {theme.WARN}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {theme.BORDER_LIGHT}; border-radius: 10px; padding: 10px;")
        card.add(self.warn_label)

        row2 = QHBoxLayout()
        row2.addStretch()
        self.wipe_btn = primary_button("Затереть свободное место")
        self.wipe_btn.clicked.connect(self._confirm_wipe)
        row2.addWidget(self.wipe_btn)
        card.add_layout(row2)

        self._refresh_plan()
        return card

    def _refresh_plan(self):
        """Запрашивает план в фоне: определение типа носителя идёт через WMI."""
        drive = self.drive.currentText()
        self.plan_label.setText(f"Читаю диск {drive}…")
        self.warn_label.setText("")
        self._plan = None
        worker = PlanWorker(drive)
        worker.done.connect(self._on_plan)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_plan(self, plan):
        if plan is None:
            self.plan_label.setText("Не удалось прочитать диск.")
            return
        self._plan = plan
        kind = "SSD" if plan.is_ssd else "жёсткий диск" if plan.is_ssd is False \
            else "тип неизвестен"
        self.plan_label.setText(
            f"Свободно {plan.free_gb} ГБ · {kind} · займёт ориентировочно "
            f"{plan.minutes_estimate} мин (диск будет полностью записан один раз).")
        self.warn_label.setText(plan.warning)

    def _confirm_wipe(self):
        plan = getattr(self, "_plan", None)
        if not plan:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Затирание свободного места")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Затереть свободное место на диске {plan.drive}?")
        box.setInformativeText(
            f"Это займёт около {plan.minutes_estimate} минут. Компьютером можно "
            "пользоваться, но он будет заметно загружен, а диск — полностью занят "
            "до конца операции. Прервать можно в любой момент.\n\n"
            "Твои файлы не пострадают — затирается только уже свободное место.")
        yes = box.addButton("Затереть", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._start_wipe(plan.drive)

    def _start_wipe(self, drive: str):
        self.wipe_btn.setEnabled(False)
        self.wipe_btn.setText("Затираю… это надолго")
        worker = WipeWorker(drive)
        worker.done.connect(self._after_wipe)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _after_wipe(self, ok: bool, message: str):
        self.wipe_btn.setEnabled(True)
        self.wipe_btn.setText("Затереть свободное место")
        box = QMessageBox(self)
        box.setWindowTitle("Готово" if ok else "Не завершено")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText(message)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
