"""Страница оптимизации Windows: твики с описанием, риском и откатом."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
)

from core import optimizer
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, SectionTitle, ToggleRow, page_header, scroll_page,
    primary_button, ghost_button, dim_label,
)


class ApplyWorker(QThread):
    """Применяет набор твиков в фоне."""
    progress = Signal(str, int, int)
    done = Signal(int, int, bool)

    def __init__(self, ctx: AppContext, wanted: dict[str, bool]):
        super().__init__()
        self.ctx = ctx
        self.wanted = wanted

    def run(self):
        items = list(self.wanted.items())
        ok = 0
        reboot_needed = False
        for i, (tweak_id, enable) in enumerate(items, 1):
            tweak = self.ctx.tweak(tweak_id)
            if not tweak:
                continue
            self.progress.emit(tweak.title, i, len(items))
            if tweak.state() == enable:
                ok += 1
                continue
            if tweak.apply(enable):
                ok += 1
                reboot_needed = reboot_needed or tweak.reboot
        self.done.emit(ok, len(items), reboot_needed)


class RestoreWorker(QThread):
    done = Signal(int, int)

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

    def run(self):
        ok, total = optimizer.restore_snapshot(self.ctx.tweaks, "before_optimize")
        self.done.emit(ok, total)


class RestorePointWorker(QThread):
    done = Signal(bool)

    def run(self):
        self.done.emit(optimizer.create_restore_point())


class SystemPage(QWidget):
    """Каталог твиков Windows."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: dict[str, ToggleRow] = {}
        self.worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Оптимизация Windows",
            "Каждая настройка показывает, что именно она меняет и зачем. "
            "Перед применением сохраняется снимок — вернуть всё можно одной кнопкой."))

        lay.addWidget(self._build_controls())

        categories: dict[str, list] = {}
        for tweak in ctx.tweaks:
            categories.setdefault(tweak.category, []).append(tweak)

        for category, tweaks in categories.items():
            card = Card(category)
            for tweak in tweaks:
                row = ToggleRow(tweak.id, tweak.title, tweak.desc, tweak.why,
                                tweak.risk, tweak.reboot)
                row.set_state(tweak.state())
                self.rows[tweak.id] = row
                card.add(row)
            lay.addWidget(card)

        lay.addStretch()

    def _build_controls(self) -> Card:
        card = Card()
        top = QHBoxLayout()
        top.setSpacing(10)

        apply_btn = primary_button("Применить рекомендованные")
        apply_btn.clicked.connect(self.apply_recommended)
        top.addWidget(apply_btn)

        selected_btn = ghost_button("Применить отмеченные")
        selected_btn.clicked.connect(self.apply_selected)
        top.addWidget(selected_btn)

        restore_btn = ghost_button("Откатить всё")
        restore_btn.clicked.connect(self.restore_all)
        top.addWidget(restore_btn)

        point_btn = ghost_button("Точка восстановления Windows")
        point_btn.clicked.connect(self.make_restore_point)
        top.addWidget(point_btn)
        top.addStretch()

        if not is_admin():
            top.addWidget(Badge("часть настроек требует прав администратора", theme.WARN))
        card.add_layout(top)

        self.status = QLabel("Готово к работе.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        card.add(self.status)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        card.add(self.bar)
        return card

    # ------------------------------------------------------------ действия

    def _start(self, wanted: dict[str, bool]):
        if not wanted:
            self.status.setText("Нечего применять — ни одна настройка не отмечена.")
            return
        optimizer.save_snapshot(self.ctx.tweaks, "before_optimize")
        self.bar.setVisible(True)
        self.bar.setRange(0, len(wanted))
        self.worker = ApplyWorker(self.ctx, wanted)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def apply_recommended(self):
        wanted = {t.id: True for t in self.ctx.tweaks if t.recommended}
        self._start(wanted)

    def apply_selected(self):
        wanted = {tid: row.is_checked() for tid, row in self.rows.items()}
        self._start(wanted)

    def _on_progress(self, title: str, index: int, total: int):
        self.bar.setValue(index)
        self.status.setText(f"[{index}/{total}] {title}")

    def _on_done(self, ok: int, total: int, reboot: bool):
        self.bar.setVisible(False)
        self.refresh_states()
        text = f"Применено {ok} из {total}."
        if reboot:
            text += " Часть изменений заработает после перезагрузки."
        self.status.setText(text)

    def restore_all(self):
        if optimizer.load_snapshot("before_optimize") is None:
            self.status.setText("Снимка нет — сначала примени хотя бы одну настройку.")
            return
        answer = QMessageBox.question(
            self, "Откат настроек",
            "Вернуть все настройки Windows к состоянию до оптимизации?",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.status.setText("Возвращаю прежние настройки…")
        self.worker = RestoreWorker(self.ctx)
        self.worker.done.connect(self._on_restored)
        self.worker.start()

    def _on_restored(self, ok: int, total: int):
        self.refresh_states()
        self.status.setText(f"Возвращено {ok} из {total} настроек.")

    def make_restore_point(self):
        if not is_admin():
            self.status.setText("Точку восстановления можно создать только с правами "
                                "администратора.")
            return
        self.status.setText("Создаю точку восстановления Windows, это займёт до минуты…")
        self.worker = RestorePointWorker()
        self.worker.done.connect(
            lambda ok: self.status.setText(
                "Точка восстановления создана." if ok else
                "Не удалось создать точку — возможно, защита системы отключена "
                "в свойствах системы."))
        self.worker.start()

    def refresh_states(self):
        for tweak in self.ctx.tweaks:
            row = self.rows.get(tweak.id)
            if row:
                row.set_state(tweak.state())
