"""Главная страница: живой мониторинг, оценка системы и режим «Турбо»."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QDialog,
    QDialogButtonBox, QCheckBox, QTextEdit, QMessageBox, QFrame, QComboBox,
)

from core import autopilot, boost, optimizer
from core import advisor as advisor_core
from core import telemetry as telemetry_core
from core.context import AppContext
from core.tuner import system_score
from core.util import human_bytes, is_admin, log

from .. import theme
from ..widgets import (
    Card, Gauge, StatTile, KeyValue, Badge, SectionTitle, Collapsible,
    page_header, scroll_page, primary_button, ghost_button, dim_label, glow,
    clear_layout,
)


class BoostWorker(QThread):
    """Выполняет «Турбо» в фоне, чтобы интерфейс не подвисал."""
    step = Signal(str)
    done = Signal(list)

    def __init__(self, ctx: AppContext, close_apps: list[str], restore: bool = False):
        super().__init__()
        self.ctx = ctx
        self.close_apps = close_apps
        self.restore = restore

    def run(self):
        if self.restore:
            steps = boost.restore_boost(self.ctx)
        else:
            steps = boost.run_boost(self.ctx, self.close_apps, progress=self.step.emit)
        self.done.emit(steps)


class BoostDialog(QDialog):
    """Спрашивает, какие фоновые программы закрыть перед игрой."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Режим «Турбо»")
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.STYLESHEET)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)

        title = QLabel("Что сделать перед игрой")
        title.setObjectName("CardTitle")
        lay.addWidget(title)
        lay.addWidget(dim_label(
            "Будут применены рекомендованные настройки Windows, включён максимальный "
            "план питания и очищена память. Прежнее состояние сохранится — вернуть "
            "всё можно одной кнопкой."))

        self.boxes: list[tuple[QCheckBox, str]] = []
        rows = boost.heavy_background()
        if rows:
            lay.addWidget(SectionTitle("Фоновые программы — можно закрыть"))
            for row in rows:
                box = QCheckBox(f"{row['title']} — {row['mb']} МБ")
                box.setChecked(row["mb"] > 300)
                lay.addWidget(box)
                self.boxes.append((box, row["process"]))
        else:
            lay.addWidget(dim_label("Тяжёлых фоновых программ не найдено — память уже свободна."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Поехали")
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def selected(self) -> list[str]:
        return [proc for box, proc in self.boxes if box.isChecked()]


class DashboardPage(QWidget):
    """Обзор системы в реальном времени."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.worker: BoostWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        m = ctx.machine
        lay.addWidget(page_header(
            "Обзор системы",
            f"{m.cpu_name} · {m.gpu.name} · {m.ram_gb:.0f} ГБ {m.ram_type} · {m.resolution}"))

        lay.addWidget(self._build_advisor_card())
        lay.addWidget(self._build_score_card())
        lay.addWidget(self._build_gauges())
        lay.addLayout(self._build_tiles())
        lay.addWidget(self._build_history())
        lay.addWidget(self._build_sessions())
        lay.addWidget(self._build_specs())
        lay.addStretch()

    # ------------------------------------------------------------ советник

    def _build_advisor_card(self) -> Card:
        """Короткий человеческий ответ на «что мне сделать прямо сейчас»."""
        card = Card()
        head = QHBoxLayout()
        title = QLabel("Что стоит сделать")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch()
        refresh = ghost_button("Проверить заново")
        refresh.clicked.connect(self.refresh_advice)
        head.addWidget(refresh)
        card.add_layout(head)

        self.advice_summary = QLabel("Смотрю на компьютер…")
        self.advice_summary.setObjectName("Dim")
        self.advice_summary.setWordWrap(True)
        card.add(self.advice_summary)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(8)
        card.add_layout(self.advice_box)
        self._advice_built = False
        return card

    def refresh_advice(self):
        """Пересобирает советы. Дёшево: все данные уже есть в памяти."""
        try:
            items = advisor_core.build_advice(self.ctx, self.ctx.last_reading)
        except Exception as exc:
            log(f"Советник не смог собрать советы: {exc}")
            return
        self.advice_summary.setText(advisor_core.summary_line(items))
        clear_layout(self.advice_box)

        tones = {"accent": theme.ACCENT, "ok": theme.OK,
                 "warn": theme.WARN, "danger": theme.DANGER}
        for item in items[:5]:
            color = tones.get(item.tone, theme.ACCENT)
            block = QFrame()
            block.setObjectName("CardFlat")
            block.setStyleSheet(
                f"QFrame#CardFlat {{ background: {theme.SURFACE_2}; "
                f"border: 1px solid {theme.BORDER}; border-left: 3px solid {color}; "
                "border-radius: 10px; }")
            bl = QVBoxLayout(block)
            bl.setContentsMargins(14, 11, 14, 11)
            bl.setSpacing(4)

            top = QHBoxLayout()
            name = QLabel(f"{item.icon}   {item.title}")
            name.setStyleSheet("font-size: 13px; font-weight: 700; background: transparent;")
            top.addWidget(name)
            top.addStretch()
            if item.weight == advisor_core.CRITICAL:
                top.addWidget(Badge("срочно", theme.DANGER))
            elif item.weight == advisor_core.IMPORTANT:
                top.addWidget(Badge("важно", theme.WARN))
            if item.action_label and item.action_page:
                go = ghost_button(item.action_label)
                go.clicked.connect(
                    lambda _c=False, page=item.action_page: self._goto(page))
                top.addWidget(go)
            bl.addLayout(top)

            text = QLabel(item.text)
            text.setObjectName("Dim")
            text.setWordWrap(True)
            text.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px; "
                               "background: transparent;")
            bl.addWidget(text)
            self.advice_box.addWidget(block)

        if len(items) > 5:
            more = QLabel(f"…и ещё {len(items) - 5}. Показаны самые важные.")
            more.setObjectName("Faint")
            self.advice_box.addWidget(more)
        self._advice_built = True

    def _goto(self, page_key: str):
        window = self.window()
        goto = getattr(window, "_goto", None)
        if callable(goto):
            goto(page_key)

    # ------------------------------------------------------------ блоки

    def _build_score_card(self) -> Card:
        card = Card()
        score, label = system_score(self.ctx.machine)

        row = QHBoxLayout()
        row.setSpacing(22)

        left = QVBoxLayout()
        left.setSpacing(2)
        num = QLabel(str(score))
        num.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {theme.ACCENT};")
        left.addWidget(num)
        cls = QLabel(label)
        cls.setStyleSheet("font-size: 15px; font-weight: 600;")
        left.addWidget(cls)
        left.addWidget(QLabel("Оценка сборки из 100", objectName="Faint"))
        row.addLayout(left)

        mid = QVBoxLayout()
        mid.setSpacing(7)
        m = self.ctx.machine
        target = "1440p" if m.megapixels > 3 else ("4K" if m.megapixels > 6 else "1080p")
        mid.addWidget(QLabel(
            f"Комфортное разрешение для этой сборки — {target} при {m.refresh} Гц.",
            objectName="Dim", wordWrap=True))
        badges = QHBoxLayout()
        badges.setSpacing(6)
        badges.addWidget(Badge(f"Монитор {m.refresh} Гц", theme.ACCENT))
        badges.addWidget(Badge(f"Видеопамять {m.gpu.vram_mb // 1024} ГБ",
                               theme.OK if m.gpu.vram_mb >= 10240 else theme.WARN))
        badges.addWidget(Badge("Двухканал" if m.ram_sticks >= 2 else "Одноканал!",
                               theme.OK if m.ram_sticks >= 2 else theme.DANGER))
        badges.addWidget(Badge("SSD" if m.system_disk_ssd else "HDD",
                               theme.OK if m.system_disk_ssd else theme.WARN))
        badges.addWidget(Badge("Администратор" if is_admin() else "Без прав админа",
                               theme.OK if is_admin() else theme.WARN))
        badges.addStretch()
        mid.addLayout(badges)
        row.addLayout(mid, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.boost_btn = primary_button("⚡  ТУРБО-РЕЖИМ")
        self.boost_btn.setMinimumWidth(200)
        self.boost_btn.clicked.connect(self.on_boost)
        glow(self.boost_btn, theme.ACCENT, 30)
        right.addWidget(self.boost_btn)

        self.restore_btn = ghost_button("Вернуть как было")
        self.restore_btn.clicked.connect(self.on_restore)
        self.restore_btn.setEnabled(optimizer.load_snapshot(boost.SNAPSHOT_BOOST) is not None)
        right.addWidget(self.restore_btn)
        row.addLayout(right)

        card.add_layout(row)
        self.boost_status = QLabel("")
        self.boost_status.setObjectName("Faint")
        self.boost_status.setWordWrap(True)
        card.add(self.boost_status)
        return card

    def _build_gauges(self) -> Card:
        card = Card("Датчики в реальном времени")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.g_cpu = Gauge("Процессор")
        self.g_gpu = Gauge("Видеокарта")
        self.g_ram = Gauge("Память")
        self.g_temp_gpu = Gauge("Темп. GPU", "°")
        self.g_temp_gpu.maximum = 100
        self.g_disk = Gauge("Диск C:")
        for g in (self.g_cpu, self.g_gpu, self.g_ram, self.g_temp_gpu, self.g_disk):
            row.addWidget(g)
        card.add_layout(row)
        return card

    def _build_tiles(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(12)
        self.t_cpu = StatTile("Загрузка процессора")
        self.t_gpu = StatTile("Загрузка видеокарты")
        self.t_ram = StatTile("Занято памяти", unit="%")
        self.t_power = StatTile("Потребление всего ПК", unit=" Вт")
        for i, tile in enumerate((self.t_cpu, self.t_gpu, self.t_ram, self.t_power)):
            grid.addWidget(tile, 0, i)
        return grid

    def _build_history(self) -> Collapsible:
        """Долговременная история: как компьютер жил всю неделю, а не минуту."""
        section = Collapsible(
            "История наблюдений",
            "Раз в минуту приложение записывает сжатый срез датчиков. "
            "Это отвечает на вопросы, которых не видно на живых графиках: "
            "когда компьютер грелся сильнее всего и сколько он ест под нагрузкой.",
            opened=False)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(QLabel("Период:"))
        self.history_range = QComboBox()
        self._history_days = [1, 3, 7, 30]
        for days in self._history_days:
            self.history_range.addItem(
                {1: "сутки", 3: "три дня", 7: "неделя", 30: "месяц"}[days])
        self.history_range.setCurrentIndex(2)
        self.history_range.currentIndexChanged.connect(self.refresh_history)
        controls.addWidget(self.history_range)
        controls.addStretch()
        refresh = ghost_button("Обновить")
        refresh.clicked.connect(self.refresh_history)
        controls.addWidget(refresh)
        holder = QWidget()
        holder.setLayout(controls)
        section.add(holder)

        self.history_summary = QLabel("Открой раздел, чтобы посмотреть историю.")
        self.history_summary.setObjectName("Dim")
        self.history_summary.setWordWrap(True)
        section.add(self.history_summary)

        self.history_body = QVBoxLayout()
        self.history_body.setSpacing(8)
        wrapper = QWidget()
        wrapper.setLayout(self.history_body)
        section.add(wrapper)

        section.head.clicked.connect(self._on_history_opened)
        self._history_loaded = False
        return section

    def _on_history_opened(self):
        if not self._history_loaded:
            self.refresh_history()

    def refresh_history(self):
        """Перечитывает историю за выбранный период."""
        self._history_loaded = True
        days = self._history_days[self.history_range.currentIndex()]
        try:
            minutes = telemetry_core.load_range(days)
            summary = telemetry_core.summarize(minutes)
            breakdown = telemetry_core.daily_breakdown(minutes)
        except Exception as exc:
            log(f"История наблюдений не прочиталась: {exc}")
            return

        self.history_summary.setText(" ".join(summary.notes))
        clear_layout(self.history_body)
        if not breakdown:
            return

        head = QHBoxLayout()
        for title in ("День", "Часов", "ЦП", "GPU", "Пик GPU °C", "Пик Вт", "кВт·ч"):
            label = QLabel(title)
            label.setObjectName("Faint")
            label.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px; "
                                "font-weight: 700;")
            head.addWidget(label, 1)
        head_holder = QWidget()
        head_holder.setLayout(head)
        self.history_body.addWidget(head_holder)

        for row in breakdown[-14:]:
            line = QHBoxLayout()
            temp = row["gpu_temp_peak"]
            cells = [
                (row["day"], theme.TEXT),
                (f"{row['hours']}", theme.TEXT_DIM),
                (f"{row['cpu']:.0f}%", theme.load_color(row["cpu"])),
                (f"{row['gpu']:.0f}%", theme.load_color(row["gpu"])),
                (f"{temp}" if temp else "—", theme.temp_color(temp or 0)),
                (f"{row['watts_peak']}" if row["watts_peak"] else "—", theme.ACCENT_2),
                (f"{row['kwh']}", theme.TEXT_DIM),
            ]
            for text, color in cells:
                label = QLabel(text)
                label.setStyleSheet(f"color: {color}; font-size: 12px;")
                line.addWidget(label, 1)
            holder = QWidget()
            holder.setLayout(line)
            self.history_body.addWidget(holder)

    def _build_sessions(self) -> Card:
        """Дневник игр: его наполняет автопилот, пока ты играешь."""
        card = Card("Дневник игр",
                    "Автопилот сам записывает, во что и сколько ты играл, "
                    "как грелось железо и сколько при этом ел компьютер.")
        self.sessions_box = QVBoxLayout()
        card.add_layout(self.sessions_box)
        self._refresh_sessions()
        return card

    def _refresh_sessions(self):
        clear_layout(self.sessions_box)
        rows = autopilot.stats_by_game()
        if not rows:
            hint = QLabel(
                "Пока пусто. Включи автопилот на странице «Профили» — "
                "он будет замечать запуск игр и вести дневник сам.")
            hint.setObjectName("Dim")
            hint.setWordWrap(True)
            self.sessions_box.addWidget(hint)
            return

        header = QHBoxLayout()
        for title, width in (("Игра", 0), ("Часов", 90), ("Запусков", 90),
                             ("Энергия", 110), ("Пик GPU", 90)):
            label = QLabel(title)
            label.setObjectName("SectionHeader")
            if width:
                label.setMinimumWidth(width)
                label.setAlignment(Qt.AlignRight)
            header.addWidget(label, 1 if not width else 0)
        self.sessions_box.addLayout(header)

        total_hours = sum(r["hours"] for r in rows)
        total_kwh = sum(r["kwh"] for r in rows)
        for row in rows[:8]:
            line = QHBoxLayout()
            name = QLabel(row["game"])
            name.setStyleSheet("font-weight: 600;")
            line.addWidget(name, 1)
            for value, color, width in (
                    (f"{row['hours']:.1f}", theme.ACCENT, 90),
                    (str(row["sessions"]), theme.TEXT_DIM, 90),
                    (f"{row['kwh']:.2f} кВт·ч", theme.ACCENT_2, 110),
                    (f"{row['max_temp']:.0f} °C" if row["max_temp"] else "—",
                     theme.temp_color(row["max_temp"]), 90)):
                cell = QLabel(value)
                cell.setStyleSheet(f"color: {color}; font-weight: 600;")
                cell.setAlignment(Qt.AlignRight)
                cell.setMinimumWidth(width)
                line.addWidget(cell)
            self.sessions_box.addLayout(line)

        total = QLabel(
            f"Всего наиграно {total_hours:.1f} ч, израсходовано {total_kwh:.2f} кВт·ч "
            f"— это примерно {self.ctx.money(total_kwh * self.ctx.tariff)}.")
        total.setObjectName("Faint")
        total.setWordWrap(True)
        self.sessions_box.addWidget(total)

    def _build_specs(self) -> Card:
        m = self.ctx.machine
        card = Card("Конфигурация")
        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        grid.setVerticalSpacing(0)

        left = [
            ("Процессор", m.cpu_name),
            ("Ядра / потоки", f"{m.cores} / {m.threads}"),
            ("Базовая частота", f"{m.cpu_base_mhz} МГц"),
            ("Материнская плата", f"{m.board_vendor} {m.board}".strip()),
            ("Система", m.os_name or "—"),
        ]
        right = [
            ("Видеокарта", m.gpu.name),
            ("Видеопамять", f"{m.gpu.vram_mb} МБ"),
            ("Драйвер", m.gpu.driver or "—"),
            ("Память", f"{m.ram_gb:.0f} ГБ {m.ram_type} {m.ram_speed} МГц "
                       f"({m.ram_sticks} планк{'и' if m.ram_sticks in (2, 3, 4) else 'а'})"),
            ("Экран", f"{m.resolution} @ {m.refresh} Гц"),
        ]
        for i, (k, v) in enumerate(left):
            grid.addWidget(KeyValue(k, v), i, 0)
        for i, (k, v) in enumerate(right):
            grid.addWidget(KeyValue(k, v), i, 1)
        card.add_layout(grid)

        if m.disks:
            card.add(SectionTitle("Накопители"))
            for d in m.disks:
                kind = "SSD" if "SSD" in d["type"] else ("HDD" if d["type"] else "—")
                card.add(KeyValue(d["name"] or "Диск", f"{d['gb']} ГБ · {kind}"))
        return card

    # ------------------------------------------------------------ обновление

    def tick(self, reading: dict):
        """Вызывается таймером главного окна."""
        if not getattr(self, "_advice_built", False):
            self.refresh_advice()

        cpu = reading.get("cpu_load") or 0
        gpu = reading.get("gpu_load")
        ram = reading.get("ram_percent") or 0
        gtemp = reading.get("gpu_temp")
        disk = reading.get("disk_percent") or 0

        self.g_cpu.set_value(cpu, theme.load_color(cpu), f"ЦП · {reading.get('cpu_mhz', 0)} МГц")
        if gpu is None:
            self.g_gpu.set_value(0, theme.TEXT_FAINT, "GPU · нет данных")
        else:
            self.g_gpu.set_value(gpu, theme.load_color(gpu),
                                 f"GPU · {reading.get('gpu_mhz') or 0} МГц")
        self.g_ram.set_value(ram, theme.load_color(ram),
                             f"{reading.get('ram_used_gb', 0)} / {reading.get('ram_total_gb', 0)} ГБ")
        if gtemp is None:
            self.g_temp_gpu.set_value(0, theme.TEXT_FAINT, "нет данных")
        else:
            self.g_temp_gpu.set_value(gtemp, theme.temp_color(gtemp),
                                      f"вентилятор {reading.get('gpu_fan') or 0}%")
        self.g_disk.set_value(disk, theme.load_color(disk),
                              f"свободно {reading.get('disk_free_gb', 0)} ГБ")

        self.t_cpu.update_value(cpu, theme.load_color(cpu),
                                f"{reading.get('processes', 0)} процессов")
        self.t_gpu.update_value(gpu, theme.load_color(gpu or 0),
                                f"{reading.get('gpu_mem_used') or 0} / "
                                f"{reading.get('gpu_mem_total') or 0} МБ" if gpu is not None else "")
        self.t_ram.update_value(ram, theme.load_color(ram),
                                f"{reading.get('ram_used_gb', 0)} ГБ занято")
        # общая мощность из розетки: её считает главное окно
        wall = reading.get("wall_w")
        gpu_power = reading.get("gpu_power")
        self.t_power.update_value(
            wall, theme.ACCENT_2,
            f"видеокарта {gpu_power:.0f} Вт" if gpu_power else "")

    # ------------------------------------------------------------ действия

    def on_boost(self):
        dialog = BoostDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.boost_btn.setEnabled(False)
        self.boost_btn.setText("Работаю…")
        self.worker = BoostWorker(self.ctx, dialog.selected())
        self.worker.step.connect(lambda text: self.boost_status.setText(text))
        self.worker.done.connect(self.on_boost_done)
        self.worker.start()

    def on_boost_done(self, steps: list[str]):
        self.boost_btn.setEnabled(True)
        self.boost_btn.setText("⚡  ТУРБО-РЕЖИМ")
        self.restore_btn.setEnabled(True)
        self.boost_status.setText("Готово. " + steps[-1] if steps else "Готово.")
        self._show_report("Режим «Турбо» включён", steps)

    def on_restore(self):
        self.restore_btn.setEnabled(False)
        self.worker = BoostWorker(self.ctx, [], restore=True)
        self.worker.done.connect(self.on_restore_done)
        self.worker.start()

    def on_restore_done(self, steps: list[str]):
        self.restore_btn.setEnabled(True)
        self._show_report("Настройки возвращены", steps)

    def _show_report(self, title: str, steps: list[str]):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(560)
        dialog.setStyleSheet(theme.STYLESHEET)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(22, 20, 22, 20)
        head = QLabel(title)
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(f"• {s}" for s in steps))
        text.setMinimumHeight(200)
        lay.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        lay.addWidget(buttons)
        dialog.exec()
