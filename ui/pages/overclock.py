"""Страница разгона: видеокарта, процессор и контроль стабильности."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QMessageBox,
    QGridLayout, QButtonGroup, QPushButton, QComboBox, QSpinBox, QProgressBar,
)

from core import overclock
from core.autooc import SmartOverclock
from core.stress import MODES as STRESS_MODES, StressTest
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, Sparkline,
)


class ActionWorker(QThread):
    """Выполняет команду разгона в фоне."""
    done = Signal(bool, str)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            ok, msg = self.func(*self.args)
        except Exception as exc:
            ok, msg = False, str(exc)
        self.done.emit(ok, msg)


class OverclockPage(QWidget):
    """Разгон и тонкая настройка."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.caps = overclock.read_gpu_caps()
        self.watch = overclock.StabilityWatch()
        self.autooc = SmartOverclock()
        self.stress = StressTest(ctx)
        ctx.autooc = self.autooc
        self.worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Разгон и тонкая настройка",
            "Приложение само меняет только то, что официально поддерживают драйвер "
            "и Windows. Смещения частот считаются и выдаются планом — вслепую их "
            "не применяет ни одна честная программа."))

        lay.addWidget(self._warning_card())
        lay.addWidget(self._gpu_card())
        lay.addWidget(self._autooc_card())
        lay.addWidget(self._power_card())
        lay.addWidget(self._profiles_card())
        lay.addWidget(self._stress_card())
        lay.addWidget(self._stability_card())
        lay.addWidget(self._cpu_card())
        lay.addWidget(self._advice_card())
        lay.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_watch)
        self.timer.start(1000)

    # ------------------------------------------------------------ карточки

    def _warning_card(self) -> Card:
        card = Card()
        card.body.setSpacing(6)
        head = QLabel("⚠  Прочитай до начала")
        head.setStyleSheet(f"color: {theme.WARN}; font-size: 15px; font-weight: 700;")
        card.add(head)
        card.add(dim_label(
            "Разгон видеокарты — это выход за заводские режимы. Риск не в том, что карта "
            "сгорит: современные карты защищены и просто сбросят частоты или уронят драйвер. "
            "Риск в том, что нестабильный разгон даёт редкие вылеты и повреждённые "
            "сохранения в играх. Поэтому проверяй каждый шаг и не торопись.\n\n"
            "Гарантию разгон формально не отменяет у большинства производителей, "
            "но менять напряжение через сторонние прошивки — уже другая история, "
            "и этого приложение не делает."))
        return card

    def _gpu_card(self) -> Card:
        card = Card("Видеокарта", self.ctx.machine.gpu.name)
        if not self.caps.available:
            card.add(dim_label(self.caps.note or "Данные о видеокарте недоступны."))
            return card

        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        rows = [
            ("Текущий предел мощности", f"{self.caps.power_now:.0f} Вт"),
            ("Заводской предел", f"{self.caps.power_default:.0f} Вт"),
            ("Диапазон", f"{self.caps.power_min:.0f} — {self.caps.power_max:.0f} Вт"),
            ("Максимальная частота ядра", f"{self.caps.max_core_mhz} МГц"),
            ("Максимальная частота памяти", f"{self.caps.max_mem_mhz} МГц"),
            ("Температура сейчас", f"{self.caps.temp} °C"),
        ]
        for i, (k, v) in enumerate(rows):
            grid.addWidget(KeyValue(k, v), i % 3, i // 3)
        card.add_layout(grid)

        if self.caps.note:
            note = QLabel("ℹ  " + self.caps.note)
            note.setObjectName("Dim")
            note.setWordWrap(True)
            card.add(note)
        return card

    def _power_card(self) -> Card:
        card = Card("Предел мощности",
                    "Единственный параметр разгона, который приложение меняет само — "
                    "через официальный интерфейс драйвера NVIDIA.")
        if not self.caps.available:
            card.add(dim_label("Недоступно для этой видеокарты."))
            return card

        row = QHBoxLayout()
        self.power_slider = QSlider(Qt.Horizontal)
        self.power_slider.setMinimum(int(self.caps.power_min))
        self.power_slider.setMaximum(int(self.caps.power_max))
        self.power_slider.setValue(int(self.caps.power_now))
        self.power_slider.valueChanged.connect(self._on_power_slide)
        row.addWidget(self.power_slider, 1)

        self.power_label = QLabel(f"{self.caps.power_now:.0f} Вт")
        self.power_label.setStyleSheet(
            f"font-size: 19px; font-weight: 700; color: {theme.ACCENT}; min-width: 80px;")
        row.addWidget(self.power_label)
        card.add_layout(row)

        self.power_hint = QLabel("")
        self.power_hint.setObjectName("Faint")
        self.power_hint.setWordWrap(True)
        card.add(self.power_hint)
        self._on_power_slide(int(self.caps.power_now))

        buttons = QHBoxLayout()
        apply_btn = primary_button("Применить предел мощности")
        apply_btn.clicked.connect(self._apply_power)
        buttons.addWidget(apply_btn)

        reset_btn = ghost_button("Вернуть заводской")
        reset_btn.clicked.connect(self._reset_power)
        buttons.addWidget(reset_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.power_status = QLabel("")
        self.power_status.setObjectName("Dim")
        self.power_status.setWordWrap(True)
        card.add(self.power_status)
        return card

    def _profiles_card(self) -> Card:
        card = Card("Профили разгона", "Три сценария под разные задачи.")
        profiles = overclock.gpu_profiles(self.ctx.machine, self.caps)
        row = QHBoxLayout()
        row.setSpacing(12)
        for prof in profiles:
            block = Card(flat=True)
            block.body.setSpacing(6)

            head = QHBoxLayout()
            title = QLabel(prof.title)
            title.setStyleSheet("font-size: 15px; font-weight: 700;")
            head.addWidget(title)
            head.addStretch()
            color = {"нулевой": theme.OK, "низкий": theme.OK,
                     "средний": theme.WARN}.get(prof.risk, theme.WARN)
            head.addWidget(Badge(f"риск: {prof.risk}", color))
            block.add_layout(head)

            block.add(dim_label(prof.subtitle))

            values = QLabel(
                f"Ядро <b>+{prof.core_offset}</b> МГц · Память <b>+{prof.mem_offset}</b> МГц · "
                f"Мощность <b>{prof.power_percent}%</b> · Лимит <b>{prof.temp_limit} °C</b>")
            values.setStyleSheet(f"color: {theme.ACCENT}; font-size: 12px;")
            values.setWordWrap(True)
            block.add(values)

            block.add(QLabel(prof.expected, objectName="Faint", wordWrap=True))

            block.add(SectionTitle("Пошагово"))
            for i, step in enumerate(prof.steps, 1):
                item = QLabel(f"{i}. {step}")
                item.setObjectName("Faint")
                item.setWordWrap(True)
                block.add(item)
            row.addWidget(block, 1)
        card.add_layout(row)

        tools = QHBoxLayout()
        if overclock.afterburner_path():
            btn = ghost_button("Открыть MSI Afterburner")
            btn.clicked.connect(lambda: overclock.launch_afterburner())
            tools.addWidget(btn)
        else:
            tools.addWidget(dim_label(
                "Для смещения частот нужен MSI Afterburner — он бесплатный "
                "и работает с картами любого производителя."))
        tools.addStretch()
        card.add_layout(tools)
        return card

    def _stability_card(self) -> Card:
        card = Card(
            "Контроль стабильности",
            "Запусти игру или стресс-тест, а этот наблюдатель соберёт температуры, "
            "частоты и причины сброса частот, а потом объяснит результат.")

        row = QHBoxLayout()
        self.watch_btn = primary_button("Начать наблюдение")
        self.watch_btn.clicked.connect(self._toggle_watch)
        row.addWidget(self.watch_btn)
        self.watch_state = QLabel("Не запущено.")
        self.watch_state.setObjectName("Dim")
        row.addWidget(self.watch_state, 1)
        card.add_layout(row)

        self.temp_chart = Sparkline(120, theme.WARN)
        self.temp_chart.maximum = 100
        card.add(self.temp_chart)

        self.watch_result = QLabel("")
        self.watch_result.setObjectName("Dim")
        self.watch_result.setWordWrap(True)
        card.add(self.watch_result)
        return card

    def _cpu_card(self) -> Card:
        card = Card("Процессор",
                    "Разогнать процессор можно только в BIOS, но политику питания "
                    "Windows приложение переключает само.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.cpu_group = QButtonGroup(self)
        modes = [
            ("performance", "Максимум", "Турбо всегда, парковка ядер отключена"),
            ("balanced", "Баланс", "Заводское поведение Windows"),
            ("quiet", "Тихий", "Ниже частоты и температуры, тише кулер"),
        ]
        for key, title, desc in modes:
            btn = QPushButton(f"{title}\n{desc}")
            btn.setObjectName("Preset")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(64)
            btn.clicked.connect(lambda _c=False, k=key: self._apply_cpu(k))
            self.cpu_group.addButton(btn)
            row.addWidget(btn)
        card.add_layout(row)

        self.cpu_status = QLabel("")
        self.cpu_status.setObjectName("Dim")
        self.cpu_status.setWordWrap(True)
        card.add(self.cpu_status)
        return card

    def _advice_card(self) -> Card:
        card = Card("Что даст больше, чем разгон видеокарты")
        for title, text in overclock.cpu_advice(self.ctx.machine):
            block = QLabel(f"<b>{title}</b><br>{text}")
            block.setObjectName("Dim")
            block.setWordWrap(True)
            card.add(block)
        return card

    # ------------------------------------------------------------ действия

    def _on_power_slide(self, value: int):
        self.power_label.setText(f"{value} Вт")
        default = self.caps.power_default or value
        percent = value / default * 100 if default else 100
        if percent < 90:
            hint = (f"{percent:.0f}% от заводского. Карта станет холоднее и тише, "
                    "потеря кадров обычно 3-7%.")
            color = theme.OK
        elif percent <= 101:
            hint = "Заводской режим."
            color = theme.TEXT_DIM
        else:
            hint = (f"{percent:.0f}% от заводского. Больше мощности — выше температура "
                    "и шум. Следи за нагревом.")
            color = theme.WARN
        self.power_hint.setText(hint)
        self.power_hint.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _apply_power(self):
        value = self.power_slider.value()
        self.power_status.setText("Применяю…")
        self.worker = ActionWorker(overclock.apply_power_limit, value)
        self.worker.done.connect(lambda ok, msg: self.power_status.setText(
            ("✔  " if ok else "✖  ") + msg))
        self.worker.start()

    def _reset_power(self):
        self.power_slider.setValue(int(self.caps.power_default))
        self._apply_power()

    def _apply_cpu(self, mode: str):
        self.cpu_status.setText("Применяю политику процессора…")
        self.worker = ActionWorker(overclock.apply_cpu_profile, mode)
        self.worker.done.connect(lambda ok, msg: self.cpu_status.setText(
            ("✔  " if ok else "✖  ") + msg))
        self.worker.start()

    def _toggle_watch(self):
        if self.watch.running:
            report = self.watch.stop()
            self.watch_btn.setText("Начать наблюдение")
            self.watch_state.setText("Наблюдение остановлено.")
            self._render_report(report)
        else:
            self.watch.start()
            self.watch_btn.setText("Остановить и показать результат")
            self.watch_state.setText("Идёт наблюдение — запускай игру или стресс-тест.")
            self.watch_result.setText("")

    def _update_watch(self):
        self._update_autooc()
        self._update_stress()
        if not self.watch.running:
            return
        if self.watch.temps:
            last = self.watch.temps[-1]
            self.temp_chart.push(last)
            self.watch_state.setText(
                f"Идёт наблюдение · {len(self.watch.temps)} замеров · "
                f"сейчас {last} °C, максимум {max(self.watch.temps)} °C")

    # ------------------------------------------------------------ стресс-тест

    def _stress_card(self) -> Card:
        card = Card("🧪  Проверка стабильности",
                    "Обычный «тест» просто греет железо. Здесь считается задача "
                    "с заранее известным ответом: если результат хоть раз не сойдётся, "
                    "значит железо ошибается под нагрузкой. Именно так выглядят "
                    "вылеты игр «на ровном месте» после разгона или XMP на пределе.")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Режим:"))
        self.stress_mode = QComboBox()
        self._stress_keys = list(STRESS_MODES)
        for key in self._stress_keys:
            self.stress_mode.addItem(STRESS_MODES[key][0])
        self.stress_mode.setCurrentIndex(self._stress_keys.index("mixed"))
        self.stress_mode.currentIndexChanged.connect(self._stress_mode_hint)
        row.addWidget(self.stress_mode)

        row.addWidget(QLabel("Минут:"))
        self.stress_minutes = QSpinBox()
        self.stress_minutes.setRange(1, 120)
        self.stress_minutes.setValue(5)
        row.addWidget(self.stress_minutes)
        row.addStretch()

        self.stress_btn = primary_button("Запустить проверку")
        self.stress_btn.clicked.connect(self._toggle_stress)
        row.addWidget(self.stress_btn)
        card.add_layout(row)

        self.stress_hint = QLabel(STRESS_MODES["mixed"][1])
        self.stress_hint.setObjectName("Faint")
        self.stress_hint.setWordWrap(True)
        card.add(self.stress_hint)

        self.stress_bar = QProgressBar()
        self.stress_bar.setVisible(False)
        card.add(self.stress_bar)

        self.stress_state = QLabel("")
        self.stress_state.setObjectName("Dim")
        self.stress_state.setWordWrap(True)
        card.add(self.stress_state)

        self.stress_result = QLabel("")
        self.stress_result.setWordWrap(True)
        self.stress_result.setTextFormat(Qt.RichText)
        card.add(self.stress_result)

        card.add(dim_label(
            "Видеокарту этот тест не нагружает: без собственного трёхмерного "
            "движка сделать это честно нельзя. Для неё есть наблюдение за "
            "стабильностью во время настоящей игры — карточка ниже."))
        return card

    def _stress_mode_hint(self, index: int):
        key = self._stress_keys[index]
        self.stress_hint.setText(STRESS_MODES[key][1])

    def _toggle_stress(self):
        if self.stress.running:
            report = self.stress.stop(manual=True)
            self._render_stress(report)
            self.stress_btn.setText("Запустить проверку")
            self.stress_bar.setVisible(False)
            return

        mode = self._stress_keys[self.stress_mode.currentIndex()]
        minutes = self.stress_minutes.value()
        self.stress_result.setText("")
        self.stress.start(mode=mode, minutes=minutes)
        self.stress_btn.setText("Остановить и показать результат")
        self.stress_bar.setVisible(True)
        self.stress_bar.setRange(0, minutes * 60)
        self.stress_state.setText(
            "Идёт нагрузка. Компьютером можно пользоваться, но он будет "
            "заметно занят — это нормально.")

    def _update_stress(self):
        if not self.stress.running:
            return
        elapsed = self.stress.elapsed
        self.stress_bar.setValue(int(elapsed))
        result = self.stress.result
        errors = (f"<span style='color:{theme.DANGER}'>ошибок {result.errors}</span>"
                  if result.errors else "ошибок нет")
        self.stress_state.setText(
            f"Идёт {elapsed / 60:.1f} мин из {self.stress_minutes.value()} · "
            f"кругов по процессору {result.cpu_rounds}, по памяти {result.mem_rounds} · "
            f"{errors}")
        self.stress_state.setTextFormat(Qt.RichText)

        # тест мог закончиться сам по времени
        if self.stress.remaining <= 0:
            report = self.stress.stop(manual=False)
            self._render_stress(report)
            self.stress_btn.setText("Запустить проверку")
            self.stress_bar.setVisible(False)

    def _render_stress(self, report):
        verdict, tone = report.verdict
        color = {"ok": theme.OK, "warn": theme.WARN, "danger": theme.DANGER}[tone]
        lines = [f"<span style='color:{color}; font-size:15px; font-weight:700;'>"
                 f"{verdict}</span>"]
        for text in report.advice():
            lines.append("• " + text)
        if report.error_details:
            lines.append("<br>Что именно не сошлось:")
            for detail in report.error_details[:5]:
                lines.append(f"<span style='color:{theme.DANGER}'>— {detail}</span>")
        self.stress_result.setText("<br>".join(lines))
        self.stress_state.setText("")

    # ------------------------------------------------------------ авторазгон

    def _autooc_card(self) -> Card:
        card = Card("🤖  Умный авторазгон",
                    "Приложение само следит за картой во время игры и подстраивает "
                    "предел мощности: холодно и есть упор в мощность — поднимает, "
                    "перегрев или рывки — отступает. Меняется только официальный "
                    "предел через драйвер, это полностью обратимо.")

        available, message = self.autooc.available()
        self.autooc_note = QLabel(message)
        self.autooc_note.setObjectName("Dim")
        self.autooc_note.setWordWrap(True)
        card.add(self.autooc_note)

        state = QGridLayout()
        state.setHorizontalSpacing(18)
        state.setVerticalSpacing(6)
        self.autooc_limit = KeyValue("Предел мощности сейчас", "—")
        self.autooc_temp = KeyValue("Температура", "—")
        self.autooc_verdict = KeyValue("Решение", "готов")
        state.addWidget(self.autooc_limit, 0, 0)
        state.addWidget(self.autooc_temp, 0, 1)
        state.addWidget(self.autooc_verdict, 1, 0, 1, 2)
        card.add_layout(state)

        row = QHBoxLayout()
        self.autooc_btn = primary_button("Включить авторазгон")
        self.autooc_btn.setEnabled(available)
        self.autooc_btn.clicked.connect(self._toggle_autooc)
        row.addWidget(self.autooc_btn)
        row.addStretch()
        card.add_layout(row)

        self.autooc_log = QLabel("Журнал решений появится здесь.")
        self.autooc_log.setObjectName("Faint")
        self.autooc_log.setWordWrap(True)
        self.autooc_log.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 10px;")
        card.add(self.autooc_log)
        return card

    def _toggle_autooc(self):
        if self.autooc.state.running:
            self.autooc.stop(restore=True)
            self.autooc_btn.setText("Включить авторазгон")
        else:
            ok, message = self.autooc.start()
            self.autooc_note.setText(message)
            if ok:
                self.autooc_btn.setText("Остановить и вернуть штатный предел")

    def _update_autooc(self):
        st = self.autooc.state
        if not st.running and not st.steps:
            return
        limit_text = f"{st.limit_now} Вт"
        if st.limit_max and st.limit_max != st.limit_now:
            limit_text += f"  (штат {st.limit_default}, потолок {st.limit_max})"
        self.autooc_limit.set_value(limit_text)
        self.autooc_temp.set_value("—" if not st.temp else f"{st.temp} °C",
                                   theme.temp_color(st.temp or 0))
        self.autooc_verdict.set_value(st.verdict)
        if st.steps:
            lines = []
            for step in st.steps[:8]:
                import time as _t
                stamp = _t.strftime("%H:%M:%S", _t.localtime(step.when))
                lines.append(f"{stamp} · {step.text}")
            self.autooc_log.setText("\n".join(lines))

    def _render_report(self, report: overclock.StabilityReport):
        if not report.samples:
            self.watch_result.setText(report.verdict)
            return
        color = {"Отлично": theme.OK, "В норме, но тепло": theme.WARN,
                 "Горячо": theme.DANGER}.get(report.verdict, theme.TEXT_DIM)
        lines = [
            f"<span style='color:{color}; font-size:15px; font-weight:700;'>"
            f"Вердикт: {report.verdict}</span>",
            f"Замеров: {report.samples} · максимум {report.max_temp} °C · "
            f"в среднем {report.avg_temp} °C · пик потребления {report.max_power} Вт",
            f"Частота ядра: от {report.min_clock} до {report.max_clock} МГц",
            f"Сброс частот: по мощности {report.throttle_power}, "
            f"по нагреву {report.throttle_thermal}, аварийный {report.throttle_reliability}",
            "",
        ] + [f"• {a}" for a in report.advice]
        self.watch_result.setText("<br>".join(lines))
