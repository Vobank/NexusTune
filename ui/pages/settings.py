"""Страница настроек приложения: внешний вид, поведение, данные.

Здесь настраивается сам КРЕМЕНЬ, а не Windows. Всё сохраняется сразу,
кнопки «применить» нет намеренно — лишний шаг ни к чему.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox, QSpinBox,
    QDoubleSpinBox, QLineEdit, QMessageBox, QFileDialog, QGridLayout, QApplication,
    QPushButton,
)

from core import profiles as profiles_core
from core import maintenance as maintenance_core
from core import settings as settings_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, page_header, scroll_page, primary_button,
    ghost_button, dim_label,
)


class PaletteSwatch(QPushButton):
    """Кнопка выбора палитры с живым образцом цветов."""

    def __init__(self, key: str, data: dict, parent=None):
        super().__init__(parent)
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(76)
        self.setText(f"  {data['title']}\n  {data['hint']}")
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; padding: 10px 14px; font-size: 13px; "
            f"font-weight: 600; color: {data['TEXT']}; background: {data['BG']}; "
            f"border: 1px solid {data['BORDER_LIGHT']}; border-radius: 12px; }}"
            f"QPushButton:checked {{ border: 2px solid {data['ACCENT']}; }}"
            f"QPushButton:hover {{ border-color: {data['ACCENT']}; }}")


class SettingsPage(QWidget):
    """Настройки приложения."""

    palette_changed = Signal(str)
    restart_hint = Signal(str)

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._loading = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Настройки",
            "Здесь настраивается сам КРЕМЕНЬ. Всё сохраняется сразу — "
            "отдельной кнопки «применить» нет."))

        lay.addWidget(self._appearance_card())
        lay.addWidget(self._startup_card())
        lay.addWidget(self._autopilot_card())
        lay.addWidget(self._hotkeys_card())
        lay.addWidget(self._overlay_card())
        lay.addWidget(self._energy_card())
        lay.addWidget(self._maintenance_card())
        lay.addWidget(self._safety_card())
        lay.addWidget(self._data_card())
        lay.addStretch()

        self._loading = False

    # ------------------------------------------------------------ вспомогательное

    def _set(self, key: str, value):
        if self._loading:
            return
        self.ctx.config[key] = value
        self.ctx.save()

    def _row(self, label: str, widget: QWidget, hint: str = "") -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        top = QHBoxLayout()
        name = QLabel(label)
        name.setStyleSheet("font-size: 13px; font-weight: 600;")
        top.addWidget(name, 1)
        top.addWidget(widget)
        lay.addLayout(top)
        if hint:
            lay.addWidget(dim_label(hint))
        return box

    # ------------------------------------------------------------ внешний вид

    def _appearance_card(self) -> Card:
        card = Card("Внешний вид",
                    "Палитра меняется сразу. Отдельные элементы, которые взяли "
                    "цвет при создании, догонят после перезапуска.")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.swatches: dict[str, PaletteSwatch] = {}
        current = self.ctx.config.get("palette", theme.DEFAULT_PALETTE)
        for index, (key, data) in enumerate(theme.PALETTES.items()):
            swatch = PaletteSwatch(key, data)
            swatch.setChecked(key == current)
            swatch.clicked.connect(lambda _c=False, k=key: self._pick_palette(k))
            self.swatches[key] = swatch
            grid.addWidget(swatch, index // 3, index % 3)
        card.add_layout(grid)

        start = QComboBox()
        self._pages = [("dashboard", "Обзор"), ("forge", "Кузница"),
                       ("profiles", "Профили"), ("games", "Игры"),
                       ("health", "Диагностика"), ("processes", "Процессы"),
                       ("power", "Энергия"), ("expert", "Эксперт")]
        for _key, title in self._pages:
            start.addItem(title)
        saved = self.ctx.config.get("start_page", "dashboard")
        keys = [k for k, _t in self._pages]
        start.setCurrentIndex(keys.index(saved) if saved in keys else 0)
        start.currentIndexChanged.connect(
            lambda i: self._set("start_page", self._pages[i][0]))
        card.add(self._row("Страница при запуске", start,
                           "Какую страницу открывать сразу после старта."))
        return card

    def _pick_palette(self, key: str):
        for k, swatch in self.swatches.items():
            swatch.setChecked(k == key)
        self._set("palette", key)
        self.palette_changed.emit(key)

    # ------------------------------------------------------------ запуск

    def _startup_card(self) -> Card:
        card = Card("Запуск и поведение")

        self.autostart = QCheckBox()
        self.autostart.setChecked(settings_core.autostart_enabled())
        self.autostart.toggled.connect(self._toggle_autostart)
        card.add(self._row(
            "Запускать вместе с Windows", self.autostart,
            "Нужно, чтобы автопилот и умный баланс работали без твоего участия. "
            "При автозапуске Windows не даёт прав администратора — системные "
            "твики до перезапуска с правами будут недоступны."))

        self.start_min = QCheckBox()
        self.start_min.setChecked(bool(self.ctx.config.get("start_minimized")))
        self.start_min.toggled.connect(lambda v: self._set("start_minimized", v))
        card.add(self._row("Запускаться свёрнутым в трей", self.start_min,
                           "Окно не открывается, приложение сразу уходит к часам."))

        self.to_tray = QCheckBox()
        self.to_tray.setChecked(bool(self.ctx.config.get("minimize_to_tray", True)))
        self.to_tray.toggled.connect(lambda v: self._set("minimize_to_tray", v))
        card.add(self._row(
            "Закрытие окна сворачивает в трей", self.to_tray,
            "Если выключить, крестик будет полностью закрывать приложение, "
            "и фоновые функции остановятся."))

        interval = QSpinBox()
        interval.setRange(400, 5000)
        interval.setSingleStep(100)
        interval.setSuffix(" мс")
        interval.setValue(int(self.ctx.config.get("sensor_interval_ms", 1200)))
        interval.valueChanged.connect(lambda v: self._set("sensor_interval_ms", v))
        card.add(self._row(
            "Частота опроса датчиков", interval,
            "Меньше — плавнее графики, но чаще дёргается nvidia-smi. "
            "Ниже 800 мс смысла обычно нет. Применяется после перезапуска."))
        return card

    # ------------------------------------------------------------ автопилот

    def _autopilot_card(self) -> Card:
        card = Card("Автопилот",
                    "Приложение само замечает запуск игры, включает профиль, "
                    "а после выхода возвращает всё обратно.")

        self.autopilot = QCheckBox()
        self.autopilot.setChecked(bool(self.ctx.config.get("autopilot")))
        self.autopilot.toggled.connect(self._toggle_autopilot)
        card.add(self._row("Включить автопилот", self.autopilot))

        keys = [p.key for p in profiles_core.PROFILES]
        titles = [f"{p.icon}  {p.title}" for p in profiles_core.PROFILES]

        game_box = QComboBox()
        game_box.addItems(titles)
        saved = self.ctx.config.get("autopilot_profile", "gaming")
        game_box.setCurrentIndex(keys.index(saved) if saved in keys else 0)
        game_box.currentIndexChanged.connect(
            lambda i: self._set_autopilot_profile("autopilot_profile", keys[i]))
        card.add(self._row("Профиль на время игры", game_box))

        back_box = QComboBox()
        back_box.addItems(titles)
        saved = self.ctx.config.get("autopilot_restore", "stock")
        back_box.setCurrentIndex(keys.index(saved) if saved in keys else 0)
        back_box.currentIndexChanged.connect(
            lambda i: self._set_autopilot_profile("autopilot_restore", keys[i]))
        card.add(self._row("Профиль после выхода из игры", back_box))
        return card

    def _set_autopilot_profile(self, key: str, value: str):
        self._set(key, value)
        watcher = self.ctx.watcher
        if watcher:
            if key == "autopilot_profile":
                watcher.game_profile = value
            else:
                watcher.restore_profile = value

    def _toggle_autopilot(self, enabled: bool):
        self._set("autopilot", enabled)
        watcher = self.ctx.watcher
        if not watcher:
            return
        try:
            watcher.start() if enabled else watcher.stop()
        except Exception:
            pass

    # ------------------------------------------------------------ горячие клавиши

    def _hotkeys_card(self) -> Card:
        from ..hotkeys import DEFAULT_HOTKEYS, parse_combo

        card = Card("Горячие клавиши",
                    "Эти комбинации работают всегда — даже когда окно свёрнуто "
                    "и на экране полноэкранная игра. Windows доставит нажатие "
                    "приложению напрямую.")
        saved = dict(self.ctx.config.get("hotkeys") or {})
        window = self.window()
        state = {h.key: h for h in getattr(window, "hotkey_state", [])}

        self.hotkey_fields: dict[str, QLineEdit] = {}
        for key, (default_combo, title) in DEFAULT_HOTKEYS.items():
            field = QLineEdit(saved.get(key, default_combo))
            field.setFixedWidth(170)
            field.setPlaceholderText("например Ctrl+Alt+F9")
            field.editingFinished.connect(
                lambda k=key, f=field: self._save_hotkey(k, f))
            self.hotkey_fields[key] = field

            hint = ""
            registered = state.get(key)
            if registered is not None and not registered.registered:
                hint = "⚠ " + (registered.error or "Не удалось занять комбинацию.")
            card.add(self._row(title, field, hint))

        card.add(dim_label(
            "Оставь поле пустым, чтобы отключить комбинацию. Изменения "
            "применяются после перезапуска приложения: занять клавишу можно "
            "только один раз при старте."))
        return card

    def _save_hotkey(self, key: str, field):
        from ..hotkeys import parse_combo

        text = field.text().strip()
        if text and parse_combo(text) is None:
            QMessageBox.warning(
                self, "Комбинация не понята",
                "Пиши через плюс: модификаторы Ctrl, Alt, Shift, Win и одна "
                "клавиша — буква, цифра или F1…F12. "
                "Например: Ctrl+Alt+F9")
            return
        saved = dict(self.ctx.config.get("hotkeys") or {})
        saved[key] = text
        self._set("hotkeys", saved)

    # ------------------------------------------------------------ оверлей

    def _overlay_card(self) -> Card:
        card = Card("Оверлей поверх игры")

        host = QLineEdit(str(self.ctx.config.get("overlay_ping_host", "1.1.1.1")))
        host.setFixedWidth(200)
        host.editingFinished.connect(
            lambda: self._set("overlay_ping_host", host.text().strip() or "1.1.1.1"))
        card.add(self._row(
            "Адрес для замера задержки", host,
            "По умолчанию 1.1.1.1 — быстрый публичный адрес. Можно вписать адрес "
            "своего игрового сервера, тогда пинг будет показывать именно его."))

        corner = QComboBox()
        self._corners = [("top-right", "Правый верхний"), ("top-left", "Левый верхний"),
                         ("bottom-right", "Правый нижний"), ("bottom-left", "Левый нижний")]
        corner.addItems([t for _k, t in self._corners])
        saved = self.ctx.config.get("overlay_corner", "top-right")
        ckeys = [k for k, _t in self._corners]
        corner.setCurrentIndex(ckeys.index(saved) if saved in ckeys else 0)
        corner.currentIndexChanged.connect(
            lambda i: self._set("overlay_corner", self._corners[i][0]))
        card.add(self._row("Угол появления", corner,
                           "Оверлей всё равно можно перетащить мышью куда угодно."))

        opacity = QSpinBox()
        opacity.setRange(40, 100)
        opacity.setSuffix(" %")
        opacity.setValue(int(self.ctx.config.get("overlay_opacity", 95)))
        opacity.valueChanged.connect(self._set_opacity)
        card.add(self._row("Непрозрачность", opacity,
                           "Чем ниже, тем меньше оверлей закрывает картинку."))
        return card

    def _set_opacity(self, value: int):
        self._set("overlay_opacity", value)
        window = self.window()
        overlay = getattr(window, "overlay", None)
        if overlay is not None:
            overlay.setWindowOpacity(value / 100)

    # ------------------------------------------------------------ энергия

    def _energy_card(self) -> Card:
        card = Card("Энергия и деньги",
                    "Используется на странице «Энергия» для подсчёта расходов.")

        tariff = QDoubleSpinBox()
        tariff.setRange(0.01, 100.0)
        tariff.setDecimals(2)
        tariff.setSingleStep(0.1)
        tariff.setValue(float(self.ctx.tariff))
        tariff.valueChanged.connect(lambda v: self._set("tariff", v))
        card.add(self._row("Цена киловатт-часа", tariff))

        currency = QLineEdit(str(self.ctx.currency))
        currency.setFixedWidth(80)
        currency.editingFinished.connect(
            lambda: self._set("currency", currency.text().strip() or "₴"))
        card.add(self._row("Обозначение валюты", currency))

        hours = QDoubleSpinBox()
        hours.setRange(0.5, 24.0)
        hours.setDecimals(1)
        hours.setSuffix(" ч")
        hours.setValue(float(self.ctx.config.get("hours_per_day", 6.0)))
        hours.valueChanged.connect(lambda v: self._set("hours_per_day", v))
        card.add(self._row("Часов за компьютером в день", hours,
                           "Нужно для прогноза расхода на месяц."))
        return card

    # ------------------------------------------------------------ обслуживание

    def _maintenance_card(self) -> Card:
        card = Card("Обслуживание по расписанию",
                    "Приложение заводит настоящую задачу в планировщике Windows. "
                    "Она сработает, даже если КРЕМЕНЬ не запущен, и переживёт "
                    "перезагрузку — в памяти круглые сутки висеть не нужно.")

        state = maintenance_core.read_state()
        self.maintenance_state = QLabel(self._maintenance_text(state))
        self.maintenance_state.setObjectName("Dim")
        self.maintenance_state.setWordWrap(True)
        card.add(self.maintenance_state)

        saved_jobs = set(self.ctx.config.get("maintenance_jobs")
                         or ["junk", "recycle", "chronicle"])
        self.job_boxes: dict[str, QCheckBox] = {}
        for key, (title, why, needs_admin) in maintenance_core.JOBS.items():
            box = QCheckBox(title)
            box.setChecked(key in saved_jobs)
            box.toggled.connect(lambda _v, k=key: self._save_jobs())
            self.job_boxes[key] = box
            hint = why + (" Требует прав администратора." if needs_admin else "")
            holder = QWidget()
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(1)
            hl.addWidget(box)
            hl.addWidget(dim_label(hint))
            card.add(holder)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Когда:"))
        self.schedule_box = QComboBox()
        self._schedule_keys = list(maintenance_core.SCHEDULES)
        for key in self._schedule_keys:
            self.schedule_box.addItem(maintenance_core.SCHEDULES[key][0])
        saved = self.ctx.config.get("maintenance_schedule", "weekly")
        if saved in self._schedule_keys:
            self.schedule_box.setCurrentIndex(self._schedule_keys.index(saved))
        row.addWidget(self.schedule_box)
        row.addStretch()

        apply_btn = primary_button("Включить расписание")
        apply_btn.clicked.connect(self._create_task)
        row.addWidget(apply_btn)

        run_btn = ghost_button("Выполнить сейчас")
        run_btn.clicked.connect(self._run_maintenance_now)
        row.addWidget(run_btn)

        off_btn = ghost_button("Отключить")
        off_btn.clicked.connect(self._remove_task)
        row.addWidget(off_btn)
        card.add_layout(row)
        return card

    def _maintenance_text(self, state) -> str:
        if not state.exists:
            return ("Расписание не заведено. Выбери работы и нажми "
                    "«Включить расписание».")
        parts = ["Задача в планировщике заведена."]
        if state.next_run:
            parts.append(f"Следующий запуск: {state.next_run}.")
        if state.last_run:
            parts.append(f"Последний: {state.last_run}.")
        return " ".join(parts)

    def _save_jobs(self):
        jobs = [k for k, box in self.job_boxes.items() if box.isChecked()]
        self._set("maintenance_jobs", jobs)

    def _create_task(self):
        jobs = [k for k, box in self.job_boxes.items() if box.isChecked()]
        schedule = self._schedule_keys[self.schedule_box.currentIndex()]
        self._set("maintenance_schedule", schedule)
        ok, message = maintenance_core.create_task(schedule, jobs)
        QMessageBox.information(self, "Расписание" if ok else "Не удалось", message)
        self.maintenance_state.setText(
            self._maintenance_text(maintenance_core.read_state()))

    def _remove_task(self):
        ok, message = maintenance_core.remove_task()
        QMessageBox.information(self, "Расписание" if ok else "Не удалось", message)
        self.maintenance_state.setText(
            self._maintenance_text(maintenance_core.read_state()))

    def _run_maintenance_now(self):
        jobs = [k for k, box in self.job_boxes.items() if box.isChecked()]
        if not jobs:
            QMessageBox.information(self, "Обслуживание",
                                    "Не выбрано ни одной работы.")
            return
        report = maintenance_core.run_jobs(jobs)
        QMessageBox.information(
            self, "Обслуживание",
            "\n".join(report) or "Делать было нечего.")

    # ------------------------------------------------------------ безопасность

    def _safety_card(self) -> Card:
        card = Card("Безопасность",
                    "Как приложение ведёт себя перед рискованными действиями.")

        confirm = QCheckBox()
        confirm.setChecked(bool(self.ctx.config.get("confirm_risky", True)))
        confirm.toggled.connect(lambda v: self._set("confirm_risky", v))
        card.add(self._row(
            "Спрашивать перед настройками с высоким риском", confirm,
            "Выключать не советую: именно это окно объясняет цену настройки."))

        point = QCheckBox()
        point.setChecked(bool(self.ctx.config.get("auto_restore_point", True)))
        point.toggled.connect(lambda v: self._set("auto_restore_point", v))
        card.add(self._row(
            "Предлагать точку восстановления перед экспертными твиками", point,
            "Точка восстановления Windows — последняя линия обороны, если "
            "система перестанет загружаться."))
        return card

    # ------------------------------------------------------------ данные

    def _data_card(self) -> Card:
        card = Card("Данные приложения")
        usage = settings_core.data_usage()

        self.usage_label = QLabel(
            f"Папка: {usage['folder']}\n"
            f"Всего {usage['total_text']} · журнал {usage['log_text']} · "
            f"снимков {usage['snapshots']} на {usage['snapshots_text']}")
        self.usage_label.setObjectName("Dim")
        self.usage_label.setWordWrap(True)
        card.add(self.usage_label)

        row = QHBoxLayout()
        row.setSpacing(10)
        open_btn = ghost_button("Открыть папку")
        open_btn.clicked.connect(settings_core.open_data_folder)
        row.addWidget(open_btn)

        clear_btn = ghost_button("Очистить журнал")
        clear_btn.clicked.connect(self._clear_log)
        row.addWidget(clear_btn)

        export_btn = ghost_button("Выгрузить настройки")
        export_btn.clicked.connect(self._export)
        row.addWidget(export_btn)

        import_btn = ghost_button("Загрузить настройки")
        import_btn.clicked.connect(self._import)
        row.addWidget(import_btn)

        reset_btn = ghost_button("Сбросить к заводским")
        reset_btn.setObjectName("Danger")
        reset_btn.clicked.connect(self._reset)
        row.addWidget(reset_btn)
        row.addStretch()
        card.add_layout(row)

        card.add(dim_label(
            "Выгрузка складывает конфиг и снимки в один архив — удобно перенести "
            "настройки на другой компьютер или к другу. Сброс не трогает снимки "
            "системы: без них нечем откатывать изменения в Windows."))
        return card

    def _refresh_usage(self):
        usage = settings_core.data_usage()
        self.usage_label.setText(
            f"Папка: {usage['folder']}\n"
            f"Всего {usage['total_text']} · журнал {usage['log_text']} · "
            f"снимков {usage['snapshots']} на {usage['snapshots_text']}")

    def _clear_log(self):
        ok, message = settings_core.clear_log()
        self._refresh_usage()
        QMessageBox.information(self, "Журнал", message)

    def _export(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, "Куда сохранить настройки", "kremen-настройки.zip",
            "Архив (*.zip)")
        if not path:
            return
        ok, message = settings_core.export_config(path)
        QMessageBox.information(self, "Выгрузка" if ok else "Ошибка", message)

    def _import(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Выбери архив настроек", "", "Архив (*.zip)")
        if not path:
            return
        ok, message = settings_core.import_config(path)
        QMessageBox.information(self, "Загрузка" if ok else "Ошибка", message)

    def _reset(self):
        box = QMessageBox(self)
        box.setWindowTitle("Сброс настроек")
        box.setIcon(QMessageBox.Warning)
        box.setText("Сбросить настройки приложения к заводским?")
        box.setInformativeText(
            "Настройки Windows, которые ты применял, НЕ откатятся — для этого "
            "есть кнопки отката на страницах оптимизации. Снимки системы "
            "сохранятся.")
        yes = box.addButton("Сбросить", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if box.clickedButton() is not yes:
            return
        ok, message = settings_core.reset_all(keep_snapshots=True)
        QMessageBox.information(self, "Сброс" if ok else "Ошибка", message)

    def _toggle_autostart(self, enabled: bool):
        ok, message = settings_core.set_autostart(enabled)
        if not ok:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not enabled)
            self.autostart.blockSignals(False)
        QMessageBox.information(self, "Автозапуск", message)
