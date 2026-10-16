"""Страница «Хроника»: что изменилось на компьютере со временем.

Компьютер меняется сам — обновления Windows возвращают отключённое,
установщики добавляют себя в автозагрузку, драйверы обновляются. Обычно это
замечают, только когда становится хуже. Здесь видно, что именно произошло
и когда.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QMessageBox,
)

from core import chronicle as chronicle_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, page_header, scroll_page, primary_button,
    ghost_button, dim_label, clear_layout,
)

_TONE = {"accent": theme.ACCENT, "ok": theme.OK, "warn": theme.WARN}


class CaptureWorker(QThread):
    done = Signal(object)

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

    def run(self):
        try:
            snapshot = chronicle_core.capture(self.ctx)
            chronicle_core.save(snapshot)
            self.done.emit(snapshot)
        except Exception:
            self.done.emit(None)


class ChroniclePage(QWidget):
    """История изменений системы."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.snapshots: list = []
        self.worker: CaptureWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Хроника",
            "Компьютер меняется сам. Здесь видно, что именно изменилось "
            "и когда — программы, автозагрузка, драйверы и сбросившиеся настройки."))

        lay.addWidget(self._controls_card())
        lay.addWidget(self._changes_card())
        lay.addStretch()

        self.reload()

    # ------------------------------------------------------------ шапка

    def _controls_card(self) -> Card:
        card = Card()
        self.status = QLabel("Читаю хронику…")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        card.add(self.status)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Сравнить:"))

        self.old_box = QComboBox()
        self.old_box.setMinimumWidth(190)
        self.old_box.currentIndexChanged.connect(self._render)
        row.addWidget(self.old_box)

        arrow = QLabel("→")
        arrow.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 15px;")
        row.addWidget(arrow)

        self.new_box = QComboBox()
        self.new_box.setMinimumWidth(190)
        self.new_box.currentIndexChanged.connect(self._render)
        row.addWidget(self.new_box)
        row.addStretch()

        self.capture_btn = primary_button("Снять слепок сейчас")
        self.capture_btn.clicked.connect(self._capture)
        row.addWidget(self.capture_btn)
        card.add_layout(row)

        card.add(dim_label(
            "Слепок снимается автоматически не чаще раза в сутки — при запуске "
            "приложения. В нём только имена и версии: ни путей к документам, "
            "ни их содержимого."))
        return card

    def _changes_card(self) -> Card:
        card = Card("Что изменилось")
        self.summary = QLabel("")
        self.summary.setObjectName("Dim")
        self.summary.setWordWrap(True)
        card.add(self.summary)

        self.changes_box = QVBoxLayout()
        self.changes_box.setSpacing(10)
        card.add_layout(self.changes_box)
        return card

    # ------------------------------------------------------------ данные

    def reload(self):
        self.snapshots = chronicle_core.history()
        count = len(self.snapshots)
        if not count:
            self.status.setText(
                "Слепков ещё нет. Нажми «Снять слепок сейчас» — это займёт "
                "несколько секунд. Смысл появится со второго слепка: сравнивать "
                "пока не с чем.")
        elif count == 1:
            self.status.setText(
                f"Есть один слепок от {self.snapshots[0].when} "
                f"({self.snapshots[0].age_text}). Сравнивать пока не с чем — "
                "второй появится автоматически завтра или по кнопке.")
        else:
            self.status.setText(
                f"Слепков: {count}. Самый ранний — {self.snapshots[-1].when}, "
                f"самый свежий — {self.snapshots[0].when} "
                f"({self.snapshots[0].age_text}).")

        for box in (self.old_box, self.new_box):
            box.blockSignals(True)
            box.clear()
            for snap in self.snapshots:
                box.addItem(f"{snap.when}  ({snap.age_text})")
            box.blockSignals(False)

        if count >= 2:
            self.old_box.blockSignals(True)
            self.new_box.blockSignals(True)
            self.old_box.setCurrentIndex(1)      # предыдущий
            self.new_box.setCurrentIndex(0)      # самый свежий
            self.old_box.blockSignals(False)
            self.new_box.blockSignals(False)
        self._render()

    def _render(self):
        clear_layout(self.changes_box)
        if len(self.snapshots) < 2:
            self.summary.setText("")
            return

        old_index = self.old_box.currentIndex()
        new_index = self.new_box.currentIndex()
        if old_index < 0 or new_index < 0:
            return
        if old_index == new_index:
            self.summary.setText("Выбран один и тот же слепок — выбери разные.")
            return

        old_snap = self.snapshots[old_index]
        new_snap = self.snapshots[new_index]
        # сравниваем всегда от старого к новому, как бы их ни выбрали
        if old_snap.stamp > new_snap.stamp:
            old_snap, new_snap = new_snap, old_snap

        changes = chronicle_core.compare(old_snap, new_snap)
        chronicle_core.resolve_titles(changes, self.ctx)
        self.summary.setText(chronicle_core.describe(changes))

        if not changes:
            self.changes_box.addWidget(dim_label(
                "Между этими слепками система не изменилась вообще. "
                "Для рабочей машины это хороший знак."))
            return

        groups: dict[str, list] = {}
        for change in changes:
            groups.setdefault(change.kind, []).append(change)

        for kind, items in groups.items():
            warn = any(c.tone == "warn" for c in items)
            section = Collapsible(kind, opened=True,
                                  accent=theme.WARN if warn else theme.ACCENT)
            for change in items:
                section.add(self._change_row(change))
            self.changes_box.addWidget(section)

    def _change_row(self, change) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        marks = {"появилось": "＋", "исчезло": "－", "изменилось": "≠"}
        name = QLabel(f"{marks.get(change.direction, '·')}  {change.title}")
        name.setStyleSheet("font-weight: 600; font-size: 13px;")
        name.setWordWrap(True)
        top.addWidget(name, 1)
        top.addWidget(Badge(change.direction,
                            _TONE.get(change.tone, theme.ACCENT)))
        lay.addLayout(top)

        if change.detail:
            detail = QLabel(change.detail)
            detail.setObjectName("Faint")
            detail.setWordWrap(True)
            lay.addWidget(detail)
        return block

    # ------------------------------------------------------------ действия

    def _capture(self):
        if self.worker and self.worker.isRunning():
            return
        self.capture_btn.setEnabled(False)
        self.capture_btn.setText("Снимаю…")
        self.status.setText("Читаю список программ, автозагрузку и драйверы…")
        self.worker = CaptureWorker(self.ctx)
        self.worker.done.connect(self._on_captured)
        self.worker.start()

    def _on_captured(self, snapshot):
        self.capture_btn.setEnabled(True)
        self.capture_btn.setText("Снять слепок сейчас")
        if snapshot is None:
            QMessageBox.warning(self, "Хроника", "Слепок собрать не удалось.")
            return
        self.reload()
