"""Страница диагностики: что не так с компьютером и тест производительности."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QProgressBar,
    QFrame, QLineEdit, QComboBox,
)

from core import bench as bench_core
from core import health as health_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, KeyValue, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, clear_layout,
)

_STATUS_COLOR = {
    health_core.OK: theme.OK,
    health_core.WARN: theme.WARN,
    health_core.BAD: theme.DANGER,
}
_STATUS_MARK = {health_core.OK: "✔", health_core.WARN: "!", health_core.BAD: "✖"}


class CheckWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

    def run(self):
        checks = health_core.run_checks(self.ctx, self.progress.emit)
        self.done.emit(checks)


class FixWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, fix):
        super().__init__()
        self.fix = fix

    def run(self):
        try:
            ok, message = self.fix()
        except Exception as exc:
            ok, message = False, str(exc)
        self.done.emit(ok, message)


class BenchWorker(QThread):
    progress = Signal(str, int)
    done = Signal(object)

    def __init__(self, machine, label: str):
        super().__init__()
        self.machine = machine
        self.label = label

    def run(self):
        try:
            result = bench_core.run_full(self.machine, self.label, self.progress.emit)
        except Exception:
            result = None
        self.done.emit(result)


class CheckRow(QFrame):
    """Строка проверки с кнопкой починки."""

    fix_requested = Signal(object)

    def __init__(self, check: health_core.Check, parent=None):
        super().__init__(parent)
        self.check = check
        self.setObjectName("CardFlat")
        color = _STATUS_COLOR.get(check.status, theme.TEXT_DIM)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(5)

        top = QHBoxLayout()
        mark = QLabel(_STATUS_MARK.get(check.status, "?"))
        mark.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: 800;")
        mark.setFixedWidth(20)
        top.addWidget(mark)

        title = QLabel(check.title)
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        top.addWidget(title)
        top.addStretch()

        value = QLabel(check.value)
        value.setStyleSheet(f"color: {color}; font-weight: 700;")
        top.addWidget(value)

        if check.fix:
            self.fix_btn = ghost_button(check.fix_label or "Починить")
            self.fix_btn.setStyleSheet(
                f"background: transparent; border: 1px solid {color}; color: {color}; "
                "border-radius: 8px; padding: 5px 12px;")
            self.fix_btn.clicked.connect(lambda: self.fix_requested.emit(self))
            top.addWidget(self.fix_btn)
        else:
            self.fix_btn = None
        lay.addLayout(top)

        if check.why:
            why = QLabel(check.why)
            why.setObjectName("Faint")
            why.setWordWrap(True)
            why.setContentsMargins(20, 0, 0, 0)
            lay.addWidget(why)


class HealthPage(QWidget):
    """Диагностика и замер производительности."""

    fixed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.checks: list[health_core.Check] = []
        self.workers: list[QThread] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Диагностика",
            "Приложение проверяет компьютер по списку и предлагает починить то, "
            "что чинится кнопкой. Ничего не меняется, пока ты сам не нажмёшь."))

        lay.addWidget(self._score_card())
        lay.addWidget(self._checks_card())
        lay.addWidget(self._bench_card())
        lay.addStretch()

        self.run_checks()

    # ------------------------------------------------------------ оценка

    def _score_card(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(24)

        left = QVBoxLayout()
        left.setSpacing(0)
        self.score_value = QLabel("—")
        self.score_value.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {theme.ACCENT};")
        left.addWidget(self.score_value)
        self.score_label = QLabel("Проверяю…")
        self.score_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        left.addWidget(self.score_label)
        left.addWidget(QLabel("Здоровье системы из 100", objectName="Faint"))
        row.addLayout(left)

        mid = QVBoxLayout()
        self.score_summary = QLabel("")
        self.score_summary.setObjectName("Dim")
        self.score_summary.setWordWrap(True)
        mid.addWidget(self.score_summary)
        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, len(health_core._CHECKS))
        mid.addWidget(self.score_bar)
        row.addLayout(mid, 1)

        right = QVBoxLayout()
        self.fix_all_btn = primary_button("Починить всё разом")
        self.fix_all_btn.clicked.connect(self.fix_all)
        self.fix_all_btn.setEnabled(False)
        right.addWidget(self.fix_all_btn)
        again = ghost_button("Проверить заново")
        again.clicked.connect(self.run_checks)
        right.addWidget(again)
        row.addLayout(right)

        card.add_layout(row)
        self.fix_status = QLabel("")
        self.fix_status.setObjectName("Faint")
        self.fix_status.setWordWrap(True)
        card.add(self.fix_status)
        return card

    def _checks_card(self) -> Card:
        card = Card("Результаты проверки")
        self.checks_box = QVBoxLayout()
        self.checks_box.setSpacing(7)
        card.add_layout(self.checks_box)
        return card

    # ------------------------------------------------------------ тест скорости

    def _bench_card(self) -> Card:
        card = Card(
            "Тест производительности",
            "Замеряет процессор, память и диск. Прогони до оптимизации и после — "
            "увидишь разницу в цифрах, а не на словах.")

        row = QHBoxLayout()
        self.bench_btn = primary_button("▶  Запустить тест (~15 секунд)")
        self.bench_btn.clicked.connect(self.run_bench)
        row.addWidget(self.bench_btn)

        row.addWidget(QLabel("Подпись:", objectName="Dim"))
        self.bench_label = QLineEdit()
        self.bench_label.setPlaceholderText("например: до оптимизации")
        self.bench_label.setMaximumWidth(240)
        row.addWidget(self.bench_label)
        row.addStretch()
        card.add_layout(row)

        self.bench_bar = QProgressBar()
        self.bench_bar.setRange(0, 100)
        self.bench_bar.setVisible(False)
        card.add(self.bench_bar)

        self.bench_status = QLabel("")
        self.bench_status.setObjectName("Dim")
        self.bench_status.setWordWrap(True)
        card.add(self.bench_status)

        self.bench_results = QVBoxLayout()
        card.add_layout(self.bench_results)
        self._render_bench_history()
        return card

    def _render_bench_history(self):
        clear_layout(self.bench_results)
        results = bench_core.load_results()
        if not results:
            hint = QLabel("Тест ещё не запускался.")
            hint.setObjectName("Faint")
            self.bench_results.addWidget(hint)
            return

        latest = results[0]
        head = QLabel(f"Последний результат: {latest.total_score} очков")
        head.setStyleSheet(
            f"font-size: 19px; font-weight: 800; color: "
            f"{theme.OK if latest.total_score >= 1000 else theme.WARN};")
        self.bench_results.addWidget(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        details = [
            ("Одно ядро", latest.single_score, f"{latest.single_ops / 1e6:.1f} млн оп/с"),
            ("Все ядра", latest.multi_score, f"{latest.multi_mbs:.0f} МБ/с"),
            ("Память", latest.memory_score, f"{latest.memory_mbs:.0f} МБ/с"),
            ("Диск", latest.disk_score,
             f"запись {latest.disk_write_mbs:.0f} МБ/с, чтение {latest.disk_read_mbs:.0f} МБ/с"),
        ]
        for index, (name, score, detail) in enumerate(details):
            grid.addWidget(KeyValue(name, f"{score} очков", theme.ACCENT), index, 0)
            note = QLabel(detail)
            note.setObjectName("Faint")
            grid.addWidget(note, index, 1)
        self.bench_results.addLayout(grid)

        note = QLabel("Эталон в 1000 очков — типичный игровой компьютер среднего класса. "
                      "Больше — быстрее эталона.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        self.bench_results.addWidget(note)

        if len(results) > 1:
            self.bench_results.addWidget(SectionTitle("Сравнить два прогона"))
            self._results_cache = results

            picker = QHBoxLayout()
            picker.setSpacing(8)
            self.bench_old = QComboBox()
            self.bench_new = QComboBox()
            for result in results:
                title = result.when + (f" · {result.label}" if result.label else "")
                title += f" — {result.total_score} очков"
                self.bench_old.addItem(title)
                self.bench_new.addItem(title)
            # по умолчанию: предыдущий -> последний
            self.bench_old.setCurrentIndex(1)
            self.bench_new.setCurrentIndex(0)
            self.bench_old.currentIndexChanged.connect(self._render_comparison)
            self.bench_new.currentIndexChanged.connect(self._render_comparison)
            picker.addWidget(self.bench_old, 1)
            arrow = QLabel("→")
            arrow.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 15px;")
            picker.addWidget(arrow)
            picker.addWidget(self.bench_new, 1)
            self.bench_results.addLayout(picker)

            self.bench_compare = QVBoxLayout()
            self.bench_compare.setSpacing(3)
            self.bench_results.addLayout(self.bench_compare)
            self._render_comparison()

    def _render_comparison(self):
        """Сравнивает два выбранных прогона."""
        results = getattr(self, "_results_cache", [])
        if len(results) < 2:
            return
        clear_layout(self.bench_compare)

        old_index = self.bench_old.currentIndex()
        new_index = self.bench_new.currentIndex()
        if old_index == new_index:
            hint = QLabel("Выбран один и тот же прогон — выбери разные.")
            hint.setObjectName("Faint")
            self.bench_compare.addWidget(hint)
            return

        older, newer = results[old_index], results[new_index]
        # прогоны с разной калибровкой сравнивать нельзя: цифры несопоставимы
        if older.calibration != newer.calibration:
            warn = QLabel(
                "Эти прогоны сделаны с разной калибровкой эталона — сравнивать "
                "их некорректно. Прогони тест заново.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
            self.bench_compare.addWidget(warn)
            return

        for name, before, after, delta in bench_core.compare(newer, older):
            color = (theme.OK if delta > 1 else
                     theme.DANGER if delta < -1 else theme.TEXT_DIM)
            sign = "+" if delta > 0 else ""
            row = QLabel(
                f"<b>{name}</b>: было {before:.0f} → стало {after:.0f} "
                f"<span style='color:{color}'>({sign}{delta}%)</span>")
            row.setObjectName("Dim")
            self.bench_compare.addWidget(row)

        rows = bench_core.compare(newer, older)
        total = next((d for n, _b, _a, d in rows if n == "Общий счёт"), 0.0)
        disk = next((d for n, _b, _a, d in rows if n == "Диск"), 0.0)
        text = self._compare_verdict(total)

        # Диск — самая шумная часть теста: состояние кэша SSD и фоновая запись
        # легко меняют его результат вдвое, а он весит пятую часть общего счёта.
        if abs(disk) >= 25:
            text += ("\n\nВнимание: сильнее всего разошёлся результат диска "
                     f"({disk:+.0f}%). Это самая шумная часть теста — на неё "
                     "влияют кэш SSD и фоновая запись. Смотри на счёт "
                     "процессора и памяти: они куда стабильнее.")
        verdict = QLabel(text)
        verdict.setWordWrap(True)
        verdict.setStyleSheet(
            f"color: {theme.TEXT}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 8px; "
            "padding: 9px; font-size: 12px;")
        self.bench_compare.addWidget(verdict)

    @staticmethod
    def _compare_verdict(delta: float) -> str:
        """Честный вывод: разница в пару процентов — это шум, а не результат."""
        if delta >= 5:
            return (f"Стало быстрее на {delta:.0f}%. Это заметная разница, "
                    "она не спишется на погрешность.")
        if delta >= 2:
            return (f"Прибавка {delta:.0f}% — небольшая, но за пределами обычного "
                    "разброса замеров.")
        if delta > -2:
            return ("Разница в пределах погрешности: два прогона на одной и той же "
                    "системе легко расходятся на пару процентов. Считай, что "
                    "ничего не изменилось.")
        if delta > -5:
            return (f"Стало медленнее на {abs(delta):.0f}%. Проверь, не запущено ли "
                    "что-то тяжёлое в фоне, и прогони ещё раз.")
        return (f"Стало заметно медленнее — на {abs(delta):.0f}%. Что-то из "
                "изменений навредило. Посмотри Хронику: она покажет, что "
                "менялось между прогонами.")

    # ------------------------------------------------------------ действия

    def run_checks(self):
        self.score_label.setText("Проверяю…")
        self.score_bar.setVisible(True)
        self.fix_all_btn.setEnabled(False)
        worker = CheckWorker(self.ctx)
        worker.progress.connect(lambda i, n: self.score_bar.setValue(i))
        worker.done.connect(self._on_checks)
        self.workers.append(worker)
        worker.start()

    def _on_checks(self, checks: list):
        self.checks = checks
        self.score_bar.setVisible(False)
        clear_layout(self.checks_box)

        score, label = health_core.health_score(checks)
        self.score_value.setText(str(score))
        color = theme.OK if score >= 85 else (theme.WARN if score >= 60 else theme.DANGER)
        self.score_value.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {color};")
        self.score_label.setText(label)

        problems = [c for c in checks if c.status != health_core.OK]
        fixable = [c for c in problems if c.fix]
        if problems:
            self.score_summary.setText(
                f"Найдено замечаний: {len(problems)}. "
                f"Из них чинится кнопкой: {len(fixable)}. "
                "Остальное требует решений, которые приложение принимать не должно.")
        else:
            self.score_summary.setText("Замечаний нет — система в порядке.")
        self.fix_all_btn.setEnabled(bool(fixable))

        for check in checks:
            row = CheckRow(check)
            row.fix_requested.connect(self._run_fix)
            self.checks_box.addWidget(row)

    def _run_fix(self, row: CheckRow):
        if row.fix_btn:
            row.fix_btn.setEnabled(False)
            row.fix_btn.setText("Чиню…")
        worker = FixWorker(row.check.fix)
        worker.done.connect(lambda ok, msg: self._on_fixed(ok, msg))
        self.workers.append(worker)
        worker.start()

    def _on_fixed(self, ok: bool, message: str):
        self.fix_status.setText(("✔  " if ok else "✖  ") + message)
        self.fixed.emit()
        self.run_checks()

    def fix_all(self):
        fixable = [c for c in self.checks if c.fix and c.status != health_core.OK]
        if not fixable:
            return
        self.fix_all_btn.setEnabled(False)
        self.fix_status.setText(f"Чиню {len(fixable)} замечаний…")
        self._fix_queue = list(fixable)
        self._fix_results: list[str] = []
        self._next_fix()

    def _next_fix(self):
        if not self._fix_queue:
            self.fix_status.setText(" · ".join(self._fix_results[-3:])
                                    or "Готово.")
            self.fix_all_btn.setEnabled(True)
            self.fixed.emit()
            self.run_checks()
            return
        check = self._fix_queue.pop(0)
        worker = FixWorker(check.fix)

        def handle(ok: bool, message: str):
            self._fix_results.append(message)
            self.fix_status.setText(message)
            self._next_fix()

        worker.done.connect(handle)
        self.workers.append(worker)
        worker.start()

    def run_bench(self):
        warnings = bench_core.readiness_warnings()
        if warnings:
            self.bench_status.setText("⚠  " + "  ".join(warnings))
        self.bench_btn.setEnabled(False)
        self.bench_bar.setVisible(True)
        worker = BenchWorker(self.ctx.machine, self.bench_label.text().strip())
        worker.progress.connect(self._on_bench_progress)
        worker.done.connect(self._on_bench_done)
        self.workers.append(worker)
        worker.start()

    def _on_bench_progress(self, text: str, percent: int):
        self.bench_bar.setValue(percent)
        self.bench_status.setText(text)

    def _on_bench_done(self, result):
        self.bench_btn.setEnabled(True)
        self.bench_bar.setVisible(False)
        if result is None:
            self.bench_status.setText("Тест не удалось завершить.")
            return
        self.bench_status.setText(f"Готово: {result.total_score} очков.")
        self._render_bench_history()
