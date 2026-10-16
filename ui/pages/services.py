"""Страница «Службы»: полный список служб Windows с объяснениями.

Отличие от списков «отключи эти 40 служб» из интернета в том, что здесь у
каждой службы написано, что именно перестанет работать. Критические службы
заблокированы, чужие (антивирус, драйверы, античиты) не трогаются вовсе.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QMessageBox, QProgressBar, QCheckBox,
)

from core import services as services_core
from core.context import AppContext
from core.util import is_admin, log

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, page_header, scroll_page, primary_button,
    ghost_button, dim_label, clear_layout,
)

_TONE = {"ok": theme.OK, "warn": theme.WARN, "danger": theme.DANGER,
         "dim": theme.TEXT_FAINT}


class LoadWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(services_core.read_services())
        except Exception as exc:
            log(f"Список служб не прочитался: {exc}")
            self.done.emit([])


class ApplyWorker(QThread):
    """Применяет изменения к нескольким службам подряд."""
    progress = Signal(str, int, int)
    done = Signal(int, int, list)

    def __init__(self, actions: list[tuple[str, str, str]]):
        super().__init__()
        self.actions = actions            # (имя, показываемое имя, режим)

    def run(self):
        ok = 0
        failed: list[str] = []
        for index, (name, display, mode) in enumerate(self.actions, 1):
            self.progress.emit(display, index, len(self.actions))
            try:
                if mode == "disabled":
                    success, _msg = services_core.disable(name)
                else:
                    success, _msg = services_core.set_start_type(name, mode)
                    if mode == "automatic":
                        services_core.start_service(name)
            except Exception:
                success = False
            if success:
                ok += 1
            else:
                failed.append(display)
        self.done.emit(ok, len(self.actions), failed)


class RestoreWorker(QThread):
    progress = Signal(str)
    done = Signal(int, int)

    def run(self):
        changed, total = services_core.restore_snapshot(
            progress=self.progress.emit)
        self.done.emit(changed, total)


class ServicesPage(QWidget):
    """Менеджер служб Windows."""

    changed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.services: list = []
        self.workers: list[QThread] = []
        self.rows: dict[str, QCheckBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Службы Windows",
            "У каждой службы написано, что она делает и что именно перестанет "
            "работать, если её выключить. Критические заблокированы."))

        lay.addWidget(self._controls_card())
        self.body = QVBoxLayout()
        self.body.setSpacing(14)
        holder = QWidget()
        holder.setLayout(self.body)
        lay.addWidget(holder)
        lay.addStretch()

        self.reload()

    # ------------------------------------------------------------ шапка

    def _controls_card(self) -> Card:
        card = Card()
        self.status = QLabel("Читаю список служб…")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        card.add(self.status)

        if not is_admin():
            note = QLabel("Без прав администратора список виден, но менять "
                          "ничего нельзя. Перезапусти с правами через левое меню.")
            note.setWordWrap(True)
            note.setStyleSheet(
                f"color: {theme.WARN}; background: {theme.SURFACE_2}; "
                f"border: 1px solid {theme.WARN}55; border-radius: 8px; padding: 9px;")
            card.add(note)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию службы…")
        self.search.textChanged.connect(self._render)
        filters.addWidget(self.search, 1)

        self.kind_filter = QComboBox()
        self._kinds = ["Все", services_core.SAFE, services_core.SITUATIONAL,
                       services_core.CRITICAL, services_core.UNKNOWN]
        self.kind_filter.addItems([k.capitalize() for k in self._kinds])
        self.kind_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.kind_filter)

        self.only_running = QCheckBox("Только запущенные")
        self.only_running.toggled.connect(self._render)
        filters.addWidget(self.only_running)
        card.add_layout(filters)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        safe_btn = primary_button("Отключить безопасные")
        safe_btn.setToolTip("Только службы класса «безопасная», которые сейчас "
                            "не отключены.")
        safe_btn.clicked.connect(self._disable_safe)
        buttons.addWidget(safe_btn)

        selected_btn = ghost_button("Отключить отмеченные")
        selected_btn.clicked.connect(self._disable_selected)
        buttons.addWidget(selected_btn)

        restore_btn = ghost_button("Вернуть по снимку")
        restore_btn.clicked.connect(self._restore)
        buttons.addWidget(restore_btn)

        refresh_btn = ghost_button("Обновить")
        refresh_btn.clicked.connect(self.reload)
        buttons.addWidget(refresh_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        card.add(self.bar)
        return card

    # ------------------------------------------------------------ данные

    def reload(self):
        self.status.setText("Читаю список служб…")
        worker = LoadWorker()
        worker.done.connect(self._on_loaded)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _drop(self, worker):
        if worker in self.workers:
            self.workers.remove(worker)

    def _on_loaded(self, services: list):
        self.services = services
        self.status.setText(services_core.summary(services))
        self._render()

    def _render(self):
        clear_layout(self.body)
        self.rows.clear()
        if not self.services:
            return

        text = self.search.text().strip().lower()
        kind = self._kinds[self.kind_filter.currentIndex()]
        only_running = self.only_running.isChecked()

        groups: dict[str, list] = {}
        for service in self.services:
            if only_running and not service.running:
                continue
            if kind != "Все" and service.kind != kind:
                continue
            if text and text not in f"{service.display} {service.name}".lower():
                continue
            groups.setdefault(service.kind, []).append(service)

        order = [services_core.SAFE, services_core.SITUATIONAL,
                 services_core.UNKNOWN, services_core.CRITICAL]
        hints = {
            services_core.SAFE:
                "Эти службы на игровом компьютере не нужны почти никому. "
                "Отключение безопасно и обратимо.",
            services_core.SITUATIONAL:
                "Зависит от того, чем ты пользуешься. Прочитай последствия — "
                "они написаны у каждой строки.",
            services_core.UNKNOWN:
                "Приложение об этих службах ничего не знает и советов не даёт. "
                "Службы антивируса, драйверов и античитов помечены замком.",
            services_core.CRITICAL:
                "Без них Windows не работает. Изменить их отсюда нельзя.",
        }
        accents = {services_core.SAFE: theme.OK,
                   services_core.SITUATIONAL: theme.WARN,
                   services_core.UNKNOWN: theme.TEXT_FAINT,
                   services_core.CRITICAL: theme.DANGER}

        for kind_key in order:
            items = groups.get(kind_key)
            if not items:
                continue
            section = Collapsible(
                kind_key.capitalize(), hints[kind_key],
                opened=(kind_key in (services_core.SAFE, services_core.SITUATIONAL)),
                accent=accents[kind_key])
            for service in items[:200]:
                section.add(self._service_row(service))
            if len(items) > 200:
                section.add(dim_label(f"…и ещё {len(items) - 200}. "
                                      "Сузь список поиском."))
            self.body.addWidget(section)

    def _service_row(self, service) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        if service.can_change:
            check = QCheckBox(service.display)
            check.setStyleSheet("font-weight: 600; font-size: 13px;")
            self.rows[service.name] = check
            top.addWidget(check, 1)
        else:
            locked = QLabel(f"🔒  {service.display}")
            locked.setStyleSheet(f"font-weight: 600; font-size: 13px; "
                                 f"color: {theme.TEXT_DIM};")
            locked.setWordWrap(True)
            top.addWidget(locked, 1)

        if service.running:
            memory = f" · {service.memory_mb:.0f} МБ" if service.memory_mb else ""
            top.addWidget(Badge(f"работает{memory}", theme.ACCENT))
        top.addWidget(Badge(service.start_text,
                            theme.TEXT_FAINT if service.start_type != "disabled"
                            else theme.OK))
        lay.addLayout(top)

        if service.what:
            what = QLabel(service.what)
            what.setObjectName("Dim")
            what.setWordWrap(True)
            lay.addWidget(what)
        if service.consequence:
            colour = (theme.TEXT_FAINT if service.kind == services_core.SAFE
                      else theme.WARN)
            hint = QLabel("→ " + service.consequence)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {colour}; font-size: 11px;")
            lay.addWidget(hint)
        if not service.what and service.description:
            desc = QLabel(service.description[:220])
            desc.setObjectName("Faint")
            desc.setWordWrap(True)
            lay.addWidget(desc)
        return block

    # ------------------------------------------------------------ действия

    def _disable_safe(self):
        targets = services_core.recommended_to_disable(self.services)
        if not targets:
            self.status.setText("Отключать нечего — все безопасные службы "
                                "уже выключены.")
            return
        self._confirm_and_apply(targets, "безопасные службы")

    def _disable_selected(self):
        names = {name for name, box in self.rows.items() if box.isChecked()}
        targets = [s for s in self.services if s.name in names and s.can_change]
        if not targets:
            self.status.setText("Ни одна служба не отмечена.")
            return
        self._confirm_and_apply(targets, "отмеченные службы")

    def _confirm_and_apply(self, targets: list, what: str):
        if not is_admin():
            QMessageBox.information(
                self, "Нужны права",
                "Менять службы можно только с правами администратора. "
                "Перезапусти приложение через кнопку в левом меню.")
            return

        risky = [s for s in targets if s.kind == services_core.SITUATIONAL]
        lines = [f"• {s.display} — {s.consequence}" for s in targets[:14]]
        if len(targets) > 14:
            lines.append(f"…и ещё {len(targets) - 14}.")

        box = QMessageBox(self)
        box.setWindowTitle("Отключение служб")
        box.setIcon(QMessageBox.Warning if risky else QMessageBox.Question)
        box.setText(f"Отключить {what}? Всего: {len(targets)}.")
        box.setInformativeText(
            "\n".join(lines) +
            "\n\nПеред изменением сохраняется снимок — вернуть всё можно "
            "кнопкой «Вернуть по снимку».")
        yes = box.addButton("Отключить", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if box.clickedButton() is not yes:
            return

        services_core.save_snapshot(self.services)
        actions = [(s.name, s.display, "disabled") for s in targets]
        self.bar.setVisible(True)
        self.bar.setRange(0, len(actions))
        worker = ApplyWorker(actions)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_applied)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _on_progress(self, display: str, index: int, total: int):
        self.bar.setValue(index)
        self.status.setText(f"[{index}/{total}] {display}")

    def _on_applied(self, ok: int, total: int, failed: list):
        self.bar.setVisible(False)
        text = f"Отключено служб: {ok} из {total}."
        if failed:
            text += " Не поддались: " + ", ".join(failed[:4])
            if len(failed) > 4:
                text += f" и ещё {len(failed) - 4}"
            text += "."
        self.status.setText(text)
        self.changed.emit()
        self.reload()

    def _restore(self):
        if services_core.load_snapshot() is None:
            self.status.setText("Снимка нет — приложение ещё ничего не меняло.")
            return
        answer = QMessageBox.question(
            self, "Возврат служб",
            "Вернуть типы запуска всех служб к сохранённому снимку?",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.status.setText("Возвращаю прежние настройки служб…")
        worker = RestoreWorker()
        worker.progress.connect(lambda name: self.status.setText(f"Возвращаю {name}…"))
        worker.done.connect(self._on_restored)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _on_restored(self, changed: int, total: int):
        self.status.setText(f"Возвращено служб: {changed} из {total}.")
        self.changed.emit()
        self.reload()
