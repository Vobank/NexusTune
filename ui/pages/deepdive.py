"""Страница «Разбор»: загрузка системы и драйверы.

Две вещи, которые Windows знает про себя, но прячет: сколько на самом деле
занимает её загрузка (и кто именно её тормозит) и насколько устарели драйверы
устройств. Обе читаются из системы, ничего не выдумывается и не скачивается.
"""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QGridLayout,
    QFileDialog, QApplication, QProgressBar,
)

from core import boottime as boot_core
from core import drivers as drivers_core
from core import integrity as integrity_core
from core import report as report_core
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, KeyValue, Sparkline, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)

_TONE = {"ok": theme.OK, "warn": theme.WARN, "danger": theme.DANGER}


class BootWorker(QThread):
    done = Signal(object, dict, bool)

    def run(self):
        try:
            report = boot_core.read_boot_report()
            uptime = boot_core.uptime_summary()
            fast = boot_core.fast_startup_enabled()
            self.done.emit(report, uptime, fast)
        except Exception:
            self.done.emit(boot_core.BootReport(), {}, False)


class DriversWorker(QThread):
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(drivers_core.read_drivers())
        except Exception:
            self.done.emit([])


class EnableLogWorker(QThread):
    done = Signal(bool, str)

    def run(self):
        ok, message = boot_core.enable_log()
        self.done.emit(ok, message)


class ReportWorker(QThread):
    done = Signal(str)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    def run(self):
        try:
            self.done.emit(report_core.build_report(self.ctx))
        except Exception as exc:
            self.done.emit(f"Отчёт собрать не удалось: {exc}")


class IntegrityWorker(QThread):
    """Долгие проверки целостности: SFC и DISM идут минутами."""
    progress = Signal(str, int)
    done = Signal(object)

    def __init__(self, step: str, drive: str = "C"):
        super().__init__()
        self.step = step
        self.drive = drive

    def run(self):
        try:
            if self.step == "dism_scan":
                result = integrity_core.dism_scan(on_progress=self._emit)
            elif self.step == "dism_repair":
                result = integrity_core.dism_scan(on_progress=self._emit, repair=True)
            elif self.step == "sfc":
                result = integrity_core.sfc_scan(on_progress=self._emit)
            else:
                result = integrity_core.chkdsk_scan(self.drive, on_progress=self._emit)
        except Exception as exc:
            result = integrity_core.CheckResult(
                tool=self.step, title="Проверка сорвалась", detail=str(exc))
        self.done.emit(result)

    def _emit(self, line: str, percent):
        self.progress.emit(line, percent if percent is not None else -1)


class DeepDivePage(QWidget):
    """Загрузка системы и состояние драйверов."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Разбор",
            "Сколько на самом деле длится загрузка Windows, кто её тормозит "
            "и насколько устарели драйверы твоего железа."))

        lay.addWidget(self._integrity_card())
        lay.addWidget(self._report_card())
        lay.addWidget(self._boot_card())
        lay.addWidget(self._drivers_card())
        lay.addStretch()

        self._load_boot()
        self._load_drivers()

    # ------------------------------------------------------------ целостность

    def _integrity_card(self) -> Card:
        card = Card("Целостность Windows",
                    "Когда система начинает вести себя странно — вылетают программы, "
                    "не ставятся обновления, пропадают элементы интерфейса — виноваты "
                    "обычно повреждённые системные файлы. У Windows есть встроенные "
                    "инструменты, которые это чинят, но они живут в командной строке "
                    "и печатают результат так, что понять его без поиска в интернете нельзя.")

        card.add(dim_label(
            "Правильный порядок: сначала проверить хранилище компонентов — из него "
            "берутся эталонные копии файлов. Если оно повреждено, SFC чинить нечем. "
            "Потом при необходимости починить хранилище, и только затем запускать SFC."))

        self.integrity_buttons = {}
        for key, title, why in integrity_core.STEPS:
            row = QHBoxLayout()
            row.setSpacing(10)
            button = (primary_button(title) if key == "dism_scan"
                      else ghost_button(title))
            button.clicked.connect(lambda _c=False, k=key: self._run_integrity(k))
            button.setMinimumWidth(250)
            self.integrity_buttons[key] = button
            row.addWidget(button)
            hint = QLabel(why)
            hint.setObjectName("Faint")
            hint.setWordWrap(True)
            row.addWidget(hint, 1)
            holder = QWidget()
            holder.setLayout(row)
            card.add(holder)

        self.integrity_bar = QProgressBar()
        self.integrity_bar.setVisible(False)
        card.add(self.integrity_bar)

        self.integrity_line = QLabel("")
        self.integrity_line.setObjectName("Faint")
        self.integrity_line.setWordWrap(True)
        card.add(self.integrity_line)

        self.integrity_result = QLabel("")
        self.integrity_result.setWordWrap(True)
        self.integrity_result.setTextFormat(Qt.RichText)
        self.integrity_result.setVisible(False)
        card.add(self.integrity_result)

        card.add(dim_label(
            "Проверки идут долго: хранилище 1-3 минуты, SFC 5-15, восстановление "
            "до 20. Компьютером можно пользоваться, окно останется отзывчивым."))
        return card

    def _run_integrity(self, step: str):
        if any(isinstance(w, IntegrityWorker) and w.isRunning()
               for w in self.workers):
            return
        if not is_admin():
            QMessageBox.information(
                self, "Нужны права",
                "Проверка целостности работает только с правами администратора. "
                "Перезапусти приложение через кнопку в левом меню.")
            return

        for button in self.integrity_buttons.values():
            button.setEnabled(False)
        self.integrity_result.setVisible(False)
        self.integrity_bar.setVisible(True)
        self.integrity_bar.setRange(0, 100)
        self.integrity_bar.setValue(0)
        self.integrity_line.setText("Запускаю…")

        worker = IntegrityWorker(step)
        worker.progress.connect(self._on_integrity_progress)
        worker.done.connect(self._on_integrity_done)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_integrity_progress(self, line: str, percent: int):
        if percent >= 0:
            self.integrity_bar.setValue(percent)
        # строки с полосой прогресса показывать незачем — они мельтешат
        if "%" not in line and len(line) > 3:
            self.integrity_line.setText(line[:160])

    def _on_integrity_done(self, result):
        self.integrity_bar.setVisible(False)
        self.integrity_line.setText("")
        for button in self.integrity_buttons.values():
            button.setEnabled(True)

        colour = _TONE.get(result.tone, theme.TEXT_DIM)
        parts = [f"<span style='color:{colour}; font-size:15px; font-weight:700;'>"
                 f"{result.title}</span>",
                 f"<span style='color:{theme.TEXT_DIM}'>{result.tool}</span>",
                 result.detail]
        for advice in result.advice:
            parts.append(f"<span style='color:{theme.WARN}'>→ {advice}</span>")
        if result.raw_tail:
            parts.append(f"<span style='color:{theme.TEXT_FAINT}; font-size:11px'>"
                         + result.raw_tail.replace("\n", "<br>") + "</span>")
        self.integrity_result.setText("<br><br>".join(parts))
        self.integrity_result.setVisible(True)

    # ------------------------------------------------------------ отчёт

    def _report_card(self) -> Card:
        card = Card("Отчёт о компьютере",
                    "Один текстовый файл со всем составом железа, драйверами, "
                    "накопителями, включёнными настройками и автозагрузкой. "
                    "Удобно отправить другу или в тему на форуме, когда просишь совета.")
        card.add(dim_label(
            "В отчёт не попадают личные данные: ни имён файлов, ни истории "
            "браузера, ни паролей. Только железо и настройки системы — "
            "об этом написано и в самом файле."))

        row = QHBoxLayout()
        row.setSpacing(10)
        self.report_btn = primary_button("Собрать отчёт")
        self.report_btn.clicked.connect(self._build_report)
        row.addWidget(self.report_btn)

        self.copy_btn = ghost_button("Скопировать")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_report)
        row.addWidget(self.copy_btn)

        self.save_btn = ghost_button("Сохранить в файл")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_report)
        row.addWidget(self.save_btn)
        row.addStretch()
        card.add_layout(row)

        self.report_status = QLabel("")
        self.report_status.setObjectName("Faint")
        self.report_status.setWordWrap(True)
        card.add(self.report_status)
        self._report_text = ""
        return card

    def _build_report(self):
        self.report_btn.setEnabled(False)
        self.report_btn.setText("Собираю…")
        self.report_status.setText(
            "Опрашиваю железо, драйверы и накопители. Это занимает 15-25 секунд: "
            "часть данных Windows отдаёт медленно.")
        worker = ReportWorker(self.ctx)
        worker.done.connect(self._on_report)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_report(self, text: str):
        self._report_text = text
        self.report_btn.setEnabled(True)
        self.report_btn.setText("Собрать заново")
        self.copy_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.report_status.setText(
            f"Отчёт готов: {len(text.splitlines())} строк. "
            "Скопируй его или сохрани в файл.")

    def _copy_report(self):
        QApplication.clipboard().setText(self._report_text)
        self.report_status.setText("Отчёт скопирован — можно вставлять куда угодно.")

    def _save_report(self):
        import time as _time
        default = f"КРЕМЕНЬ-отчёт-{_time.strftime('%Y-%m-%d')}.txt"
        path, _f = QFileDialog.getSaveFileName(
            self, "Куда сохранить отчёт", default, "Текстовый файл (*.txt)")
        if not path:
            return
        try:
            from pathlib import Path as _Path
            _Path(path).write_text(self._report_text, encoding="utf-8")
            self.report_status.setText(f"Сохранено: {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить: {exc}")

    # ------------------------------------------------------------ загрузка

    def _boot_card(self) -> Card:
        card = Card("Загрузка системы",
                    "Windows измеряет каждый свой старт и записывает, какие "
                    "приложения, драйверы и службы его замедлили. Эти данные "
                    "не показывает ни одна её собственная панель.")
        self.boot_status = QLabel("Читаю журнал загрузки…")
        self.boot_status.setObjectName("Dim")
        self.boot_status.setWordWrap(True)
        card.add(self.boot_status)

        self.boot_stats = QGridLayout()
        self.boot_stats.setHorizontalSpacing(20)
        card.add_layout(self.boot_stats)

        self.boot_chart = Sparkline(capacity=20)
        self.boot_chart.setVisible(False)
        card.add(self.boot_chart)

        self.boot_body = QVBoxLayout()
        self.boot_body.setSpacing(8)
        card.add_layout(self.boot_body)

        row = QHBoxLayout()
        self.enable_btn = primary_button("Включить журнал загрузки")
        self.enable_btn.setVisible(False)
        self.enable_btn.clicked.connect(self._enable_log)
        row.addWidget(self.enable_btn)
        refresh = ghost_button("Обновить")
        refresh.clicked.connect(self._load_boot)
        row.addWidget(refresh)
        row.addStretch()
        card.add_layout(row)
        return card

    def _load_boot(self):
        self.boot_status.setText("Читаю журнал загрузки…")
        worker = BootWorker()
        worker.done.connect(self._on_boot)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_boot(self, report, uptime: dict, fast_startup: bool):
        clear_layout(self.boot_stats)
        clear_layout(self.boot_body)

        col = 0
        if uptime.get("last_boot"):
            self.boot_stats.addWidget(
                KeyValue("Последняя загрузка", uptime["last_boot"]), 0, col)
            col += 1
            self.boot_stats.addWidget(
                KeyValue("Работает без перезагрузки",
                         f"{uptime['uptime_hours']:.1f} ч"), 0, col)
            col += 1

        if fast_startup:
            self.boot_stats.addWidget(
                KeyValue("Быстрый запуск", "включён", theme.WARN), 0, col)

        if not report.records:
            self.boot_status.setText(report.note or "Данных о загрузке нет.")
            self.enable_btn.setVisible(not report.enabled and is_admin())
            self.boot_chart.setVisible(False)
            if fast_startup:
                self.boot_body.addWidget(self._notice(
                    "Кстати, о «быстром запуске»: с ним Windows не выключается "
                    "по-настоящему, а сохраняет ядро на диск и поднимает обратно. "
                    "Старт быстрее, но накопленные проблемы драйверов не сбрасываются "
                    "и обновления применяются только после перезагрузки. Отключается "
                    "на странице «Оптимизация».", theme.WARN))
            if not report.enabled and not is_admin():
                self.boot_body.addWidget(self._notice(
                    "Чтобы включить журнал, нужны права администратора.",
                    theme.TEXT_DIM))
            return

        self.enable_btn.setVisible(False)
        latest = report.records[0]
        verdict, tone = latest.verdict
        self.boot_status.setText(
            f"Последняя загрузка заняла {latest.total_s} с — {verdict}. "
            f"В среднем за {len(report.records)} запусков: {report.average_s} с "
            f"(лучший {report.best_s} с, худший {report.worst_s} с). {report.trend}")

        self.boot_stats.addWidget(
            KeyValue("Последняя загрузка", f"{latest.total_s} с",
                     _TONE.get(tone, theme.TEXT)), 1, 0)
        self.boot_stats.addWidget(
            KeyValue("Основной путь", f"{latest.main_ms / 1000:.1f} с"), 1, 1)
        self.boot_stats.addWidget(
            KeyValue("После рабочего стола", f"{latest.post_ms / 1000:.1f} с"), 1, 2)

        self.boot_chart.setVisible(True)
        self.boot_chart.maximum = max((r.total_s for r in report.records), default=60) * 1.2
        for record in reversed(report.records):
            self.boot_chart.push(record.total_s)

        if report.culprits:
            section = Collapsible("Кто тормозит загрузку", opened=True,
                                  accent=theme.WARN)
            for culprit in report.culprits:
                section.add(self._culprit_row(culprit))
            self.boot_body.addWidget(section)
        else:
            self.boot_body.addWidget(dim_label(
                "Windows не отметила ни одного замедлителя — загрузка чистая."))

    def _culprit_row(self, culprit) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        top = QHBoxLayout()
        name = QLabel(culprit.name)
        name.setStyleSheet("font-weight: 600; font-size: 13px;")
        top.addWidget(name)
        top.addStretch()
        top.addWidget(Badge(culprit.kind, theme.TEXT_FAINT))
        colour = theme.DANGER if culprit.seconds >= 5 else theme.WARN
        top.addWidget(Badge(f"{culprit.seconds} с", colour))
        lay.addLayout(top)
        hint = QLabel(culprit.explanation +
                      (f" Замечено {culprit.count} раз." if culprit.count > 1 else ""))
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return block

    def _notice(self, text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {color}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {color}44; border-radius: 8px; padding: 10px;")
        return label

    def _enable_log(self):
        self.enable_btn.setEnabled(False)
        self.enable_btn.setText("Включаю…")
        worker = EnableLogWorker()
        worker.done.connect(self._on_log_enabled)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_log_enabled(self, ok: bool, message: str):
        self.enable_btn.setEnabled(True)
        self.enable_btn.setText("Включить журнал загрузки")
        QMessageBox.information(self, "Журнал загрузки" if ok else "Не удалось",
                                message)
        if ok:
            self._load_boot()

    # ------------------------------------------------------------ драйверы

    def _drivers_card(self) -> Card:
        card = Card("Драйверы устройств",
                    "Приложение ничего не скачивает и не ставит само: подсовывать "
                    "драйверы из интернета — верный способ убить систему. "
                    "Оно показывает возраст и ведёт на официальную страницу.")
        self.drivers_status = QLabel("Читаю список драйверов…")
        self.drivers_status.setObjectName("Dim")
        self.drivers_status.setWordWrap(True)
        card.add(self.drivers_status)

        self.drivers_body = QVBoxLayout()
        self.drivers_body.setSpacing(10)
        card.add_layout(self.drivers_body)

        row = QHBoxLayout()
        refresh = ghost_button("Перечитать")
        refresh.clicked.connect(self._load_drivers)
        row.addWidget(refresh)
        row.addStretch()
        card.add_layout(row)
        return card

    def _load_drivers(self):
        self.drivers_status.setText("Читаю список драйверов…")
        worker = DriversWorker()
        worker.done.connect(self._on_drivers)
        worker.finished.connect(lambda w=worker: self.workers.remove(w))
        self.workers.append(worker)
        worker.start()

    def _on_drivers(self, drivers: list):
        clear_layout(self.drivers_body)
        self.drivers_status.setText(drivers_core.summary(drivers))
        if not drivers:
            return

        stale = drivers_core.outdated(drivers)
        if stale:
            section = Collapsible("Стоит обновить", opened=True, accent=theme.WARN)
            for driver in stale:
                section.add(self._driver_row(driver, show_why=True))
            self.drivers_body.addWidget(section)

        groups: dict[str, list] = {}
        for driver in drivers:
            groups.setdefault(driver.group, []).append(driver)
        for group, items in groups.items():
            section = Collapsible(group, opened=False)
            for driver in items:
                section.add(self._driver_row(driver))
            self.drivers_body.addWidget(section)

        self.drivers_body.addWidget(dim_label(
            "Дата — это дата выпуска драйвера производителем, а не дата установки. "
            "Встроенные в Windows драйверы и виртуальные устройства из списка "
            "убраны: у них общая дата-заглушка 21.06.2006, и обновлять там нечего."))

    def _driver_row(self, driver, show_why: bool = False) -> QWidget:
        block = QWidget()
        lay = QVBoxLayout(block)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        name = QLabel(driver.device)
        name.setStyleSheet("font-weight: 600; font-size: 13px;")
        name.setWordWrap(True)
        top.addWidget(name, 1)

        verdict, tone = driver.verdict
        top.addWidget(Badge(verdict, _TONE.get(tone, theme.TEXT_DIM)))

        vendor = driver.vendor_key
        if vendor and tone in ("warn", "danger"):
            title, url = drivers_core.VENDOR_LINKS[vendor]
            link = ghost_button(f"Сайт {title}")
            link.clicked.connect(lambda _c=False, u=url: webbrowser.open(u))
            top.addWidget(link)
        lay.addLayout(top)

        info = QLabel(f"{driver.provider or 'производитель неизвестен'} · "
                      f"версия {driver.version} · от {driver.date} "
                      f"({driver.years_old} лет назад)")
        info.setObjectName("Faint")
        info.setWordWrap(True)
        lay.addWidget(info)

        if show_why and driver.why:
            why = QLabel("→ " + driver.why)
            why.setObjectName("Faint")
            why.setWordWrap(True)
            why.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
            lay.addWidget(why)
        return block
