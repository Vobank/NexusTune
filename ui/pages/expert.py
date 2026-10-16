"""Страница «Эксперт»: глубокие настройки ядра, драйверов и сетевого стека.

Отдельно от обычной оптимизации намеренно: здесь параметры, у которых есть
цена. Каждый показывает свой уровень риска, а самые серьёзные — ещё и красное
предупреждение с последствиями. Всё обратимо, снимок сохраняется отдельно
от снимка обычной оптимизации.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QLineEdit, QComboBox, QCheckBox,
)

from core import expert as expert_core
from core import optimizer
from core.context import AppContext
from core.util import is_admin

from .. import theme
from ..widgets import (
    Card, Badge, Collapsible, ToggleRow, page_header, scroll_page,
    primary_button, ghost_button, dim_label,
)

SNAPSHOT = "before_expert"

_RISK_ORDER = {"низкий": 0, "средний": 1, "высокий": 2}
_RISK_COLOR = {"низкий": theme.OK, "средний": theme.WARN, "высокий": theme.DANGER}


class ApplyWorker(QThread):
    progress = Signal(str, int, int)
    done = Signal(int, int, bool, list)

    def __init__(self, tweaks: list, wanted: dict[str, bool]):
        super().__init__()
        self.tweaks = {t.id: t for t in tweaks}
        self.wanted = wanted

    def run(self):
        items = list(self.wanted.items())
        ok = 0
        reboot = False
        failed: list[str] = []
        for index, (tid, enable) in enumerate(items, 1):
            tweak = self.tweaks.get(tid)
            if not tweak:
                continue
            self.progress.emit(tweak.title, index, len(items))
            if tweak.state() == enable:
                ok += 1
                continue
            if tweak.apply(enable):
                ok += 1
                reboot = reboot or tweak.reboot
            else:
                failed.append(tweak.title)
        self.done.emit(ok, len(items), reboot, failed)


class RestorePointWorker(QThread):
    done = Signal(bool)

    def run(self):
        self.done.emit(optimizer.create_restore_point(
            "КРЕМЕНЬ — перед экспертными настройками"))


class ExpertPage(QWidget):
    """Глубокие настройки системы."""

    changed = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: dict[str, ToggleRow] = {}
        self.sections: dict[str, Collapsible] = {}
        self.worker = None
        # Состояния читаются один раз на построение страницы. Раньше их
        # опрашивали дважды — для сводки и для строк, — и короткий кэш команд
        # успевал протухнуть между проходами: bcdedit и netsh запускались
        # заново, страница открывалась почти пять секунд.
        self._states: dict[str, bool] = {
            tweak.id: tweak.state() for tweak in ctx.expert_tweaks}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Экспертные настройки",
            "Параметры ядра, планировщика, памяти, драйвера видеокарты и сетевого "
            "стека. У большинства из них нет интерфейса в самой Windows."))

        lay.addWidget(self._warning_card())
        lay.addWidget(self._controls_card())

        groups = expert_core.categories(ctx.expert_tweaks)
        for category, tweaks in groups.items():
            worst = max((_RISK_ORDER.get(t.risk, 1) for t in tweaks), default=0)
            accent = [theme.OK, theme.WARN, theme.DANGER][worst]
            section = Collapsible(category, accent=accent,
                                  opened=(worst == 0))
            for tweak in tweaks:
                row = ToggleRow(tweak.id, tweak.title, tweak.desc, tweak.why,
                                tweak.risk, tweak.reboot,
                                expert=tweak.expert, warn=tweak.warn)
                row.set_state(self._states.get(tweak.id, False))
                self.rows[tweak.id] = row
                section.add(row)
            self.sections[category] = section
            lay.addWidget(section)

        lay.addStretch()

    # ------------------------------------------------------------ шапка

    def _warning_card(self) -> Card:
        card = Card()
        card.setStyleSheet(
            f"QFrame#Card {{ background: {theme.SURFACE}; "
            f"border: 1px solid {theme.WARN}66; border-radius: 14px; }}")
        title = QLabel("⚠  Читай перед тем, как трогать")
        title.setStyleSheet(f"color: {theme.WARN}; font-size: 15px; font-weight: 700;")
        card.add(title)
        card.add(dim_label(
            "Здесь нет ничего необратимого: у каждой галочки есть обратная операция, "
            "а состояние читается прямо из системы, а не запоминается приложением. "
            "Но цена у настроек разная. «Низкий» риск — можно включать смело. "
            "«Средний» — стоит проверить в своей игре. «Высокий» — читай красное "
            "предупреждение под настройкой целиком.\n\n"
            "Правильный порядок работы: сделай точку восстановления, включи одну "
            "группу, замерь результат в Кузнице, оставь то, что реально помогло. "
            "Включать всё подряд смысла нет — часть настроек друг друга гасит."))
        if not is_admin():
            card.add(self._notice(
                "Приложение запущено без прав администратора. Настройки ядра, "
                "загрузчика и сетевого стека сейчас недоступны — перезапусти "
                "с правами через кнопку в левом меню.", theme.DANGER))
        return card

    def _notice(self, text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {color}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {color}55; border-radius: 8px; padding: 10px;")
        return label

    def _controls_card(self) -> Card:
        card = Card()

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или описанию…")
        self.search.textChanged.connect(self._apply_filter)
        filters.addWidget(self.search, 1)

        self.risk_filter = QComboBox()
        self.risk_filter.addItems(["Любой риск", "Только низкий",
                                   "Низкий и средний", "Только высокий"])
        self.risk_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.risk_filter)

        self.only_on = QCheckBox("Только включённые")
        self.only_on.toggled.connect(self._apply_filter)
        filters.addWidget(self.only_on)
        card.add_layout(filters)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        safe_btn = primary_button("Включить безопасные")
        safe_btn.setToolTip("Все настройки с низким риском, помеченные как "
                            "рекомендованные.")
        safe_btn.clicked.connect(self.apply_safe)
        buttons.addWidget(safe_btn)

        selected_btn = ghost_button("Применить отмеченные")
        selected_btn.clicked.connect(self.apply_selected)
        buttons.addWidget(selected_btn)

        off_btn = ghost_button("Выключить всё экспертное")
        off_btn.clicked.connect(self.turn_all_off)
        buttons.addWidget(off_btn)

        restore_btn = ghost_button("Откатить по снимку")
        restore_btn.clicked.connect(self.restore_snapshot)
        buttons.addWidget(restore_btn)

        point_btn = ghost_button("Точка восстановления")
        point_btn.clicked.connect(self.make_restore_point)
        buttons.addWidget(point_btn)
        buttons.addStretch()
        card.add_layout(buttons)

        self.status = QLabel(self._summary())
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        card.add(self.status)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        card.add(self.bar)
        return card

    def _summary(self) -> str:
        total = len(self.ctx.expert_tweaks)
        on = sum(1 for value in self._states.values() if value)
        high = sum(1 for t in self.ctx.expert_tweaks if t.risk == "высокий")
        return (f"Доступно {total} экспертных настроек в {len(self.sections)} группах · "
                f"сейчас включено {on} · с высоким риском {high}")

    # ------------------------------------------------------------ фильтр

    def _apply_filter(self):
        text = self.search.text().strip().lower()
        mode = self.risk_filter.currentIndex()
        only_on = self.only_on.isChecked()
        allowed = {0: {"низкий", "средний", "высокий"}, 1: {"низкий"},
                   2: {"низкий", "средний"}, 3: {"высокий"}}[mode]

        visible_by_section: dict[str, int] = {}
        for tweak in self.ctx.expert_tweaks:
            row = self.rows.get(tweak.id)
            if row is None:
                continue
            ok = tweak.risk in allowed
            if ok and text:
                haystack = (f"{tweak.title} {tweak.desc} {tweak.why} "
                            f"{tweak.category} {tweak.id}").lower()
                ok = text in haystack
            if ok and only_on:
                ok = row.is_checked()
            row.setVisible(ok)
            if ok:
                visible_by_section[tweak.category] = \
                    visible_by_section.get(tweak.category, 0) + 1

        filtering = bool(text) or mode != 0 or only_on
        for category, section in self.sections.items():
            count = visible_by_section.get(category, 0)
            section.setVisible(count > 0)
            if filtering and count:
                section.set_open(True)

    # ------------------------------------------------------------ действия

    def _start(self, wanted: dict[str, bool], label: str):
        if not wanted:
            self.status.setText("Нечего применять.")
            return
        optimizer.save_snapshot(self.ctx.expert_tweaks, SNAPSHOT)
        self.bar.setVisible(True)
        self.bar.setRange(0, len(wanted))
        self.status.setText(label)
        self.worker = ApplyWorker(self.ctx.expert_tweaks, wanted)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def apply_safe(self):
        wanted = {t.id: True for t in self.ctx.expert_tweaks
                  if t.risk == "низкий" and t.recommended}
        self._start(wanted, "Включаю безопасные настройки…")

    def apply_selected(self):
        wanted = {tid: row.is_checked() for tid, row in self.rows.items()}
        risky = [self._tweak(tid).title for tid, want in wanted.items()
                 if want and self._tweak(tid).risk == "высокий"
                 and not self._tweak(tid).state()]
        if risky and not self._confirm_risky(risky):
            return
        self._start(wanted, "Применяю отмеченные настройки…")

    def turn_all_off(self):
        answer = QMessageBox.question(
            self, "Выключить всё экспертное",
            "Вернуть все экспертные настройки к заводскому поведению Windows?\n\n"
            "Обычная оптимизация со страницы «Оптимизация» не тронется.",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        wanted = {t.id: False for t in self.ctx.expert_tweaks}
        self._start(wanted, "Возвращаю заводское поведение…")

    def _tweak(self, tid: str):
        for t in self.ctx.expert_tweaks:
            if t.id == tid:
                return t
        return None

    def _confirm_risky(self, titles: list[str]) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Настройки с высоким риском")
        box.setIcon(QMessageBox.Warning)
        box.setText("Ты включаешь настройки, у которых есть серьёзная цена:")
        box.setInformativeText(
            "\n".join(f"• {t}" for t in titles) +
            "\n\nПрочитай красные предупреждения под ними. Рекомендую сначала "
            "сделать точку восстановления Windows.")
        yes = box.addButton("Понимаю, применить", QMessageBox.AcceptRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        return box.clickedButton() is yes

    def _on_progress(self, title: str, index: int, total: int):
        self.bar.setValue(index)
        self.status.setText(f"[{index}/{total}] {title}")

    def _on_done(self, ok: int, total: int, reboot: bool, failed: list):
        self.bar.setVisible(False)
        self.refresh_states()
        text = f"Применено {ok} из {total}. "
        if failed:
            text += f"Не удалось: {', '.join(failed[:4])}"
            if len(failed) > 4:
                text += f" и ещё {len(failed) - 4}"
            text += ". Скорее всего нужны права администратора. "
        if reboot:
            text += "Часть изменений заработает после перезагрузки."
        self.status.setText(text + "\n" + self._summary())
        self.changed.emit()

    def restore_snapshot(self):
        if optimizer.load_snapshot(SNAPSHOT) is None:
            self.status.setText("Снимка нет — сначала примени хотя бы одну настройку.")
            return
        answer = QMessageBox.question(
            self, "Откат", "Вернуть экспертные настройки к состоянию из снимка?",
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        ok, total = optimizer.restore_snapshot(self.ctx.expert_tweaks, SNAPSHOT)
        self.refresh_states()
        self.status.setText(f"Возвращено {ok} из {total}.\n" + self._summary())

    def make_restore_point(self):
        if not is_admin():
            self.status.setText("Точку восстановления можно создать только "
                                "с правами администратора.")
            return
        self.status.setText("Создаю точку восстановления Windows, до минуты…")
        self.worker = RestorePointWorker()
        self.worker.done.connect(
            lambda ok: self.status.setText(
                "Точка восстановления создана — теперь можно экспериментировать."
                if ok else
                "Не удалось создать точку. Проверь, включена ли защита системы "
                "в свойствах системы."))
        self.worker.start()

    def refresh_states(self):
        """Перечитывает состояния из системы одним проходом."""
        from core import expert as expert_module
        expert_module.invalidate_cache()
        self._states = {t.id: t.state() for t in self.ctx.expert_tweaks}
        for tweak in self.ctx.expert_tweaks:
            row = self.rows.get(tweak.id)
            if row:
                row.set_state(self._states.get(tweak.id, False))
