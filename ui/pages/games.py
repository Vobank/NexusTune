"""Страница игр: подбор оптимальных настроек под конкретное железо."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QButtonGroup, QScrollArea, QGridLayout,
    QFileDialog, QCompleter, QFrame, QSizePolicy, QMessageBox, QComboBox,
    QCheckBox,
)

from core.context import AppContext
from core import gameconfig
from core import gameprofiles
from core.games import Game, scan_all
from core.tuner import PRESETS, PRESET_INFO, build_all, profile_for, Recommendation
from core.util import log, run

from .. import theme
from ..widgets import (
    Card, Badge, SectionTitle, page_header, dim_label,
    primary_button, ghost_button, clear_layout,
)

# Названия из базы для автодополнения при ручном вводе
SUGGESTIONS = [
    "Counter-Strike 2", "Valorant", "Dota 2", "Apex Legends", "Fortnite", "PUBG",
    "Call of Duty: Warzone", "Battlefield 2042", "Escape from Tarkov", "Cyberpunk 2077",
    "Red Dead Redemption 2", "Grand Theft Auto V", "The Witcher 3", "Elden Ring",
    "Baldur's Gate 3", "Starfield", "Hogwarts Legacy", "Alan Wake 2", "Black Myth: Wukong",
    "S.T.A.L.K.E.R. 2", "The Last of Us Part I", "God of War", "Marvel's Spider-Man",
    "Horizon Forbidden West", "Assassin's Creed Mirage", "Far Cry 6", "Resident Evil 4",
    "Forza Horizon 5", "Microsoft Flight Simulator", "Minecraft", "Rust", "Valheim",
    "Palworld", "War Thunder", "Destiny 2", "Genshin Impact", "Path of Exile 2",
    "The Finals", "Helldivers 2", "Marvel Rivals", "Overwatch 2", "Rainbow Six Siege",
    "Cities: Skylines II", "Satisfactory", "Total War: Warhammer III",
]


class ScanWorker(QThread):
    """Сканирует лаунчеры в фоне."""
    finished_scan = Signal(list)

    def run(self):
        try:
            self.finished_scan.emit(scan_all())
        except Exception as exc:
            log(f"Сканирование игр не удалось: {exc}")
            self.finished_scan.emit([])


class PresetButton(QPushButton):
    """Кнопка профиля с прогнозом кадров."""

    def __init__(self, rec: Recommendation, target: int, parent=None):
        super().__init__(parent)
        self.setObjectName("Preset")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)

        top = QHBoxLayout()
        title = QLabel(f"{rec.icon}  {rec.title}")
        title.setStyleSheet("font-size: 15px; font-weight: 700; background: transparent;")
        top.addWidget(title)
        top.addStretch()
        lay.addLayout(top)

        fps = QLabel(f"~{rec.fps} FPS")
        fps.setStyleSheet(f"font-size: 19px; font-weight: 800; background: transparent; "
                          f"color: {theme.fps_color(rec.fps, target)};")
        lay.addWidget(fps)

        sub = QLabel(rec.subtitle)
        sub.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_DIM}; background: transparent;")
        sub.setWordWrap(True)
        lay.addWidget(sub)


class GamesPage(QWidget):
    """Список игр и рекомендации настроек."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.games: list[Game] = []
        self.current_name: str = ""
        self.recs: dict[str, Recommendation] = {}
        self.current_preset = "balanced"
        self.worker: ScanWorker | None = None
        try:
            self.configs = gameconfig.detect_configs()
        except Exception:
            self.configs = []

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 26)
        root.setSpacing(16)

        root.addWidget(page_header(
            "Настройки игр",
            "Приложение считает ожидаемый FPS для твоего железа и собирает набор "
            "настроек по правилам: что снижать в первую очередь, а что не трогать вовсе."))

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_left(), 2)
        body.addWidget(self._build_right(), 5)
        root.addLayout(body)

        self.refresh_games()

    # ------------------------------------------------------------ левая колонка

    def _build_left(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти или ввести название игры вручную…")
        completer = QCompleter(SUGGESTIONS)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.search.setCompleter(completer)
        self.search.textChanged.connect(self._filter_list)
        self.search.returnPressed.connect(self._use_typed_name)
        search_row.addWidget(self.search)

        add_btn = ghost_button("＋")
        add_btn.setFixedWidth(40)
        add_btn.setToolTip("Подобрать настройки для введённой игры, даже если она не установлена")
        add_btn.clicked.connect(self._use_typed_name)
        search_row.addWidget(add_btn)
        lay.addLayout(search_row)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        lay.addWidget(self.list, 1)

        self.scan_status = QLabel("Ищу игры…")
        self.scan_status.setObjectName("Faint")
        lay.addWidget(self.scan_status)

        rescan = ghost_button("Пересканировать лаунчеры")
        rescan.clicked.connect(self.refresh_games)
        lay.addWidget(rescan)
        return panel

    # ------------------------------------------------------------ правая колонка

    def _build_right(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        self.game_title = QLabel("Выбери игру слева")
        self.game_title.setObjectName("PageTitle")
        lay.addWidget(self.game_title)

        self.game_meta = QHBoxLayout()
        self.game_meta.setSpacing(6)
        lay.addLayout(self.game_meta)

        self.preset_row = QHBoxLayout()
        self.preset_row.setSpacing(10)
        lay.addLayout(self.preset_row)
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("Root")
        self.details = QVBoxLayout(holder)
        self.details.setContentsMargins(0, 0, 8, 0)
        self.details.setSpacing(12)
        area.setWidget(holder)
        lay.addWidget(area, 1)

        actions = QHBoxLayout()
        self.launch_btn = primary_button("▶  Запустить игру")
        self.launch_btn.clicked.connect(self._launch)
        self.launch_btn.setEnabled(False)
        actions.addWidget(self.launch_btn)

        copy_btn = ghost_button("Скопировать настройки")
        copy_btn.clicked.connect(self._copy)
        actions.addWidget(copy_btn)

        save_btn = ghost_button("Сохранить в файл")
        save_btn.clicked.connect(self._save)
        actions.addWidget(save_btn)
        actions.addStretch()
        lay.addLayout(actions)
        return panel

    # ------------------------------------------------------------ данные

    def refresh_games(self):
        self.scan_status.setText("Ищу игры в Steam, Epic и других лаунчерах…")
        self.worker = ScanWorker()
        self.worker.finished_scan.connect(self._on_scanned)
        self.worker.start()

    def _on_scanned(self, games: list[Game]):
        self.games = games
        self.list.clear()
        for game in games:
            item = QListWidgetItem(f"{game.name}\n{game.launcher} · {game.size_gb} ГБ")
            item.setData(Qt.UserRole, game.name)
            self.list.addItem(item)
        if games:
            self.scan_status.setText(f"Найдено игр: {len(games)}. "
                                     "Нет нужной? Впиши название вручную.")
            self.list.setCurrentRow(0)
        else:
            self.scan_status.setText("Установленных игр не нашлось — "
                                     "впиши название вручную в поле поиска.")

    def _filter_list(self, text: str):
        text = text.lower().strip()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _use_typed_name(self):
        name = self.search.text().strip()
        if name:
            self.show_game(name, installed=False)

    def _on_select(self, current: QListWidgetItem, _previous):
        if current:
            self.show_game(current.data(Qt.UserRole), installed=True)

    # ------------------------------------------------------------ отображение

    def show_game(self, name: str, installed: bool):
        self.current_name = name
        self.recs = build_all(self.ctx.machine, name)
        prof = profile_for(name)

        self.game_title.setText(name)
        clear_layout(self.game_meta)
        self.game_meta.addWidget(Badge(prof.category, theme.ACCENT_2))
        self.game_meta.addWidget(Badge(f"нагрузка на GPU {prof.weight:.1f}/10",
                                       theme.WARN if prof.weight >= 7 else theme.OK))
        if prof.dlss:
            self.game_meta.addWidget(Badge("DLSS", theme.OK))
        if prof.fsr:
            self.game_meta.addWidget(Badge("FSR", theme.OK))
        if prof.rt:
            self.game_meta.addWidget(Badge("трассировка лучей", theme.ACCENT))
        if not installed:
            self.game_meta.addWidget(Badge("не установлена", theme.TEXT_FAINT))
        self.game_meta.addStretch()

        self.launch_btn.setEnabled(installed and self._find_game(name) is not None)

        # кнопки профилей
        for btn in self.preset_group.buttons():
            self.preset_group.removeButton(btn)
        clear_layout(self.preset_row)

        target = self.ctx.machine.refresh
        for preset in PRESETS:
            rec = self.recs[preset]
            btn = PresetButton(rec, target)
            btn.clicked.connect(lambda _c=False, p=preset: self.select_preset(p))
            self.preset_group.addButton(btn)
            self.preset_row.addWidget(btn)
            if preset == self.current_preset:
                btn.setChecked(True)

        self.select_preset(self.current_preset)

    def select_preset(self, preset: str):
        self.current_preset = preset
        for btn, key in zip(self.preset_group.buttons(), PRESETS):
            btn.setChecked(key == preset)
        rec = self.recs.get(preset)
        if rec:
            self._render_details(rec)

    def _render_details(self, rec: Recommendation):
        clear_layout(self.details)

        # сводка
        summary = Card(f"{rec.icon}  {rec.title} — ожидаемые {rec.fps} FPS",
                       f"Ограничивающее звено: {rec.limiter}. "
                       f"Оценка для {self.ctx.machine.resolution} на твоём железе.")
        bar = QHBoxLayout()
        bar.setSpacing(6)
        target = self.ctx.machine.refresh
        if rec.fps >= target:
            bar.addWidget(Badge(f"монитор {target} Гц раскрывается полностью", theme.OK))
        elif rec.fps >= 60:
            bar.addWidget(Badge(f"из {target} Гц монитора используется ~{rec.fps}", theme.WARN))
        else:
            bar.addWidget(Badge("ниже комфортных 60 кадров", theme.DANGER))
        if rec.upscale != "Выкл":
            bar.addWidget(Badge(f"апскейлинг: {rec.upscale}", theme.ACCENT))
        if rec.rt:
            bar.addWidget(Badge("трассировка включена", theme.ACCENT_2))
        bar.addStretch()
        summary.add_layout(bar)
        self.details.addWidget(summary)

        # предупреждения
        if rec.warnings:
            warn = Card("Что важно знать")
            for text in rec.warnings:
                row = QLabel("•  " + text)
                row.setObjectName("Dim")
                row.setWordWrap(True)
                warn.add(row)
            self.details.addWidget(warn)

        # настройки
        table = Card("Настройки в игре",
                     "Слева параметр, справа значение. Под каждым — зачем это нужно.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        for row, (key, value, why) in enumerate(rec.settings):
            name = QLabel(key)
            name.setStyleSheet("font-weight: 600; font-size: 13px;")
            val = QLabel(value)
            important = value.lower() in ("выкл", "низкие", "низкая", "низкий")
            val.setStyleSheet(
                f"color: {theme.WARN if important else theme.ACCENT}; "
                "font-weight: 700; font-size: 13px;")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            hint = QLabel(why)
            hint.setObjectName("Faint")
            hint.setWordWrap(True)

            grid.addWidget(name, row * 3, 0)
            grid.addWidget(val, row * 3, 1)
            grid.addWidget(hint, row * 3 + 1, 0, 1, 2)

            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setFixedHeight(1)
            line.setStyleSheet(f"background: {theme.BORDER}; border: none;")
            grid.addWidget(line, row * 3 + 2, 0, 1, 2)
        table.add_layout(grid)
        self.details.addWidget(table)

        # автоприменение прямо в файл настроек игры, если умеем
        config_card = self._config_card(rec)
        if config_card is not None:
            self.details.addWidget(config_card)

        # персональное правило для этой игры
        self.details.addWidget(self._rule_card())

        # драйвер
        driver = Card("Панель управления NVIDIA" if self.ctx.machine.gpu.vendor == "nvidia"
                      else "Настройки драйвера видеокарты")
        for key, value in self._driver_tips(rec):
            row = QLabel(f"<b>{key}</b> — {value}")
            row.setObjectName("Dim")
            row.setWordWrap(True)
            driver.add(row)
        self.details.addWidget(driver)
        self.details.addStretch()

    # ------------------------------------------------------------ правило игры

    def _rule_card(self) -> Card:
        """Персональные настройки, которые автопилот применит именно к этой игре."""
        card = Card(f"⚙  Правило для «{self.current_name}»",
                    "Автопилот применит это, как только заметит запуск игры. "
                    "Если правила нет, работает общий профиль из настроек.")

        existing = gameprofiles.get_rule(self.current_name) or             gameprofiles.GameRule(game=self.current_name)
        game = self._find_game(self.current_name)
        if game is not None and not existing.process:
            existing.process = getattr(game, "exe", "") or ""

        from core import profiles as profiles_core
        self._rule_profiles = [("", "не менять")] + [
            (p.key, f"{p.icon}  {p.title}") for p in profiles_core.PROFILES
            if p.key in gameprofiles.SYSTEM_PROFILES]

        self.rule_profile = QComboBox()
        for _k, title in self._rule_profiles:
            self.rule_profile.addItem(title)
        keys = [k for k, _t in self._rule_profiles]
        if existing.system_profile in keys:
            self.rule_profile.setCurrentIndex(keys.index(existing.system_profile))
        card.add(self._rule_row("Профиль системы", self.rule_profile))

        self._rule_presets = [("", "не менять")] + [
            (key, PRESET_INFO[key]["title"]) for key in PRESETS]
        self.rule_preset = QComboBox()
        for _k, title in self._rule_presets:
            self.rule_preset.addItem(title)
        pkeys = [k for k, _t in self._rule_presets]
        if existing.graphics_preset in pkeys:
            self.rule_preset.setCurrentIndex(pkeys.index(existing.graphics_preset))
        card.add(self._rule_row("Настройки графики", self.rule_preset))

        self.rule_auto = QCheckBox("Записывать настройки прямо в игру автоматически")
        self.rule_auto.setChecked(bool(existing.auto_graphics))
        card.add(self.rule_auto)

        self.rule_priority = QComboBox()
        self._rule_prio_keys = list(gameprofiles.PRIORITIES)
        for key in self._rule_prio_keys:
            self.rule_priority.addItem(gameprofiles.PRIORITIES[key])
        if existing.priority in self._rule_prio_keys:
            self.rule_priority.setCurrentIndex(
                self._rule_prio_keys.index(existing.priority))
        card.add(self._rule_row("Приоритет процесса игры", self.rule_priority))

        self.rule_affinity = QComboBox()
        self._rule_aff_keys = list(gameprofiles.AFFINITY)
        for key in self._rule_aff_keys:
            self.rule_affinity.addItem(gameprofiles.AFFINITY[key])
        if existing.affinity in self._rule_aff_keys:
            self.rule_affinity.setCurrentIndex(
                self._rule_aff_keys.index(existing.affinity))
        card.add(self._rule_row("Ядра для игры", self.rule_affinity))

        row = QHBoxLayout()
        save_btn = primary_button("Сохранить правило")
        save_btn.clicked.connect(self._save_rule)
        row.addWidget(save_btn)
        if gameprofiles.get_rule(self.current_name):
            clear_btn = ghost_button("Убрать правило")
            clear_btn.clicked.connect(self._clear_rule)
            row.addWidget(clear_btn)
        row.addStretch()
        card.add_layout(row)

        self.rule_status = QLabel(
            f"Сейчас: {existing.describe()}" if not existing.is_empty
            else "Правила нет — работает общий профиль автопилота.")
        self.rule_status.setObjectName("Faint")
        self.rule_status.setWordWrap(True)
        card.add(self.rule_status)
        return card

    def _rule_row(self, label: str, widget) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setStyleSheet("font-size: 13px;")
        lay.addWidget(name, 1)
        widget.setMinimumWidth(230)
        lay.addWidget(widget)
        return box

    def _save_rule(self):
        game = self._find_game(self.current_name)
        rule = gameprofiles.GameRule(
            game=self.current_name,
            process=(getattr(game, "exe", "") or "") if game else "",
            system_profile=self._rule_profiles[self.rule_profile.currentIndex()][0],
            graphics_preset=self._rule_presets[self.rule_preset.currentIndex()][0],
            priority=self._rule_prio_keys[self.rule_priority.currentIndex()],
            affinity=self._rule_aff_keys[self.rule_affinity.currentIndex()],
            auto_graphics=self.rule_auto.isChecked())
        gameprofiles.set_rule(rule)
        self.rule_status.setText(
            f"Сохранено: {rule.describe()}" if not rule.is_empty
            else "Правило убрано — работает общий профиль автопилота.")

    def _clear_rule(self):
        gameprofiles.remove_rule(self.current_name)
        rec = self.recs.get(self.current_preset)
        if rec:
            self._render_details(rec)

    # ------------------------------------------------------------ запись в игру

    def _matching_config(self):
        """Ищет файл настроек, подходящий текущей игре по названию."""
        name = (self.current_name or "").lower()
        if not name:
            return None
        for config in self.configs:
            title = config.title.lower()
            if name in title or title in name:
                return config
            # общие сокращения
            if "grand theft" in name and "grand theft" in title:
                return config
        return None

    def _config_card(self, rec: Recommendation):
        """Карточка «применить настройки прямо в игру», если умеем писать в неё."""
        config = self._matching_config()
        if config is None:
            return None

        try:
            changes = gameconfig.plan(config, rec.preset, self.ctx.machine)
        except Exception as exc:
            log(f"Не удалось составить план настроек игры: {exc}")
            return None

        card = Card(f"🛠  Применить прямо в игру · {gameconfig.ENGINE_NAMES[config.engine]}",
                    "Приложение само пропишет эти настройки в файл игры. Перед записью "
                    "делается копия, откат — в один клик. Игра при этом должна быть закрыта.")

        running = gameconfig.is_running(config)
        if running:
            card.add(self._notice(
                f"Игра сейчас запущена ({running}). Закрой её — иначе при выходе "
                "она перезапишет файл своими значениями.", theme.WARN))

        if not changes:
            card.add(dim_label("В файле игры уже стоят эти значения — менять нечего."))
        else:
            preview = QLabel(f"Будет изменено параметров: {len(changes)}")
            preview.setObjectName("Dim")
            card.add(preview)
            for change in changes[:8]:
                row = QLabel(f"<b>{change.title}</b>: {change.old} → "
                             f"<span style='color:{theme.ACCENT}'>{change.new}</span>")
                row.setObjectName("Faint")
                row.setWordWrap(True)
                card.add(row)
            if len(changes) > 8:
                card.add(dim_label(f"…и ещё {len(changes) - 8}."))

        buttons = QHBoxLayout()
        apply_btn = primary_button(f"Записать «{rec.title.lower()}» в игру")
        apply_btn.setEnabled(bool(changes) and not running)
        apply_btn.clicked.connect(lambda _c=False, c=config, ch=changes: self._apply_config(c, ch))
        buttons.addWidget(apply_btn)

        if config.has_backup():
            restore_btn = ghost_button("Вернуть как было")
            restore_btn.clicked.connect(lambda _c=False, c=config: self._restore_config(c))
            buttons.addWidget(restore_btn)
        buttons.addStretch()
        card.add_layout(buttons)
        return card

    def _notice(self, text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {color}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {color}55; border-radius: 8px; padding: 8px;")
        return label

    def _apply_config(self, config, changes):
        ok, message = gameconfig.apply(config, changes)
        box = QMessageBox(self)
        box.setWindowTitle("Настройки игры" if ok else "Не удалось")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText(message)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if ok:
            self.configs = gameconfig.detect_configs()
            rec = self.recs.get(self.current_preset)
            if rec:
                self._render_details(rec)

    def _restore_config(self, config):
        ok, message = gameconfig.restore(config)
        box = QMessageBox(self)
        box.setWindowTitle("Откат настроек" if ok else "Не удалось")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText(message)
        box.setStyleSheet(theme.STYLESHEET)
        box.exec()
        if ok:
            rec = self.recs.get(self.current_preset)
            if rec:
                self._render_details(rec)

    def _driver_tips(self, rec: Recommendation) -> list[tuple[str, str]]:
        refresh = self.ctx.machine.refresh
        tips = [
            ("Режим управления электропитанием", "«Предпочтителен максимум производительности» "
             "для этой игры — карта не будет сбрасывать частоты между кадрами."),
            ("Режим низкой задержки",
             "«Вкл» для одиночных игр, «Ультра» для соревновательных."),
            ("Вертикальный синхроимпульс", "«Выкл» в панели, управление оставь игре."),
            ("Максимальная частота кадров",
             f"{max(refresh - 3, 30)} — держит кадр в окне G-Sync и снижает задержку."),
            ("Фильтрация текстур — качество", "«Высокая производительность» даёт 2-4% кадров "
             "и почти не видна в движении."),
        ]
        if rec.preset == "esports":
            tips.append(("Масштабирование", "Выполняется на видеокарте, без растягивания — "
                                            "меньше задержка при пониженном разрешении."))
        return tips

    # ------------------------------------------------------------ действия

    def _find_game(self, name: str) -> Game | None:
        for game in self.games:
            if game.name == name:
                return game
        return None

    def _launch(self):
        game = self._find_game(self.current_name)
        if not game:
            return
        target = game.launch_url
        log(f"Запуск игры: {target}")
        try:
            os.startfile(target)
        except OSError:
            run(["cmd", "/c", "start", "", target], timeout=10)

    def _as_text(self) -> str:
        rec = self.recs.get(self.current_preset)
        if not rec:
            return ""
        m = self.ctx.machine
        lines = [
            f"КРЕМЕНЬ — настройки для «{self.current_name}»",
            f"Профиль: {rec.title} ({rec.subtitle})",
            f"Железо: {m.cpu_name} / {m.gpu.name} / {m.ram_gb:.0f} ГБ / {m.resolution} @ {m.refresh} Гц",
            f"Ожидаемый FPS: ~{rec.fps} (упор в {rec.limiter})",
            "",
            "НАСТРОЙКИ В ИГРЕ",
        ]
        width = max(len(k) for k, _v, _w in rec.settings) + 2
        for key, value, _why in rec.settings:
            lines.append(f"  {key.ljust(width)} {value}")
        if rec.warnings:
            lines += ["", "ВАЖНО"]
            lines += [f"  - {w}" for w in rec.warnings]
        lines += ["", "ДРАЙВЕР ВИДЕОКАРТЫ"]
        for key, value in self._driver_tips(rec):
            lines.append(f"  - {key}: {value}")
        return "\n".join(lines)

    def _copy(self):
        text = self._as_text()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.scan_status.setText("Настройки скопированы в буфер обмена.")

    def _save(self):
        text = self._as_text()
        if not text:
            return
        safe = "".join(c for c in self.current_name if c.isalnum() or c in " -_").strip()
        default = str(Path.home() / "Desktop" / f"Настройки — {safe}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить настройки", default,
                                              "Текстовый файл (*.txt)")
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.scan_status.setText(f"Сохранено: {path}")
