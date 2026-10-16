"""Страница профилей: одна кнопка перестраивает весь компьютер под задачу."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QDialog,
    QDialogButtonBox, QTextEdit, QProgressBar, QCheckBox, QComboBox, QFrame,
)

from core import profiles as profiles_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, glow, clear_layout,
)


class ApplyWorker(QThread):
    """Применяет профиль в фоне."""
    step = Signal(str)
    done = Signal(list)

    def __init__(self, ctx: AppContext, profile):
        super().__init__()
        self.ctx = ctx
        self.profile = profile

    def run(self):
        try:
            steps = profiles_core.apply_profile(self.ctx, self.profile, self.step.emit)
        except Exception as exc:
            steps = [f"Ошибка: {exc}"]
        self.done.emit(steps)


class ProfileCard(QFrame):
    """Крупная карточка профиля."""

    clicked = Signal(str)

    def __init__(self, profile, active: bool, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.setObjectName("Card")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(268)
        self._set_active(active)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        top = QHBoxLayout()
        icon = QLabel(profile.icon)
        icon.setStyleSheet("font-size: 26px;")
        top.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel(profile.title)
        name.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {profile.color};")
        titles.addWidget(name)
        sub = QLabel(profile.subtitle)
        sub.setObjectName("Dim")
        titles.addWidget(sub)
        top.addLayout(titles)
        top.addStretch()
        self.active_badge = Badge("активен", theme.OK)
        self.active_badge.setVisible(active)
        top.addWidget(self.active_badge)
        lay.addLayout(top)

        desc = QLabel(profile.description)
        desc.setObjectName("Dim")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        for line in profile.highlights[:4]:
            item = QLabel("•  " + line)
            item.setObjectName("Faint")
            item.setWordWrap(True)
            lay.addWidget(item)

        lay.addStretch()
        button = primary_button(f"Включить «{profile.title}»")
        button.setStyleSheet(
            f"background: {profile.color}; border: none; color: #06111a; "
            "font-weight: 700; border-radius: 9px; padding: 10px;")
        button.clicked.connect(lambda: self.clicked.emit(profile.key))
        lay.addWidget(button)

    def _set_active(self, active: bool):
        if active:
            self.setStyleSheet(
                f"#Card {{ border: 2px solid {self.profile.color}; "
                f"background: {theme.SURFACE}; border-radius: 14px; }}")
        else:
            self.setStyleSheet("")

    def set_active(self, active: bool):
        self._set_active(active)
        self.active_badge.setVisible(active)


class ConfirmDialog(QDialog):
    """Показывает, что именно изменится, до применения."""

    def __init__(self, ctx: AppContext, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Профиль «{profile.title}»")
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.STYLESHEET)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)

        head = QLabel(f"{profile.icon}  {profile.title}")
        head.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {profile.color};")
        lay.addWidget(head)
        lay.addWidget(dim_label(profile.description))

        # что поменяется прямо сейчас
        will_change = []
        for tweak_id, want in profile.tweaks.items():
            tweak = ctx.tweak(tweak_id)
            if tweak and tweak.state() != want:
                will_change.append(("Включить" if want else "Выключить") + f": {tweak.title}")
        if profile.disable_tasks:
            will_change.append("Усыпить мусорные задачи планировщика")
        if profile.trim_startup:
            will_change.append("Отключить тяжёлые программы из автозагрузки")
        if profile.close_apps:
            will_change.append(f"Закрыть фоновые программы ({len(profile.close_apps)} шт.)")
        if profile.clean_junk:
            will_change.append("Убрать временные файлы и старый кэш")
        if profile.gpu_power_percent is not None:
            will_change.append(f"Предел мощности видеокарты: {profile.gpu_power_percent}%")

        lay.addWidget(SectionTitle(f"Будет сделано — {len(will_change)} действий"))
        listing = QTextEdit()
        listing.setReadOnly(True)
        listing.setPlainText("\n".join(f"• {t}" for t in will_change)
                             or "Система уже в этом состоянии — изменится только план питания.")
        listing.setMaximumHeight(200)
        lay.addWidget(listing)

        note = QLabel("Перед применением сохраняется снимок. Вернуть всё можно "
                      "профилем «Как с завода» или кнопкой отката на странице оптимизации.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        lay.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class ProfilesPage(QWidget):
    """Профили и автопилот."""

    profile_applied = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.cards: dict[str, ProfileCard] = {}
        self.worker: ApplyWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Профили",
            "Нажимаешь одну кнопку — приложение само расставляет все галочки, "
            "переключает питание, усыпляет лишние задачи и убирает мусор. "
            "Ничего настраивать вручную не нужно."))

        lay.addWidget(self._status_card())

        grid = QGridLayout()
        grid.setSpacing(14)
        active = self.ctx.config.get("active_profile", "")
        for index, profile in enumerate(profiles_core.PROFILES):
            card = ProfileCard(profile, profile.key == active)
            card.clicked.connect(self.on_profile_clicked)
            self.cards[profile.key] = card
            grid.addWidget(card, index // 3, index % 3)
        lay.addLayout(grid)

        lay.addWidget(self._autopilot_card())
        lay.addStretch()

    # ------------------------------------------------------------ карточки

    def _status_card(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(14)

        self.status_title = QLabel()
        self.status_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        row.addWidget(self.status_title)
        row.addStretch()
        card.add_layout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        card.add(self.progress)

        self.status_text = QLabel("")
        self.status_text.setObjectName("Dim")
        self.status_text.setWordWrap(True)
        card.add(self.status_text)

        self._refresh_status()
        return card

    def _refresh_status(self):
        key = self.ctx.config.get("active_profile", "")
        profile = profiles_core.by_key(key)
        if profile:
            self.status_title.setText(f"Сейчас активен профиль: {profile.icon} {profile.title}")
        else:
            self.status_title.setText("Профиль пока не выбран")
            self.status_text.setText(
                "Выбери профиль под свою задачу — остальное приложение сделает само.")

    def _autopilot_card(self) -> Card:
        card = Card(
            "Автопилот",
            "Приложение само заметит запуск игры, включит игровой профиль, "
            "а после выхода вернёт всё обратно. Тебе не нужно ничего нажимать.")

        self.auto_box = QCheckBox("Переключать профили автоматически при запуске игры")
        self.auto_box.setChecked(bool(self.ctx.config.get("autopilot", False)))
        self.auto_box.toggled.connect(self.on_autopilot_toggled)
        card.add(self.auto_box)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("В игре:", objectName="Dim"))
        self.game_combo = QComboBox()
        row.addWidget(self.game_combo)
        row.addWidget(QLabel("После выхода:", objectName="Dim"))
        self.after_combo = QComboBox()
        row.addWidget(self.after_combo)
        row.addStretch()
        for combo, config_key, default in (
                (self.game_combo, "autopilot_profile", "gaming"),
                (self.after_combo, "autopilot_restore", "stock")):
            for profile in profiles_core.PROFILES:
                combo.addItem(f"{profile.icon} {profile.title}", profile.key)
            saved = self.ctx.config.get(config_key, default)
            index = combo.findData(saved)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(self.on_autopilot_choice)
        card.add_layout(row)

        self.auto_status = QLabel("")
        self.auto_status.setObjectName("Faint")
        self.auto_status.setWordWrap(True)
        card.add(self.auto_status)
        self._refresh_autopilot_status()
        return card

    def _refresh_autopilot_status(self):
        watcher = self.ctx.watcher
        if not watcher:
            self.auto_status.setText("Автопилот запустится вместе с приложением.")
            return
        if watcher.current:
            self.auto_status.setText(f"Сейчас идёт игра: {watcher.current.game}.")
        elif watcher.enabled:
            self.auto_status.setText("Автопилот следит за запуском игр.")
        else:
            self.auto_status.setText("Автопилот выключен.")

    # ------------------------------------------------------------ действия

    def on_profile_clicked(self, key: str):
        profile = profiles_core.by_key(key)
        if not profile:
            return
        dialog = ConfirmDialog(self.ctx, profile, self)
        if dialog.exec() != QDialog.Accepted:
            return

        self.progress.setVisible(True)
        self.status_title.setText(f"Применяю профиль «{profile.title}»…")
        for card in self.cards.values():
            card.setEnabled(False)

        self.worker = ApplyWorker(self.ctx, profile)
        self.worker.step.connect(lambda text: self.status_text.setText(text))
        self.worker.done.connect(lambda steps: self.on_applied(profile, steps))
        self.worker.start()

    def on_applied(self, profile, steps: list[str]):
        self.progress.setVisible(False)
        for key, card in self.cards.items():
            card.setEnabled(True)
            card.set_active(key == profile.key)
        self._refresh_status()
        self.status_text.setText(steps[-1] if steps else "Готово.")
        self.profile_applied.emit()
        self._report(f"Профиль «{profile.title}» применён", steps)

    def on_autopilot_toggled(self, checked: bool):
        self.ctx.config["autopilot"] = checked
        self.ctx.save()
        watcher = self.ctx.watcher
        if watcher:
            if checked:
                watcher.start()
            else:
                watcher.stop()
        self._refresh_autopilot_status()

    def on_autopilot_choice(self):
        self.ctx.config["autopilot_profile"] = self.game_combo.currentData()
        self.ctx.config["autopilot_restore"] = self.after_combo.currentData()
        self.ctx.save()
        watcher = self.ctx.watcher
        if watcher:
            watcher.game_profile = self.game_combo.currentData()
            watcher.restore_profile = self.after_combo.currentData()

    def _report(self, title: str, steps: list[str]):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(600)
        dialog.setStyleSheet(theme.STYLESHEET)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(22, 20, 22, 20)
        head = QLabel(title)
        head.setObjectName("CardTitle")
        lay.addWidget(head)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(f"• {s}" for s in steps))
        text.setMinimumHeight(260)
        lay.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        lay.addWidget(buttons)
        dialog.exec()

    # ------------------------------------------------------------ обновление

    def tick(self, reading: dict):
        self._refresh_autopilot_status()
