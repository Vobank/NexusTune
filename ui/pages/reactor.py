"""Страница «Реактор» — эксклюзивные и опасные режимы в одном месте.

Здесь живёт то, чего нет в других оптимизаторах: реактивный планировщик,
чёрный ящик с автооткатом, разведение прерываний по ядрам и режим «Ультиматум».
Оформление намеренно агрессивное — это единственная страница, где риск виден
глазом, а не спрятан за вежливыми формулировками.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QCheckBox,
    QSpinBox, QComboBox,
)

from core import blackbox as bb_core
from core import interrupts as irq_core
from core import ultimatum as ult_core
from core.context import AppContext
from core.reactor import Reactor
from core.util import is_admin, log

from .. import theme
from ..widgets import Card, Badge, page_header, scroll_page, ghost_button, dim_label
from ..reactorwidgets import (
    ReactorGauge, PulseBar, HazardToggle, WarningStrip, CoreHeatmap, HAZARD, LIVE,
)
import psutil
from core.cpuinfo import read_topology


class InterruptsWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(irq_core.read_devices())
        except Exception:
            self.done.emit([])


class UltimatumWorker(QThread):
    done = Signal(bool, object)

    def __init__(self, engage: bool, minutes: int = 120):
        super().__init__()
        self.engage = engage
        self.minutes = minutes

    def run(self):
        try:
            if self.engage:
                ok, state = ult_core.engage(self.minutes)
            else:
                ok, state = ult_core.disengage()
            self.done.emit(ok, state)
        except Exception as exc:
            log(f"Ультиматум: сбой — {exc}")
            self.done.emit(False, None)


class ReactorPage(QWidget):
    """Боевые режимы."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.reactor = Reactor(ctx)
        self.blackbox = bb_core.BlackBox(ctx)
        ctx.reactor = self.reactor
        ctx.blackbox = self.blackbox
        self.irq_load = irq_core.InterruptLoad()
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        header = page_header(
            "⚡ РЕАКТОР",
            "Боевые режимы, которых нет в других программах. Здесь риск виден "
            "глазом. Всё обратимо, но читай, чем платишь.")
        lay.addWidget(header)

        lay.addWidget(WarningStrip(
            "Эта страница меняет поведение системы на лету и снимает защиту. "
            "Каждый режим объясняет свою цену. Ничего необратимого здесь нет — "
            "но и включать всё подряд не нужно."))

        lay.addWidget(self._battle_card())
        lay.addWidget(self._reactor_card())
        lay.addWidget(self._blackbox_card())
        lay.addWidget(self._interrupts_card())
        lay.addWidget(self._ultimatum_card())
        lay.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self._timer.start(200)
        self._load_interrupts()
        self._refresh_ultimatum()

    # ------------------------------------------------------------ боевая готовность

    def _battle_card(self) -> Card:
        card = Card("🎯  Боевая готовность",
                    "Одна кнопка вместо четырёх тумблеров: включает реактор, "
                    "чёрный ящик и разводит прерывания разом. Живая карта ядер "
                    "ниже показывает, куда упирается нагрузка прямо сейчас.")

        self.heatmap = CoreHeatmap(read_topology())
        card.add(self.heatmap)
        psutil.cpu_percent(percpu=True)      # прогрев счётчика

        row = QHBoxLayout()
        self.battle_btn = HazardToggle("БОЕВАЯ ГОТОВНОСТЬ")
        self.battle_btn.toggled.connect(self._toggle_battle)
        row.addWidget(self.battle_btn, 1)
        card.add_layout(row)

        self.battle_status = QLabel("Выключено. Реактор и чёрный ящик спят.")
        self.battle_status.setObjectName("Dim")
        self.battle_status.setWordWrap(True)
        card.add(self.battle_status)
        return card

    def _toggle_battle(self, on: bool):
        steps = []
        if on:
            ok, _n = self.reactor.start(park_background=True)
            self.reactor_toggle.set_on(ok)
            steps.append("реактор" if ok else "реактор (не удалось)")
            ok2, _n = self.blackbox.start(auto_rollback=True)
            self.bb_toggle.set_on(ok2)
            steps.append("чёрный ящик")
            self.battle_status.setText(
                "БОЕВАЯ ГОТОВНОСТЬ. Включено: " + ", ".join(steps)
                + ". Прерывания разведи кнопкой ниже — это требует перезагрузки, "
                "поэтому одним кликом не делаю.")
        else:
            self.reactor.stop(); self.reactor_toggle.set_on(False)
            self.blackbox.stop(); self.bb_toggle.set_on(False)
            self.battle_status.setText("Выключено. Реактор и чёрный ящик спят.")

    # ------------------------------------------------------------ реактор

    def _reactor_card(self) -> Card:
        card = Card("⚡  Реактивный планировщик",
                    "Следит за мышью 120 раз в секунду. В момент выстрела или "
                    "резкого разворота поднимает приоритет игры, через секунду "
                    "покоя отпускает. Обычный «игровой режим» так не умеет.")

        row = QHBoxLayout()
        row.setSpacing(16)
        self.reactor_gauge = ReactorGauge("активность", "", 100, danger_at=101)
        self.reactor_gauge.setFixedSize(150, 150)
        row.addWidget(self.reactor_gauge)

        right = QVBoxLayout()
        self.reactor_pulse = PulseBar(HAZARD)
        right.addWidget(self.reactor_pulse)
        self.reactor_stats = QLabel("Выключен.")
        self.reactor_stats.setObjectName("Dim")
        self.reactor_stats.setWordWrap(True)
        right.addWidget(self.reactor_stats)
        row.addLayout(right, 1)
        card.add_layout(row)

        controls = QHBoxLayout()
        self.reactor_toggle = HazardToggle("РЕАКТОР")
        self.reactor_toggle.toggled.connect(self._toggle_reactor)
        controls.addWidget(self.reactor_toggle, 1)

        self.park_check = QCheckBox("Прижимать фон к экономичным ядрам")
        self.park_check.setChecked(True)
        controls.addWidget(self.park_check)
        card.add_layout(controls)

        card.add(dim_label(
            "L3-кэш никто не «перераспределяет»: на домашнем процессоре это "
            "невозможно из обычной программы. Реактор делает то, что реально "
            "работает — держит игру на быстрых ядрах и не даёт планировщику "
            "отдать твой квант фону в решающий момент."))
        return card

    def _toggle_reactor(self, on: bool):
        if on:
            ok, note = self.reactor.start(
                park_background=self.park_check.isChecked())
            if not ok:
                self.reactor_toggle.set_on(False)
                QMessageBox.warning(self, "Реактор", note)
                return
            self.reactor_stats.setText(note)
        else:
            state = self.reactor.stop()
            self.reactor_stats.setText(
                f"Выключен. За сессию: вмешательств {state.bursts}, "
                f"в поднятом состоянии {state.boosted_seconds:.0f} с, "
                f"пик активности {state.peak_intensity:.0%}.")

    # ------------------------------------------------------------ чёрный ящик

    def _blackbox_card(self) -> Card:
        card = Card("🖤  Чёрный ящик и датчики смерти",
                    "Пишет показания в кольцевой буфер. При перегреве, вылете "
                    "драйвера или обвале кадров сбрасывает запись на диск и сам "
                    "снимает разгон. Как самописец в самолёте.")

        self.bb_status = QLabel("Выключен.")
        self.bb_status.setObjectName("Dim")
        self.bb_status.setWordWrap(True)
        card.add(self.bb_status)

        triggers = QLabel(" · ".join(
            t.title for t in bb_core.default_triggers()))
        triggers.setObjectName("Faint")
        triggers.setWordWrap(True)
        card.add(triggers)

        controls = QHBoxLayout()
        self.bb_toggle = HazardToggle("ЧЁРНЫЙ ЯЩИК")
        self.bb_toggle.cover_open = True     # он не опасен, крышка не нужна
        self.bb_toggle.toggled.connect(self._toggle_blackbox)
        controls.addWidget(self.bb_toggle, 1)

        self.rollback_check = QCheckBox("Автооткат разгона при сбое")
        self.rollback_check.setChecked(True)
        controls.addWidget(self.rollback_check)
        card.add_layout(controls)

        self.bb_incidents = QLabel("")
        self.bb_incidents.setObjectName("Faint")
        self.bb_incidents.setWordWrap(True)
        card.add(self.bb_incidents)
        self._refresh_incidents()
        return card

    def _toggle_blackbox(self, on: bool):
        if on:
            ok, note = self.blackbox.start(
                auto_rollback=self.rollback_check.isChecked())
            self.bb_status.setText(note)
        else:
            self.blackbox.stop()
            self.bb_status.setText("Выключен.")

    def _refresh_incidents(self):
        rows = bb_core.load_incidents(5)
        if not rows:
            self.bb_incidents.setText("Происшествий не записано — это хорошо.")
            return
        lines = ["Последние происшествия:"]
        for row in rows:
            import time as _t
            stamp = _t.strftime("%d.%m %H:%M", _t.localtime(row.get("when", 0)))
            lines.append(f"  {stamp} — {row.get('trigger', '?')}")
        self.bb_incidents.setText("\n".join(lines))

    # ------------------------------------------------------------ прерывания

    def _interrupts_card(self) -> Card:
        card = Card("🔌  Прерывания по ядрам",
                    "«Заморозить прерывания в ноль» невозможно — это выключит "
                    "мышь, звук и часы. Что реально работает: увести прерывания "
                    "устройств с тех ядер, где считает игра.")

        row = QHBoxLayout()
        row.setSpacing(16)
        self.irq_gauge = ReactorGauge("нагрузка ISR+DPC", "%", 20, danger_at=12)
        self.irq_gauge.setFixedSize(150, 150)
        row.addWidget(self.irq_gauge)

        right = QVBoxLayout()
        self.irq_rate = QLabel("Читаю счётчики прерываний…")
        self.irq_rate.setObjectName("Dim")
        self.irq_rate.setWordWrap(True)
        right.addWidget(self.irq_rate)
        self.irq_verdict = QLabel("")
        self.irq_verdict.setWordWrap(True)
        right.addWidget(self.irq_verdict)
        row.addLayout(right, 1)
        card.add_layout(row)

        self.irq_devices = QVBoxLayout()
        self.irq_devices.setSpacing(6)
        holder = QWidget(); holder.setLayout(self.irq_devices)
        card.add(holder)

        buttons = QHBoxLayout()
        self.irq_apply = ghost_button("Увести прерывания с игровых ядер")
        self.irq_apply.clicked.connect(self._steer_interrupts)
        buttons.addWidget(self.irq_apply)
        self.irq_reset = ghost_button("Вернуть как было")
        self.irq_reset.clicked.connect(self._reset_interrupts)
        buttons.addWidget(self.irq_reset)
        buttons.addStretch()
        card.add_layout(buttons)

        self.irq_status = QLabel("")
        self.irq_status.setObjectName("Faint")
        self.irq_status.setWordWrap(True)
        card.add(self.irq_status)
        return card

    def _load_interrupts(self):
        worker = InterruptsWorker()
        worker.done.connect(self._on_interrupts)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _on_interrupts(self, devices: list):
        self.devices = devices
        while self.irq_devices.count():
            item = self.irq_devices.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for device in devices:
            row = QLabel(f"{device.group}: {device.name[:44]}  —  "
                         f"{device.policy_name}")
            row.setObjectName("Faint")
            row.setWordWrap(True)
            colour = LIVE if device.pinned else theme.TEXT_FAINT
            row.setStyleSheet(f"color: {colour}; font-size: 11px;")
            self.irq_devices.addWidget(row)
        self.irq_status.setText(irq_core.summary(devices))

    def _steer_interrupts(self):
        if not is_admin():
            QMessageBox.information(self, "Нужны права",
                                    "Развести прерывания можно только "
                                    "с правами администратора.")
            return
        mask, why = irq_core.suggest_mask()
        if mask <= 0:
            self.irq_status.setText(why)
            return
        box = QMessageBox(self)
        box.setWindowTitle("Прерывания по ядрам")
        box.setIcon(QMessageBox.Question)
        box.setText("Увести прерывания сети и накопителей с игровых ядер?")
        box.setInformativeText(why + "\n\nИзменение вступит в силу после "
                               "перезагрузки. Вернуть — кнопкой рядом.")
        yes = box.addButton("Развести", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if box.clickedButton() is not yes:
            return

        done = 0
        for device in getattr(self, "devices", []):
            # видеокарту не трогаем: ей прерывания нужны на быстрых ядрах
            if device.klass == "Display":
                continue
            ok, _msg = irq_core.set_affinity(device, mask)
            if ok:
                done += 1
        self.irq_status.setText(
            f"Разведено устройств: {done}. Перезагрузи компьютер, чтобы "
            "изменение заработало.")
        self._load_interrupts()

    def _reset_interrupts(self):
        if not is_admin():
            return
        done = 0
        for device in getattr(self, "devices", []):
            if device.pinned:
                ok, _msg = irq_core.set_affinity(device, 0)
                if ok:
                    done += 1
        self.irq_status.setText(f"Возвращено устройств: {done}. "
                                "Нужна перезагрузка.")
        self._load_interrupts()

    # ------------------------------------------------------------ ультиматум

    def _ultimatum_card(self) -> Card:
        card = Card()
        card.setStyleSheet(
            f"QFrame#Card {{ background: {theme.SURFACE}; "
            f"border: 2px solid {HAZARD}; border-radius: 14px; }}")

        title = QLabel("☢  УЛЬТИМАТУМ")
        title.setStyleSheet(f"color: {LIVE}; font-size: 19px; font-weight: 800; "
                            "letter-spacing: 2px;")
        card.add(title)

        preview = ult_core.preview()
        card.add(dim_label(
            f"Глушит {preview['services']} фоновых служб, брандмауэр и защиту "
            "реального времени ради каждого свободного такта. Для оффлайн-матча "
            "или бенчмарка, не для повседневной работы."))

        cost = QLabel("Чем платишь:\n" + "\n".join("  ⚠ " + c for c in preview["cost"]))
        cost.setWordWrap(True)
        cost.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
        card.add(cost)

        safe = QLabel("Страховка:\n" + "\n".join("  ✓ " + s for s in preview["safety"]))
        safe.setWordWrap(True)
        safe.setStyleSheet(f"color: {theme.OK}; font-size: 12px;")
        card.add(safe)

        row = QHBoxLayout()
        row.addWidget(QLabel("Сам выключиться через:"))
        self.ult_minutes = QSpinBox()
        self.ult_minutes.setRange(15, 480)
        self.ult_minutes.setValue(120)
        self.ult_minutes.setSuffix(" мин")
        self.ult_minutes.setSingleStep(15)
        row.addWidget(self.ult_minutes)
        row.addStretch()
        card.add_layout(row)

        self.ult_toggle = HazardToggle("АКТИВИРОВАТЬ УЛЬТИМАТУМ")
        self.ult_toggle.toggled.connect(self._toggle_ultimatum)
        card.add(self.ult_toggle)

        self.ult_status = QLabel("")
        self.ult_status.setObjectName("Dim")
        self.ult_status.setWordWrap(True)
        card.add(self.ult_status)
        return card

    def _toggle_ultimatum(self, on: bool):
        if not is_admin():
            self.ult_toggle.set_on(False)
            QMessageBox.warning(self, "Нужны права",
                                "«Ультиматум» работает только с правами "
                                "администратора.")
            return
        if on:
            box = QMessageBox(self)
            box.setWindowTitle("Активация «Ультиматума»")
            box.setIcon(QMessageBox.Warning)
            box.setText("Снять защиту и заглушить фон?")
            box.setInformativeText(
                "Пока режим включён, компьютер без антивируса и брандмауэра. "
                "Не выходи в интернет и не открывай файлы. Режим сам "
                f"выключится через {self.ult_minutes.value()} минут.")
            yes = box.addButton("Активировать", QMessageBox.AcceptRole)
            box.addButton("Отмена", QMessageBox.RejectRole)
            box.setStyleSheet(theme.STYLESHEET)
            box.exec()
            if box.clickedButton() is not yes:
                self.ult_toggle.set_on(False)
                return
            self._run_ultimatum(True)
        else:
            self._run_ultimatum(False)

    def _run_ultimatum(self, engage: bool):
        self.ult_status.setText("Активирую…" if engage else "Возвращаю защиту…")
        worker = UltimatumWorker(engage, self.ult_minutes.value())
        worker.done.connect(self._on_ultimatum)
        worker.finished.connect(lambda w=worker: self._drop(w))
        self.workers.append(worker)
        worker.start()

    def _on_ultimatum(self, ok: bool, state):
        if state is None:
            self.ult_status.setText("Не удалось. Проверь права администратора.")
            self._refresh_ultimatum()
            return
        self.ult_status.setText("\n".join(state.steps))
        self._refresh_ultimatum()

    def _refresh_ultimatum(self):
        active = ult_core.is_active()
        self.ult_toggle.set_on(active)
        if active:
            state = ult_core.current_state()
            self.ult_status.setText(
                f"РЕЖИМ АКТИВЕН. Останется {state.minutes_left:.0f} мин, "
                "потом защита вернётся сама. Служб заглушено: "
                f"{len(state.services_stopped)}.")

    # ------------------------------------------------------------ пульс

    def _pulse(self):
        # реактор
        st = self.reactor.state
        self.reactor_gauge.set_value(st.intensity * 100)
        self.reactor_pulse.push(st.intensity)
        if st.running:
            colour = LIVE if st.boosted else theme.TEXT_DIM
            self.reactor_stats.setText(
                f"<span style='color:{colour}'>● </span>"
                f"опрос {st.poll_hz:.0f} Гц · цель: {st.target_name or 'жду игру'} · "
                f"приоритет: {st.priority_name} · вмешательств {st.bursts}")
            self.reactor_stats.setTextFormat(Qt.RichText)

    def tick(self, reading: dict):
        # живая карта ядер
        try:
            self.heatmap.set_loads(psutil.cpu_percent(percpu=True))
        except Exception:
            pass

        # живые счётчики прерываний
        values = self.irq_load.read()
        dpc = values.get("dpc_percent")
        isr = values.get("interrupt_percent")
        if dpc is not None and isr is not None:
            self.irq_gauge.set_value(dpc + isr)
            self.irq_rate.setText(
                f"Прерываний в секунду: {values.get('interrupts_sec') or 0:.0f} · "
                f"DPC в секунду: {values.get('dpc_sec') or 0:.0f}")
            verdict, tone = irq_core.InterruptLoad.verdict(values)
            colour = {"ok": theme.OK, "warn": theme.WARN,
                      "danger": HAZARD}.get(tone, theme.TEXT_DIM)
            self.irq_verdict.setText(verdict)
            self.irq_verdict.setStyleSheet(f"color: {colour}; font-size: 12px;")

        if self.blackbox.running:
            self.bb_status.setText(
                f"● ПИШЕТ. В буфере {self.blackbox.buffered_seconds:.0f} с "
                f"показаний. Происшествий за сессию: {len(self.blackbox.incidents)}.")

    def _drop(self, worker):
        if worker in self.workers:
            self.workers.remove(worker)
