"""Страница «Проводник»: контекстное меню и поведение окон Windows.

Каждая установленная программа лезет в меню правой кнопки. Через полгода оно
открывается с задержкой, и половина пунктов принадлежит давно удалённым
программам. Здесь видно всё, что там есть, и лишнее выключается — не удаляется,
а именно выключается, чтобы вернуть можно было той же галочкой.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QCheckBox,
)

from core import explorer as explorer_core
from core import optimizer
from core.context import AppContext
from core.util import is_admin, log

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, ToggleRow, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)

SNAPSHOT = "before_explorer"


class LoadWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(explorer_core.read_handlers())
        except Exception as exc:
            log(f"Обработчики меню не прочитались: {exc}")
            self.done.emit([])


class RestartWorker(QThread):
    done = Signal(bool, str)

    def run(self):
        ok, message = explorer_core.restart_explorer()
        self.done.emit(ok, message)


class ExplorerPage(QWidget):
    """Контекстное меню и поведение проводника."""

    changed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.handlers: list = []
        self.boxes: dict[str, QCheckBox] = {}
        self.rows: dict[str, ToggleRow] = {}
        self.workers: list[QThread] = []
        self.tweaks = explorer_core.build_explorer_tweaks()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Проводник",
            "Меню правой кнопки и поведение окон. Всё обратимо: обработчики "
            "не удаляются, а выключаются — вернуть можно той же галочкой."))

        lay.addWidget(self._behaviour_card())
        lay.addWidget(self._menu_card())
        lay.addStretch()

        self.reload()

    # ------------------------------------------------------------ поведение

    def _behaviour_card(self) -> Card:
        card = Card("Поведение проводника",
                    "Настройки самого окна: что видно, где открывается, "
                    "насколько плотный список.")
        for tweak in self.tweaks:
            row = ToggleRow(tweak.id, tweak.title, tweak.desc, tweak.why,
                            tweak.risk, tweak.reboot)
            row.set_state(tweak.state())
            self.rows[tweak.id] = row
            card.add(row)

        buttons = QHBoxLayout()
        apply_btn = primary_button("Применить отмеченные")
        apply_btn.clicked.connect(self._apply_tweaks)
        buttons.addWidget(apply_btn)

        restore_btn = ghost_button("Откатить по снимку")
        restore_btn.clicked.connect(self._restore_tweaks)
        buttons.addWidget(restore_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.tweak_status = QLabel("")
        self.tweak_status.setObjectName("Dim")
        self.tweak_status.setWordWrap(True)
        card.add(self.tweak_status)
        return card

    def _apply_tweaks(self):
        wanted = {tid: row.is_checked() for tid, row in self.rows.items()}
        optimizer.save_snapshot(self.tweaks, SNAPSHOT)
        done = 0
        reboot = False
        for tweak in self.tweaks:
            want = wanted.get(tweak.id)
            if want is None or tweak.state() == want:
                continue
            if tweak.apply(want):
                done += 1
                reboot = reboot or tweak.reboot
        self._refresh_tweaks()
        text = f"Применено настроек: {done}."
        if done:
            text += (" Часть изменений видна только после перезапуска "
                     "проводника — кнопка ниже.")
        self.tweak_status.setText(text)
        self.changed.emit()

    def _restore_tweaks(self):
        if optimizer.load_snapshot(SNAPSHOT) is None:
            self.tweak_status.setText("Снимка нет — приложение здесь ещё "
                                      "ничего не меняло.")
            return
        ok, total = optimizer.restore_snapshot(self.tweaks, SNAPSHOT)
        self._refresh_tweaks()
        self.tweak_status.setText(f"Возвращено {ok} из {total}.")

    def _refresh_tweaks(self):
        for tweak in self.tweaks:
            row = self.rows.get(tweak.id)
            if row:
                row.set_state(tweak.state())

    # ------------------------------------------------------------ меню

    def _menu_card(self) -> Card:
        card = Card("Контекстное меню",
                    "Обработчики, которые Windows опрашивает при каждом щелчке "
                    "правой кнопкой. Чем их меньше, тем быстрее открывается меню.")

        if not is_admin():
            note = QLabel("Список виден, но менять обработчики можно только "
                          "с правами администратора: они живут в общесистемной "
                          "ветке реестра.")
            note.setWordWrap(True)
            note.setStyleSheet(
                f"color: {theme.WARN}; background: {theme.SURFACE_2}; "
                f"border: 1px solid {theme.WARN}55; border-radius: 8px; padding: 9px;")
            card.add(note)

        self.menu_status = QLabel("Читаю обработчики…")
        self.menu_status.setObjectName("Dim")
        self.menu_status.setWordWrap(True)
        card.add(self.menu_status)

        self.menu_body = QVBoxLayout()
        self.menu_body.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self.menu_body)
        card.add(holder)

        buttons = QHBoxLayout()
        restart_btn = primary_button("Перезапустить проводник")
        restart_btn.setToolTip("Меню обновится сразу, перезагрузка не нужна.")
        restart_btn.clicked.connect(self._restart)
        buttons.addWidget(restart_btn)

        refresh_btn = ghost_button("Перечитать список")
        refresh_btn.clicked.connect(self.reload)
        buttons.addWidget(refresh_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        card.add(dim_label(
            "Перезапуск проводника закроет открытые окна папок и на секунду "
            "уберёт панель задач. Программы и игры это не затрагивает."))
        return card

    def reload(self):
        self.menu_status.setText("Читаю обработчики…")
        worker = LoadWorker()
        worker.done.connect(self._on_loaded)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _drop(self, worker):
        if worker in self.workers:
            self.workers.remove(worker)

    def _on_loaded(self, handlers: list):
        self.handlers = handlers
        self.menu_status.setText(explorer_core.summary(handlers))
        clear_layout(self.menu_body)
        self.boxes.clear()

        groups: dict[str, list] = {}
        for handler in handlers:
            groups.setdefault(handler.scope, []).append(handler)

        for scope, items in groups.items():
            section = Collapsible(f"Меню {scope}", opened=True)
            for handler in items:
                section.add(self._handler_row(handler))
            self.menu_body.addWidget(section)

    def _handler_row(self, handler) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        box = QCheckBox(handler.display)
        box.setChecked(handler.enabled)
        box.setStyleSheet("font-weight: 600; font-size: 13px;")
        box.setEnabled(is_admin() and not handler.protected)
        if handler.protected:
            box.setToolTip("Без этого обработчика ломается работа с файлами.")
        box.toggled.connect(
            lambda state, h=handler: self._toggle(h, state))
        self.boxes[handler.name + handler.clsid] = box
        top.addWidget(box, 1)

        if handler.protected:
            top.addWidget(Badge("нужен системе", theme.TEXT_FAINT))
        if not handler.enabled:
            top.addWidget(Badge("выключен", theme.OK))
        lay.addLayout(top)

        text = handler.what or ("Приложение не знает, чей это обработчик, "
                                "и ничего о нём не утверждает.")
        hint = QLabel(text)
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        ident = QLabel(handler.clsid)
        ident.setObjectName("Faint")
        ident.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px;")
        lay.addWidget(ident)
        return block

    def _toggle(self, handler, enabled: bool):
        ok, message = explorer_core.set_handler(handler, enabled)
        if not ok:
            box = self.boxes.get(handler.name + handler.clsid)
            if box:
                box.blockSignals(True)
                box.setChecked(not enabled)
                box.blockSignals(False)
            self.menu_status.setText(message)
            return
        self.menu_status.setText(
            message + " Нажми «Перезапустить проводник», чтобы увидеть результат.")
        self.changed.emit()

    def _restart(self):
        answer = QMessageBox.question(
            self, "Перезапуск проводника",
            "Перезапустить проводник?\n\nОткрытые окна папок закроются, "
            "панель задач на секунду пропадёт. Программы и игры это не затронет.",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        worker = RestartWorker()
        worker.done.connect(
            lambda ok, msg: self.menu_status.setText(msg))
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()
