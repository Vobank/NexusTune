"""Страница энергопотребления: сколько ест компьютер, пики и деньги."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QProgressBar,
)

from core import power as power_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, Sparkline, page_header, scroll_page,
    primary_button, ghost_button, dim_label,
)


class PowerBar(QWidget):
    """Полоса разложения мощности по компонентам."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.bar = QHBoxLayout()
        self.bar.setSpacing(3)
        lay.addLayout(self.bar)

        self.parts: dict[str, tuple[QProgressBar, QLabel]] = {}
        for key, name, color in (("gpu", "Видеокарта", theme.ACCENT),
                                 ("cpu", "Процессор", theme.ACCENT_2),
                                 ("other", "Остальное железо", theme.TEXT_FAINT)):
            row = QHBoxLayout()
            label = QLabel(name)
            label.setObjectName("Dim")
            label.setMinimumWidth(150)
            row.addWidget(label)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {theme.SURFACE_2}; border: none; "
                f"border-radius: 5px; height: 10px; }} "
                f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")
            row.addWidget(bar, 1)

            value = QLabel("—")
            value.setStyleSheet(f"color: {color}; font-weight: 700; min-width: 90px;")
            value.setAlignment(Qt.AlignRight)
            row.addWidget(value)
            lay.addLayout(row)
            self.parts[key] = (bar, value)

    def update_parts(self, sample, total: float):
        values = {"gpu": sample.gpu_w, "cpu": sample.cpu_w, "other": sample.other_w}
        measured = {"gpu": sample.gpu_measured, "cpu": sample.cpu_measured, "other": False}
        for key, (bar, label) in self.parts.items():
            watts = values[key]
            bar.setValue(int(watts / total * 100) if total else 0)
            mark = "" if measured[key] else "≈"
            label.setText(f"{mark}{watts:.0f} Вт")


class PowerPage(QWidget):
    """Мощность в реальном времени, пики и расходы."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.meter = ctx.power_meter

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Энергопотребление",
            "Сколько компьютер тянет из розетки прямо сейчас, каким был пик "
            "и во сколько это обходится."))

        lay.addWidget(self._live_card())
        lay.addWidget(self._session_card())
        lay.addWidget(self._history_card())
        lay.addWidget(self._psu_card())
        lay.addWidget(self._honesty_card())
        lay.addStretch()

    # ------------------------------------------------------------ карточки

    def _live_card(self) -> Card:
        card = Card("Прямо сейчас")

        top = QHBoxLayout()
        top.setSpacing(26)

        left = QVBoxLayout()
        left.setSpacing(0)
        self.now_value = QLabel("—")
        self.now_value.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {theme.ACCENT};")
        left.addWidget(self.now_value)
        self.now_label = QLabel("ватт из розетки")
        self.now_label.setObjectName("Dim")
        left.addWidget(self.now_label)
        self.verdict_badge = Badge("—", theme.TEXT_FAINT)
        left.addWidget(self.verdict_badge)
        top.addLayout(left)

        self.bars = PowerBar()
        top.addWidget(self.bars, 1)
        card.add_layout(top)

        self.chart = Sparkline(180, theme.ACCENT)
        self.chart.maximum = 500
        self.chart.setMinimumHeight(90)
        card.add(self.chart)

        self.verdict_text = QLabel("")
        self.verdict_text.setObjectName("Faint")
        self.verdict_text.setWordWrap(True)
        card.add(self.verdict_text)
        return card

    def _session_card(self) -> Card:
        card = Card("С момента запуска приложения")
        grid = QGridLayout()
        grid.setHorizontalSpacing(30)

        self.kv_peak = KeyValue("Пик потребления", "—", theme.DANGER)
        self.kv_peak_time = KeyValue("Когда был пик", "—")
        self.kv_avg = KeyValue("В среднем", "—", theme.ACCENT)
        self.kv_min = KeyValue("Минимум (простой)", "—", theme.OK)
        self.kv_energy = KeyValue("Израсходовано", "—", theme.ACCENT_2)
        self.kv_cost = KeyValue("Это стоило", "—", theme.WARN)

        for i, widget in enumerate((self.kv_peak, self.kv_avg, self.kv_energy)):
            grid.addWidget(widget, i, 0)
        for i, widget in enumerate((self.kv_peak_time, self.kv_min, self.kv_cost)):
            grid.addWidget(widget, i, 1)
        card.add_layout(grid)

        self.peak_detail = QLabel("")
        self.peak_detail.setObjectName("Faint")
        self.peak_detail.setWordWrap(True)
        card.add(self.peak_detail)

        row = QHBoxLayout()
        reset = ghost_button("Сбросить пик")
        reset.clicked.connect(self._reset_peak)
        row.addWidget(reset)
        row.addStretch()
        card.add_layout(row)
        return card

    def _history_card(self) -> Card:
        card = Card("Расходы", "Тариф можно поменять — прогноз пересчитается сразу.")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Цена киловатт-часа:", objectName="Dim"))
        self.tariff_box = QDoubleSpinBox()
        self.tariff_box.setRange(0.01, 999.0)
        self.tariff_box.setDecimals(2)
        self.tariff_box.setSingleStep(0.1)
        self.tariff_box.setValue(self.ctx.tariff)
        self.tariff_box.valueChanged.connect(self._on_tariff)
        row.addWidget(self.tariff_box)

        self.currency_box = QLineEdit(self.ctx.currency)
        self.currency_box.setMaximumWidth(70)
        self.currency_box.textChanged.connect(self._on_currency)
        row.addWidget(self.currency_box)

        row.addWidget(QLabel("Часов в день:", objectName="Dim"))
        self.hours_box = QDoubleSpinBox()
        self.hours_box.setRange(0.5, 24.0)
        self.hours_box.setSingleStep(0.5)
        self.hours_box.setValue(float(self.ctx.config.get("hours_per_day", 6.0)))
        self.hours_box.valueChanged.connect(self._on_hours)
        row.addWidget(self.hours_box)
        row.addStretch()
        card.add_layout(row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        self.kv_month = KeyValue("Прогноз на месяц", "—", theme.WARN)
        self.kv_today = KeyValue("Сегодня израсходовано", "—")
        self.kv_week = KeyValue("За неделю", "—")
        self.kv_alltime = KeyValue("Рекордный пик за всё время", "—", theme.DANGER)
        grid.addWidget(self.kv_month, 0, 0)
        grid.addWidget(self.kv_today, 1, 0)
        grid.addWidget(self.kv_week, 0, 1)
        grid.addWidget(self.kv_alltime, 1, 1)
        card.add_layout(grid)

        self.history_note = QLabel("")
        self.history_note.setObjectName("Faint")
        self.history_note.setWordWrap(True)
        card.add(self.history_note)
        self._refresh_history()
        return card

    def _psu_card(self) -> Card:
        watts, text = self.meter.psu_advice()
        card = Card("Блок питания")
        row = QHBoxLayout()
        value = QLabel(f"{watts} Вт")
        value.setStyleSheet(f"font-size: 34px; font-weight: 800; color: {theme.OK};")
        row.addWidget(value)
        row.addWidget(dim_label(text), 1)
        card.add_layout(row)
        return card

    def _honesty_card(self) -> Card:
        card = Card("Откуда берутся цифры")
        measured = []
        estimated = []
        reading = self.ctx.last_reading or {}
        if reading.get("gpu_power") is not None:
            measured.append("мощность видеокарты — с датчика драйвера")
        else:
            estimated.append("мощность видеокарты — по загрузке и классу карты")
        if reading.get("cpu_power") is not None:
            measured.append("мощность процессора — с датчика")
        else:
            estimated.append("мощность процессора — по загрузке и его тепловому пакету")
        estimated.append("плата, память, диски и вентиляторы — по составу железа")
        estimated.append("потери блока питания — по типичному КПД 88%")

        if measured:
            card.add(SectionTitle("Измерено"))
            for line in measured:
                card.add(QLabel("✔  " + line, objectName="Dim", wordWrap=True))
        card.add(SectionTitle("Оценено"))
        for line in estimated:
            card.add(QLabel("≈  " + line, objectName="Dim", wordWrap=True))
        card.add(dim_label(
            "Знаком ≈ помечено то, что посчитано, а не измерено. Точное значение "
            "из розетки покажет только ваттметр в удлинителе. Если запустить "
            "LibreHardwareMonitor, приложение возьмёт настоящую мощность процессора "
            "с его датчика, и оценка станет заметно точнее."))
        return card

    # ------------------------------------------------------------ обновление

    def tick(self, reading: dict):
        # счётчик обновляет главное окно — здесь только показываем последний замер
        sample = self.meter.last
        if sample is None:
            return

        self.now_value.setText(f"{sample.wall_w:.0f}")
        self.chart.maximum = max(200, self.meter.peak_w * 1.15)
        self.chart.push(sample.wall_w)
        self.bars.update_parts(sample, sample.total_w)

        verdict, explanation = self.meter.verdict()
        colors = {"простой": theme.OK, "лёгкая нагрузка": theme.ACCENT,
                  "игра или работа": theme.WARN, "пиковая нагрузка": theme.DANGER}
        self.verdict_badge.setText(verdict)
        self.verdict_badge.set_color(colors.get(verdict, theme.TEXT_FAINT))
        self.verdict_text.setText(explanation)

        self.kv_peak.set_value(f"{self.meter.peak_w:.0f} Вт")
        if self.meter.peak_at:
            ago = int(time.time() - self.meter.peak_at)
            when = f"{ago} с назад" if ago < 90 else f"{ago // 60} мин назад"
            self.kv_peak_time.set_value(when)
        self.kv_avg.set_value(f"{self.meter.average_w:.0f} Вт")
        self.kv_min.set_value(f"{self.meter.min_w:.0f} Вт" if self.meter.min_w else "—")
        self.kv_energy.set_value(f"{self.meter.energy_wh / 1000:.3f} кВт·ч")
        self.kv_cost.set_value(self.ctx.money(self.meter.cost(self.ctx.tariff)))

        peak = self.meter.peak_breakdown
        if peak:
            self.peak_detail.setText(
                f"В пике: видеокарта {peak.gpu_w:.0f} Вт, процессор {peak.cpu_w:.0f} Вт, "
                f"остальное {peak.other_w:.0f} Вт. "
                f"Это {peak.wall_w / 1000:.2f} кВт — примерно как один "
                f"{'электрочайник' if peak.wall_w > 700 else 'мощный светильник'}.")

        kwh, cost = self.meter.projected_month(
            float(self.ctx.config.get("hours_per_day", 6.0)), self.ctx.tariff)
        self.kv_month.set_value(f"{kwh} кВт·ч ≈ {self.ctx.money(cost)}")

    def _refresh_history(self):
        summary = power_core.history_summary(self.ctx.tariff)
        self.kv_today.set_value(f"{summary['today_kwh']:.3f} кВт·ч")
        self.kv_week.set_value(
            f"{summary['week_kwh']:.2f} кВт·ч ≈ {self.ctx.money(summary['week_cost'])}")
        peak = summary["all_time_peak"]
        self.kv_alltime.set_value(f"{peak:.0f} Вт" if peak else "ещё не замерен")
        if summary["days_tracked"] < 2:
            self.history_note.setText(
                "История копится с каждым запуском приложения. Через несколько дней "
                "здесь появится реальная статистика расхода.")
        else:
            self.history_note.setText(
                f"Данные собраны за {summary['days_tracked']} дней, "
                f"компьютер работал {summary['week_hours']:.0f} ч за последнюю неделю.")

    def _reset_peak(self):
        self.meter.reset_peak()
        self.peak_detail.setText("Пик сброшен — считаем заново.")

    def _on_tariff(self, value: float):
        self.ctx.config["tariff"] = value
        self.ctx.save()
        self._refresh_history()

    def _on_currency(self, text: str):
        self.ctx.config["currency"] = text.strip() or "₴"
        self.ctx.save()
        self._refresh_history()

    def _on_hours(self, value: float):
        self.ctx.config["hours_per_day"] = value
        self.ctx.save()
