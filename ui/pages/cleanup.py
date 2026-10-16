"""Страница уборки: мусорные задачи, автозагрузка и лишние файлы."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QProgressBar, QFrame,
)

from core import appx as appx_core
from core import cleanup as cleanup_core
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)


class ScanWorker(QThread):
    """Считает объём мусора в фоне: обход папок долгий."""
    done = Signal(list, int)

    def run(self):
        try:
            junk = cleanup_core.scan_junk()
            recycle = cleanup_core.recycle_bin_size()
        except Exception:
            junk, recycle = [], 0
        self.done.emit(junk, recycle)


class ActionWorker(QThread):
    """Выполняет уборку в фоне."""
    done = Signal(str)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            self.done.emit(str(self.func(*self.args)))
        except Exception as exc:
            self.done.emit(f"ошибка: {exc}")


class TaskWorker(QThread):
    done = Signal(int, int)

    def __init__(self, enabled: bool):
        super().__init__()
        self.enabled = enabled

    def run(self):
        done, total = cleanup_core.set_junk_tasks(self.enabled)
        self.done.emit(done, total)


class StartupWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(cleanup_core.startup_items())
        except Exception:
            self.done.emit([])


class CleanupPage(QWidget):
    """Всё, что можно вычистить из системы."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.junk_boxes: list[tuple[QCheckBox, str, float]] = []
        self.startup_rows: dict[str, QCheckBox] = {}
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Уборка",
            "Три источника тормозов: мусорные файлы на диске, программы в "
            "автозагрузке и фоновые задачи планировщика. Здесь всё видно и "
            "убирается кнопкой."))

        lay.addWidget(self._junk_card())
        lay.addWidget(self._startup_card())
        lay.addWidget(self._tasks_card())
        lay.addWidget(self._appx_card())
        lay.addStretch()

        self._scan_junk()
        self._load_startup()
        self._load_appx()

    # ------------------------------------------------------------ мусорные файлы

    def _junk_card(self) -> Card:
        card = Card("Мусорные файлы",
                    "Ничего личного здесь нет: только кэш, временные файлы и "
                    "отчёты о сбоях. Всё это создаётся заново по мере надобности.")

        head = QHBoxLayout()
        self.junk_total = QLabel("Считаю…")
        self.junk_total.setStyleSheet(
            f"font-size: 26px; font-weight: 800; color: {theme.WARN};")
        head.addWidget(self.junk_total)
        head.addStretch()
        rescan = ghost_button("Пересчитать")
        rescan.clicked.connect(self._scan_junk)
        head.addWidget(rescan)
        card.add_layout(head)

        self.junk_list = QVBoxLayout()
        self.junk_list.setSpacing(4)
        card.add_layout(self.junk_list)

        self.junk_bar = QProgressBar()
        self.junk_bar.setRange(0, 0)
        self.junk_bar.setVisible(False)
        card.add(self.junk_bar)

        row = QHBoxLayout()
        self.clean_btn = primary_button("🧹  Убрать отмеченное")
        self.clean_btn.clicked.connect(self._clean_junk)
        self.clean_btn.setEnabled(False)
        row.addWidget(self.clean_btn)

        recycle_btn = ghost_button("Очистить корзину")
        recycle_btn.clicked.connect(self._empty_recycle)
        row.addWidget(recycle_btn)
        row.addStretch()
        card.add_layout(row)

        self.junk_status = QLabel("")
        self.junk_status.setObjectName("Dim")
        self.junk_status.setWordWrap(True)
        card.add(self.junk_status)
        return card

    def _scan_junk(self):
        self.junk_total.setText("Считаю…")
        self.junk_bar.setVisible(True)
        self.clean_btn.setEnabled(False)
        worker = ScanWorker()
        worker.done.connect(self._on_junk_scanned)
        self.workers.append(worker)
        worker.start()

    def _on_junk_scanned(self, junk: list, recycle_mb: int):
        self.junk_bar.setVisible(False)
        self.clean_btn.setEnabled(True)
        clear_layout(self.junk_list)
        self.junk_boxes.clear()

        total = sum(item["mb"] for item in junk)
        self.junk_total.setText(
            f"{total / 1024:.1f} ГБ" if total > 1024 else f"{total:.0f} МБ")
        self.junk_total.setStyleSheet(
            "font-size: 26px; font-weight: 800; color: "
            f"{theme.DANGER if total > 5000 else theme.WARN if total > 1000 else theme.OK};")

        for item in junk:
            row = QHBoxLayout()
            box = QCheckBox(item["title"])
            box.setChecked(item["default"] and item["mb"] > 0)
            box.setEnabled(item["mb"] > 0)
            box.setStyleSheet("font-weight: 600;")
            row.addWidget(box, 1)
            size = QLabel(f"{item['mb']:.0f} МБ" if item["mb"] < 1024
                          else f"{item['mb'] / 1024:.1f} ГБ")
            size.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700;")
            size.setAlignment(Qt.AlignRight)
            size.setMinimumWidth(90)
            row.addWidget(size)
            self.junk_list.addLayout(row)

            why = QLabel("    " + item["why"])
            why.setObjectName("Faint")
            why.setWordWrap(True)
            self.junk_list.addWidget(why)
            self.junk_boxes.append((box, item["key"], item["mb"]))

        if recycle_mb > 50:
            self.junk_status.setText(f"В корзине лежит ещё {recycle_mb} МБ.")

    def _clean_junk(self):
        keys = [key for box, key, _mb in self.junk_boxes if box.isChecked()]
        if not keys:
            self.junk_status.setText("Ничего не отмечено.")
            return
        self.junk_bar.setVisible(True)
        self.junk_status.setText("Убираю…")
        worker = ActionWorker(cleanup_core.clean_categories, keys)
        worker.done.connect(self._on_junk_cleaned)
        self.workers.append(worker)
        worker.start()

    def _on_junk_cleaned(self, result: str):
        self.junk_bar.setVisible(False)
        self.junk_status.setText(f"Освобождено {result} МБ.")
        self._scan_junk()

    def _empty_recycle(self):
        worker = ActionWorker(cleanup_core.empty_recycle_bin)
        worker.done.connect(lambda r: self.junk_status.setText("Корзина очищена."))
        self.workers.append(worker)
        worker.start()

    # ------------------------------------------------------------ автозагрузка

    def _startup_card(self) -> Card:
        card = Card("Автозагрузка",
                    "Каждая программа здесь отодвигает момент, когда компьютером "
                    "можно пользоваться. Отключение не удаляет программу — она "
                    "просто не стартует сама.")

        head = QHBoxLayout()
        self.startup_count = QLabel("Читаю…")
        self.startup_count.setObjectName("Dim")
        head.addWidget(self.startup_count)
        head.addStretch()

        trim_btn = primary_button("Отключить лишнее автоматически")
        trim_btn.clicked.connect(self._trim_startup)
        head.addWidget(trim_btn)

        restore_btn = ghost_button("Вернуть отключённое")
        restore_btn.clicked.connect(self._restore_startup)
        head.addWidget(restore_btn)
        card.add_layout(head)

        self.startup_list = QVBoxLayout()
        self.startup_list.setSpacing(4)
        card.add_layout(self.startup_list)

        self.startup_status = QLabel("")
        self.startup_status.setObjectName("Faint")
        self.startup_status.setWordWrap(True)
        card.add(self.startup_status)
        return card

    def _load_startup(self):
        worker = StartupWorker()
        worker.done.connect(self._on_startup_loaded)
        self.workers.append(worker)
        worker.start()

    def _on_startup_loaded(self, items: list):
        clear_layout(self.startup_list)
        self.startup_rows.clear()
        enabled = sum(1 for i in items if i.enabled)
        heavy = sum(1 for i in items if i.heavy and i.enabled)
        self.startup_count.setText(
            f"Всего программ: {len(items)} · включено: {enabled} · "
            f"из них лишних: {heavy}")

        for item in items:
            row = QHBoxLayout()
            box = QCheckBox(item.name)
            box.setChecked(item.enabled)
            box.setStyleSheet("font-weight: 600;" if item.heavy else "")
            box.toggled.connect(
                lambda state, it=item: self._toggle_startup(it, state))
            row.addWidget(box, 1)
            if item.heavy:
                row.addWidget(Badge("можно отключить", theme.WARN))
            row.addWidget(Badge(item.location, theme.TEXT_FAINT))
            self.startup_list.addLayout(row)

            if item.hint:
                hint = QLabel("    " + item.hint)
                hint.setObjectName("Faint")
                hint.setWordWrap(True)
                self.startup_list.addWidget(hint)
            self.startup_rows[item.name] = box

    def _toggle_startup(self, item, enabled: bool):
        ok = cleanup_core.set_startup(item, enabled)
        self.startup_status.setText(
            f"{item.name}: {'включена' if enabled else 'отключена'}." if ok
            else f"Не удалось изменить {item.name}.")

    def _trim_startup(self):
        worker = ActionWorker(cleanup_core.trim_startup)
        worker.done.connect(lambda r: (
            self.startup_status.setText(f"Отключено программ: {r}."),
            self._load_startup()))
        self.workers.append(worker)
        worker.start()

    def _restore_startup(self):
        worker = ActionWorker(cleanup_core.restore_startup)
        worker.done.connect(lambda r: (
            self.startup_status.setText(f"Возвращено программ: {r}."),
            self._load_startup()))
        self.workers.append(worker)
        worker.start()

    # ------------------------------------------------------------ задачи планировщика

    def _tasks_card(self) -> Card:
        card = Card(
            "Мусорные задачи планировщика",
            "Задачи телеметрии и сбора статистики. Просыпаются по расписанию, "
            "грузят диск и сеть. Ни одна из них не нужна для работы Windows.")

        head = QHBoxLayout()
        disable_btn = primary_button("Усыпить все")
        disable_btn.clicked.connect(lambda: self._set_tasks(False))
        head.addWidget(disable_btn)
        enable_btn = ghost_button("Вернуть в работу")
        enable_btn.clicked.connect(lambda: self._set_tasks(True))
        head.addWidget(enable_btn)
        head.addStretch()
        if not is_admin():
            head.addWidget(Badge("нужны права администратора", theme.WARN))
        card.add_layout(head)

        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 0)
        self.task_bar.setVisible(False)
        card.add(self.task_bar)

        self.task_status = QLabel("")
        self.task_status.setObjectName("Dim")
        self.task_status.setWordWrap(True)
        card.add(self.task_status)

        card.add(SectionTitle("Что именно будет усыплено"))
        for task in cleanup_core.JUNK_TASKS:
            row = QLabel(f"<b>{task.title}</b> — {task.why}")
            row.setObjectName("Faint")
            row.setWordWrap(True)
            card.add(row)
        return card

    def _set_tasks(self, enabled: bool):
        if not is_admin():
            self.task_status.setText(
                "Нужны права администратора. Перезапусти приложение через "
                "«Запустить КРЕМЕНЬ.bat».")
            return
        self.task_bar.setVisible(True)
        self.task_status.setText("Работаю с планировщиком, это занимает полминуты…")
        worker = TaskWorker(enabled)
        worker.done.connect(lambda done, total: self._on_tasks_done(done, total, enabled))
        self.workers.append(worker)
        worker.start()

    def _on_tasks_done(self, done: int, total: int, enabled: bool):
        self.task_bar.setVisible(False)
        verb = "возвращено в работу" if enabled else "усыплено"
        self.task_status.setText(f"Задач {verb}: {done} из {total}.")


class AppxWorker(QThread):
    """Читает список приложений магазина в фоне."""
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(appx_core.list_packages())
        except Exception:
            self.done.emit([])


class AppxRemoveWorker(QThread):
    done = Signal(int, list)

    def __init__(self, packages: list):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            removed, messages = appx_core.remove_many(self.packages)
        except Exception as exc:
            removed, messages = 0, [str(exc)]
        self.done.emit(removed, messages)


def _appx_card(self) -> Card:
    """Удаление предустановленных приложений Windows."""
    card = Card(
        "Предустановленные приложения Windows",
        "Windows приносит с собой приложения, которыми почти никто не пользуется: "
        "они занимают место и обновляются в фоне. Здесь показано только то, "
        "что мы уверенно опознали как лишнее.")

    head = QHBoxLayout()
    self.appx_count = QLabel("Читаю список…")
    self.appx_count.setObjectName("Dim")
    head.addWidget(self.appx_count)
    head.addStretch()

    self.appx_btn = primary_button("Удалить отмеченное")
    self.appx_btn.clicked.connect(self._remove_appx)
    self.appx_btn.setEnabled(False)
    head.addWidget(self.appx_btn)
    card.add_layout(head)

    self.appx_list = QVBoxLayout()
    self.appx_list.setSpacing(4)
    card.add_layout(self.appx_list)

    self.appx_bar = QProgressBar()
    self.appx_bar.setRange(0, 0)
    self.appx_bar.setVisible(False)
    card.add(self.appx_bar)

    self.appx_status = QLabel("")
    self.appx_status.setObjectName("Dim")
    self.appx_status.setWordWrap(True)
    card.add(self.appx_status)

    card.add(dim_label(appx_core.restore_hint()))
    return card


def _load_appx(self):
    worker = AppxWorker()
    worker.done.connect(self._on_appx_loaded)
    self.workers.append(worker)
    worker.start()


def _on_appx_loaded(self, packages: list):
    clear_layout(self.appx_list)
    self.appx_boxes = []
    bloat = appx_core.bloat_only(packages)
    mine = appx_core.user_apps(packages)
    protected = [p for p in packages if p.protected]

    self.appx_count.setText(
        f"Всего приложений: {len(packages)} · опознано лишних: {len(bloat)} · "
        f"твоих собственных: {len(mine)} · защищено системой: {len(protected)}")
    self.appx_btn.setEnabled(bool(bloat))

    if not bloat:
        note = QLabel("Лишних предустановленных приложений не найдено — "
                      "система чистая. Это хорошая новость.")
        note.setObjectName("Dim")
        note.setWordWrap(True)
        self.appx_list.addWidget(note)
    for package in bloat:
        row = QHBoxLayout()
        box = QCheckBox(package.display_name)
        box.setChecked(True)
        box.setStyleSheet("font-weight: 600;")
        row.addWidget(box, 1)
        row.addWidget(Badge("можно удалить", theme.WARN))
        self.appx_list.addLayout(row)

        why = QLabel("    " + package.known)
        why.setObjectName("Faint")
        why.setWordWrap(True)
        self.appx_list.addWidget(why)
        self.appx_boxes.append((box, package))

    if mine:
        self.appx_list.addWidget(SectionTitle("Это ты установил сам — не трогаем"))
        names = QLabel("    " + ", ".join(p.display_name for p in mine))
        names.setObjectName("Faint")
        names.setWordWrap(True)
        self.appx_list.addWidget(names)


def _remove_appx(self):
    chosen = [package for box, package in getattr(self, "appx_boxes", []) if box.isChecked()]
    if not chosen:
        self.appx_status.setText("Ничего не отмечено.")
        return
    self.appx_btn.setEnabled(False)
    self.appx_bar.setVisible(True)
    self.appx_status.setText(f"Удаляю {len(chosen)} приложений…")
    worker = AppxRemoveWorker(chosen)
    worker.done.connect(self._on_appx_removed)
    self.workers.append(worker)
    worker.start()


def _on_appx_removed(self, removed: int, messages: list):
    self.appx_bar.setVisible(False)
    self.appx_btn.setEnabled(True)
    self.appx_status.setText(f"Удалено приложений: {removed}. " + " ".join(messages[-2:]))
    self._load_appx()


CleanupPage._appx_card = _appx_card
CleanupPage._load_appx = _load_appx
CleanupPage._on_appx_loaded = _on_appx_loaded
CleanupPage._remove_appx = _remove_appx
CleanupPage._on_appx_removed = _on_appx_removed
