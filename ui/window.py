"""Главное окно: боковое меню, страницы и общий таймер датчиков."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QPixmap, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QStackedWidget, QButtonGroup, QFrame, QSystemTrayIcon, QMenu, QScrollArea,
    QApplication,
)

from core.autopilot import GameWatcher
from core.context import AppContext
from core.games import scan_all
from core.util import is_admin, log, relaunch_as_admin

from . import theme
from .brand import APP_TITLE, TAGLINE, app_icon, mark_pixmap
from .command import Command, CommandPalette
from .hotkeys import DEFAULT_HOTKEYS, HotkeyManager
from .overlay import Overlay
from .pages.cleanup import CleanupPage
from .pages.dashboard import DashboardPage
from .pages.games import GamesPage
from .pages.health import HealthPage
from .pages.memory import MemoryPage
from .pages.overclock import OverclockPage
from .pages.power import PowerPage
from .pages.profiles import ProfilesPage
from .pages.network import NetworkPage
from .pages.processes import ProcessesPage
from .pages.rgb import RgbPage
from .pages.storage import StoragePage
from .pages.system import SystemPage
from .pages.programs import ProgramsPage
from .pages.privacy import PrivacyPage
from .pages.forge import ForgePage
from .pages.expert import ExpertPage
from .pages.settings import SettingsPage
from .pages.deepdive import DeepDivePage
from .pages.chronicle import ChroniclePage
from .pages.services import ServicesPage
from .pages.explorer import ExplorerPage
from .pages.reactor import ReactorPage

PAGES = [
    ("◈", "Обзор", "dashboard"),
    ("⚡", "Реактор", "reactor"),
    ("🔨", "Кузница", "forge"),
    ("⚡", "Профили", "profiles"),
    ("🎮", "Игры", "games"),
    ("🩺", "Диагностика", "health"),
    ("🔬", "Разбор", "deepdive"),
    ("📜", "Хроника", "chronicle"),
    ("🧹", "Уборка", "cleanup"),
    ("🗑", "Программы", "programs"),
    ("🛡", "Приватность", "privacy"),
    ("⚙", "Оптимизация", "system"),
    ("☢", "Эксперт", "expert"),
    ("🔩", "Службы", "services"),
    ("🗂", "Проводник", "explorer"),
    ("🧠", "Процессы", "processes"),
    ("🔌", "Энергия", "power"),
    ("🌐", "Сеть", "network"),
    ("🔥", "Разгон", "overclock"),
    ("▤", "Память", "memory"),
    ("💽", "Накопители", "storage"),
    ("◐", "Подсветка", "rgb"),
    ("🎛", "Настройки", "settings"),
]


class SensorWorker(QThread):
    """Опрашивает датчики в фоне: nvidia-smi слишком медленный для потока интерфейса."""
    reading = Signal(dict)

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self._running = True
        self.interval = max(400, int(ctx.config.get("sensor_interval_ms", 1200)))

    def run(self):
        while self._running:
            try:
                self.reading.emit(self.ctx.sensors.read())
            except Exception:
                pass
            self.msleep(self.interval)

    def stop(self):
        self._running = False
        self.wait(3000)


class ChronicleWorker(QThread):
    """Снимает слепок системы при запуске, но не чаще раза в сутки."""
    done = Signal(bool)

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

    def run(self):
        try:
            from core import chronicle
            self.done.emit(chronicle.capture_if_due(self.ctx) is not None)
        except Exception:
            self.done.emit(False)


class HardwareRefreshWorker(QThread):
    """Перечитывает состав железа в фоне и обновляет кэш.

    На старте состав берётся из кэша — это мгновенно. Но если человек
    поменял видеокарту или добавил планку памяти, узнать об этом нужно.
    Поэтому сразу после открытия окна опрос идёт заново, уже не мешая работе.
    """
    done = Signal(object)

    def run(self):
        try:
            from core.hwinfo import detect_machine
            self.done.emit(detect_machine(use_cache=False))
        except Exception:
            self.done.emit(None)


class LibraryWorker(QThread):
    """Разово сканирует игры, чтобы автопилот знал их папки."""
    done = Signal(list)

    def run(self):
        try:
            self.done.emit(scan_all())
        except Exception:
            self.done.emit([])




class MainWindow(QMainWindow):
    """Окно приложения."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("КРЕМЕНЬ — центр настройки ПК")
        self.setWindowIcon(app_icon())
        self.resize(1340, 880)
        self.setMinimumSize(1120, 700)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        # Страницы строятся по требованию. Раньше все двадцать две собирались
        # при запуске: «Память» и «Приватность» лезли в WMI прямо в конструкторе
        # и вместе съедали двадцать секунд на пустом месте. Теперь строится
        # только та, которую открыли.
        self._factories = {
            "dashboard": DashboardPage, "reactor": ReactorPage,
            "forge": ForgePage,
            "profiles": ProfilesPage, "games": GamesPage,
            "health": HealthPage, "deepdive": DeepDivePage,
            "chronicle": ChroniclePage, "cleanup": CleanupPage,
            "programs": ProgramsPage, "privacy": PrivacyPage,
            "system": SystemPage, "expert": ExpertPage,
            "services": ServicesPage, "explorer": ExplorerPage,
            "processes": ProcessesPage, "power": PowerPage,
            "network": NetworkPage, "overclock": OverclockPage,
            "memory": MemoryPage, "storage": StoragePage,
            "rgb": RgbPage, "settings": SettingsPage,
        }
        self.pages: dict[str, QWidget] = {}
        self._page_index = {key: index
                            for index, (_i, _t, key) in enumerate(PAGES)}
        for _icon, title, _key in PAGES:
            self.stack.addWidget(self._placeholder(title))

        from core.telemetry import Recorder
        self.recorder = Recorder()

        self.overlay = Overlay(ping_host=str(ctx.config.get("overlay_ping_host",
                                                            "1.1.1.1")))
        self.overlay.setWindowOpacity(
            max(0.4, min(1.0, int(ctx.config.get("overlay_opacity", 95)) / 100)))
        self._setup_autopilot()
        self._setup_tray()

        self.sensor_worker = SensorWorker(ctx)
        self.sensor_worker.reading.connect(self._on_reading)
        self.sensor_worker.start()

        self.library_worker = LibraryWorker()
        self.library_worker.done.connect(self._on_library)
        self.library_worker.start()

        self.hardware_worker = HardwareRefreshWorker()
        self.hardware_worker.done.connect(self._on_hardware)
        self.hardware_worker.start()

        self.chronicle_worker = ChronicleWorker(ctx)
        self.chronicle_worker.done.connect(self._on_chronicle)
        self.chronicle_worker.start()

        self._open_start_page()
        self._setup_shortcuts()
        self._setup_global_hotkeys()

    # ------------------------------------------------------------ ленивые страницы

    def _placeholder(self, title: str) -> QWidget:
        """Заглушка вместо ещё не построенной страницы."""
        holder = QWidget()
        holder.setObjectName("Root")
        lay = QVBoxLayout(holder)
        lay.addStretch()
        label = QLabel(f"Открываю «{title}»…")
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("Dim")
        lay.addWidget(label)
        lay.addStretch()
        return holder

    def ensure_page(self, key: str):
        """Строит страницу, если её ещё нет, и подключает её сигналы."""
        page = self.pages.get(key)
        if page is not None:
            return page
        factory = self._factories.get(key)
        if factory is None:
            return None

        index = self._page_index[key]
        try:
            page = factory(self.ctx)
        except Exception as exc:
            log(f"Страница «{key}» не построилась: {exc}")
            return None

        placeholder = self.stack.widget(index)
        self.stack.insertWidget(index, page)
        if placeholder is not None:
            self.stack.removeWidget(placeholder)
            placeholder.deleteLater()
        self.pages[key] = page
        self._connect_page(key, page)
        return page

    def _connect_page(self, key: str, page):
        """Сигналы, которыми страницы сообщают друг другу об изменениях."""
        if key == "profiles":
            page.profile_applied.connect(self._refresh_after_change)
        elif key == "health":
            page.fixed.connect(self._refresh_after_change)
        elif key == "forge":
            page.forged.connect(self._refresh_after_change)
        elif key == "settings":
            page.palette_changed.connect(self._apply_palette)

    # ------------------------------------------------------------ горячие клавиши

    def _setup_shortcuts(self):
        """Ctrl+K — палитра команд, Ctrl+1..9 — быстрый переход, F11 — оверлей."""
        QShortcut(QKeySequence("Ctrl+K"), self, self.open_command_palette)
        QShortcut(QKeySequence("Ctrl+P"), self, self.open_command_palette)
        QShortcut(QKeySequence("F11"), self,
                  lambda: self.overlay_btn.click())
        QShortcut(QKeySequence("Ctrl+F"), self,
                  lambda: self._goto("forge"))
        for number in range(1, 10):
            if number - 1 < len(PAGES):
                QShortcut(QKeySequence(f"Ctrl+{number}"), self,
                          lambda i=number - 1: self._goto_index(i))

    def _setup_global_hotkeys(self):
        """Системные комбинации: работают, даже когда окно свёрнуто и идёт игра."""
        self.hotkeys = HotkeyManager()
        self.hotkeys.install()
        actions = {
            "overlay": lambda: self.overlay_btn.click(),
            "profile_gaming": lambda: self._tray_profile("gaming"),
            "profile_stock": lambda: self._tray_profile("stock"),
            "clean_memory": self._hotkey_clean_memory,
            "show_window": self._show_from_tray,
        }
        saved = self.ctx.config.get("hotkeys") or {}
        self.hotkey_state = []
        for key, (default_combo, title) in DEFAULT_HOTKEYS.items():
            combo = saved.get(key, default_combo)
            if not combo:                     # пустая строка — пользователь отключил
                continue
            self.hotkey_state.append(
                self.hotkeys.register(key, combo, title, actions[key]))

        failed = [h for h in self.hotkey_state if not h.registered]
        if failed:
            log(f"Не удалось занять горячих клавиш: {len(failed)}")

    def _hotkey_clean_memory(self):
        """Освобождает память и коротко сообщает об этом всплывающей подсказкой."""
        from core.memory import clean_memory
        try:
            result = clean_memory()
            freed = result.get("freed_mb", 0)
            self.tray.showMessage(
                "Память освобождена",
                f"Вернулось {freed} МБ оперативной памяти."
                if freed else "Освобождать было нечего — память и так свободна.",
                QSystemTrayIcon.Information, 3000)
        except Exception as exc:
            log(f"Горячая клавиша очистки памяти: {exc}")

    def _goto_index(self, index: int):
        if not 0 <= index < len(PAGES):
            return
        key = PAGES[index][2]
        # показываем заглушку сразу, чтобы окно не выглядело зависшим
        self.stack.setCurrentIndex(index)
        button = self.nav_group.button(index)
        if button:
            button.setChecked(True)
        if key not in self.pages:
            QApplication.processEvents()
            self.ensure_page(key)
            self.stack.setCurrentIndex(index)

    def _goto(self, key: str):
        keys = [k for _i, _t, k in PAGES]
        if key in keys:
            self._goto_index(keys.index(key))

    def _build_commands(self) -> list:
        """Всё, что можно сделать из палитры команд."""
        from core import profiles as profiles_core

        commands: list[Command] = []
        for index, (icon, title, key) in enumerate(PAGES):
            commands.append(Command(
                title=title, hint=f"Перейти на страницу «{title}»",
                group="Страница", keywords=f"{icon} {key} страница переход",
                run=lambda i=index: self._goto_index(i)))

        for profile in profiles_core.PROFILES:
            commands.append(Command(
                title=f"Включить профиль «{profile.title}»",
                hint=profile.subtitle, group="Профиль",
                keywords=f"{profile.key} профиль применить",
                run=lambda k=profile.key: self._tray_profile(k)))

        commands += [
            Command("Показать или скрыть оверлей", "Датчики поверх игры · F11",
                    lambda: self.overlay_btn.click(), "Действие", "overlay fps"),
            Command("Ковать в Кузнице", "Замер до, перестройка, замер после · Ctrl+F",
                    lambda: self._goto("forge"), "Действие", "forge кузница замер"),
            Command("Перезапустить с правами администратора",
                    "Нужно для системных твиков", self._elevate, "Действие",
                    "админ права elevate"),
            Command("Открыть папку с данными",
                    "Журнал, снимки, история", self._open_data_folder,
                    "Действие", "логи данные папка"),
            Command("Свернуть в трей", "Приложение продолжит работать у часов",
                    self.hide, "Действие", "трей свернуть"),
            Command("Выйти из приложения", "Полностью закрыть КРЕМЕНЬ",
                    self._quit_from_tray, "Действие", "выход закрыть quit"),
        ]
        for key, data in theme.PALETTES.items():
            commands.append(Command(
                title=f"Палитра «{data['title']}»", hint=data["hint"],
                group="Оформление", keywords=f"{key} тема цвет палитра",
                run=lambda k=key: self._switch_palette(k)))
        return commands

    def _switch_palette(self, key: str):
        # страница настроек могла быть ещё не открыта — тогда меняем палитру
        # напрямую и сохраняем выбор в конфиг
        page = self.pages.get("settings")
        if page is not None:
            page._pick_palette(key)
            return
        self.ctx.config["palette"] = key
        self.ctx.save()
        self._apply_palette(key)

    def _open_data_folder(self):
        from core import settings as settings_core
        settings_core.open_data_folder()

    def open_command_palette(self):
        palette = CommandPalette(self._build_commands(), self)
        palette.show_centered(self)

    def _open_start_page(self):
        """Открывает страницу, выбранную в настройках."""
        wanted = self.ctx.config.get("start_page", "dashboard")
        keys = [key for _icon, _title, key in PAGES]
        self._goto_index(keys.index(wanted) if wanted in keys else 0)

    # ------------------------------------------------------------ боковое меню

    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(228)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(14, 20, 14, 14)
        lay.setSpacing(3)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(mark_pixmap(34))
        mark.setFixedSize(34, 34)
        brand.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(0)
        logo = QLabel(APP_TITLE)
        logo.setObjectName("Logo")
        names.addWidget(logo)
        sub = QLabel(TAGLINE)
        sub.setObjectName("LogoSub")
        names.addWidget(sub)
        brand.addLayout(names)
        brand.addStretch()
        lay.addLayout(brand)
        lay.addSpacing(16)

        # Пунктов меню много — прячем их в прокручиваемую область, чтобы нижние
        # кнопки (оверлей, статус) всегда оставались на виду.
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_host = QWidget()
        nav_host.setObjectName("Sidebar")
        nav_lay = QVBoxLayout(nav_host)
        nav_lay.setContentsMargins(0, 0, 6, 0)
        nav_lay.setSpacing(2)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (icon, title, _key) in enumerate(PAGES):
            btn = QPushButton(f"  {icon}   {title}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, i=index: self._goto_index(i))
            self.nav_group.addButton(btn, index)
            nav_lay.addWidget(btn)
            if index == 0:
                btn.setChecked(True)
        nav_lay.addStretch()
        nav_scroll.setWidget(nav_host)
        lay.addWidget(nav_scroll, 1)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {theme.BORDER};")
        lay.addWidget(line)

        self.game_badge = QLabel("")
        self.game_badge.setObjectName("Faint")
        self.game_badge.setWordWrap(True)
        lay.addWidget(self.game_badge)

        self.overlay_btn = QPushButton("Оверлей поверх игры")
        self.overlay_btn.setObjectName("Ghost")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setCursor(Qt.PointingHandCursor)
        self.overlay_btn.clicked.connect(self._toggle_overlay)
        lay.addWidget(self.overlay_btn)

        if not is_admin():
            admin_btn = QPushButton("Перезапустить как админ")
            admin_btn.setObjectName("Ghost")
            admin_btn.setCursor(Qt.PointingHandCursor)
            admin_btn.clicked.connect(self._elevate)
            lay.addWidget(admin_btn)

        self.status = QLabel("Датчики опрашиваются…")
        self.status.setObjectName("Faint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        return bar

    # ------------------------------------------------------------ значок в трее

    def _setup_tray(self):
        """Значок у часов: профили и оверлей доступны, не открывая окно."""
        from core import profiles as profiles_core

        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("КРЕМЕНЬ")

        menu = QMenu()
        menu.setStyleSheet(theme.STYLESHEET)

        show_action = QAction("Открыть КРЕМЕНЬ", self)
        show_action.triggered.connect(self._show_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()

        profiles_menu = menu.addMenu("Включить профиль")
        for profile in profiles_core.PROFILES:
            action = QAction(f"{profile.icon}  {profile.title}", self)
            action.triggered.connect(
                lambda _checked=False, key=profile.key: self._tray_profile(key))
            profiles_menu.addAction(action)

        self.tray_overlay_action = QAction("Показать оверлей", self)
        self.tray_overlay_action.setCheckable(True)
        self.tray_overlay_action.triggered.connect(self._toggle_overlay_action)
        menu.addAction(self.tray_overlay_action)
        menu.addSeparator()

        quit_action = QAction("Выйти", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(quit_action)

        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_from_tray()

    def _tray_profile(self, key: str):
        page = self.ensure_page("profiles")
        if page:
            self.stack.setCurrentIndex(1)
            self.nav_group.button(1).setChecked(True)
            self._show_from_tray()
            page.on_profile_clicked(key)

    def _toggle_overlay_action(self, checked: bool):
        self.overlay_btn.setChecked(checked)
        self._toggle_overlay(checked)

    def _quit_from_tray(self):
        self._really_quit = True
        self.close()

    # ------------------------------------------------------------ автопилот

    def _setup_autopilot(self):
        watcher = GameWatcher(
            self.ctx,
            on_game_start=self._on_game_start,
            on_game_stop=self._on_game_stop,
        )
        watcher.game_profile = self.ctx.config.get("autopilot_profile", "gaming")
        watcher.restore_profile = self.ctx.config.get("autopilot_restore", "stock")
        self.ctx.watcher = watcher
        if self.ctx.config.get("autopilot"):
            watcher.start()

    def _on_library(self, games: list):
        self.ctx.games = games
        if self.ctx.watcher:
            self.ctx.watcher.set_library(games)

    def _on_hardware(self, machine):
        """Сверяет свежий опрос железа с тем, что взяли из кэша."""
        if machine is None:
            return
        old = self.ctx.machine
        changed = []
        for field, title in (("cpu_name", "процессор"), ("ram_gb", "объём памяти"),
                             ("ram_sticks", "число планок"), ("width", "разрешение")):
            if getattr(old, field, None) != getattr(machine, field, None):
                changed.append(title)
        if not changed:
            return

        self.ctx.machine = machine
        log("Состав железа изменился с прошлого запуска: " + ", ".join(changed))
        self.tray.showMessage(
            "Железо изменилось",
            "Изменилось: " + ", ".join(changed) +
            ". Перезапусти приложение, чтобы расчёты пересобрались полностью.",
            QSystemTrayIcon.Information, 6000)

    def _on_chronicle(self, captured: bool):
        if not captured:
            return
        page = self.pages.get("chronicle")
        if page:
            try:
                page.reload()
            except Exception as exc:
                log(f"Хроника: не удалось обновить страницу — {exc}")

    def _on_game_start(self, title: str):
        log(f"Автопилот: началась игра {title}")

    def _on_game_stop(self, session):
        log(f"Автопилот: закончилась игра {session.game} ({session.minutes} мин)")

    # ------------------------------------------------------------ события

    def _on_reading(self, reading: dict):
        self.ctx.last_reading = reading
        # считаем мощность один раз здесь, чтобы страницы видели один и тот же замер
        sample = self.ctx.power_meter.update(reading)
        reading["wall_w"] = sample.wall_w
        # долговременная история: раз в минуту сжатая запись уходит на диск
        try:
            self.recorder.feed(reading)
        except Exception as exc:
            log(f"История датчиков: {exc}")

        for page in list(self.pages.values()):
            tick = getattr(page, "tick", None)
            if callable(tick):
                try:
                    tick(reading)
                except Exception as exc:
                    log(f"Страница не смогла обновиться: {exc}")
        if self.overlay.isVisible():
            self.overlay.tick(reading)

        cpu = reading.get("cpu_load") or 0
        gpu = reading.get("gpu_load")
        temp = reading.get("gpu_temp")
        parts = [f"ЦП {cpu:.0f}%"]
        if gpu is not None:
            parts.append(f"GPU {gpu:.0f}%")
        if temp is not None:
            parts.append(f"{temp}°C")
        parts.append(f"{sample.wall_w:.0f} Вт")
        self.status.setText(" · ".join(parts))

        watcher = self.ctx.watcher
        if watcher and watcher.current:
            self.game_badge.setText(f"▶  Идёт игра: {watcher.current.game}")
        else:
            self.game_badge.setText("")

    def _refresh_after_change(self):
        """После профиля или починки обновляем галочки на странице оптимизации."""
        system_page = self.pages.get("system")   # если не открыта — обновлять нечего
        if system_page:
            try:
                system_page.refresh_states()
            except Exception as exc:
                log(f"Не удалось обновить страницу оптимизации: {exc}")

    def _toggle_overlay(self, checked: bool):
        if checked:
            try:
                self.overlay.set_target(float(self.ctx.machine.refresh or 60))
            except Exception:
                pass
            # без запущенного RivaTuner счётчика кадров не будет — запускаем сами
            try:
                from core.fps import launch_rtss, rtss_installed, rtss_running
                if rtss_installed() and not rtss_running():
                    ok, msg = launch_rtss()
                    if ok:
                        self.tray.showMessage(
                            "Запускаю RivaTuner",
                            "Без него нет счётчика кадров. Дай пару секунд.",
                            QSystemTrayIcon.Information, 4000)
            except Exception as exc:
                log(f"Не удалось запустить RivaTuner: {exc}")
            self.overlay.show()
            self._place_overlay()
        else:
            self.overlay.hide()

    def _place_overlay(self):
        """Ставит оверлей в угол, выбранный в настройках."""
        screen = self.screen().availableGeometry()
        margin = 40
        size = self.overlay.sizeHint()
        width = max(self.overlay.width(), size.width())
        height = max(self.overlay.height(), size.height())
        corner = self.ctx.config.get("overlay_corner", "top-right")
        x = screen.left() + margin if "left" in corner else             screen.right() - width - margin
        y = screen.top() + margin if "top" in corner else             screen.bottom() - height - margin
        self.overlay.move(x, y)

    def _apply_palette(self, name: str):
        """Меняет палитру на лету: пересобираем таблицу стилей всего приложения."""
        css = theme.apply_palette(name)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(css)
        if hasattr(self, "tray_menu"):
            self.tray_menu.setStyleSheet(css)
        log(f"Палитра переключена: {name}")

    def _elevate(self):
        if relaunch_as_admin():
            self.close()

    def closeEvent(self, event):
        # Первое закрытие прячет окно в трей: автопилот и умный баланс должны
        # продолжать работать, пока идёт игра. Выход — через меню значка.
        if not getattr(self, "_really_quit", False) and self.ctx.config.get(
                "minimize_to_tray", True) and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "КРЕМЕНЬ продолжает работать",
                "Приложение свернулось к часам. Профили доступны по правому щелчку "
                "на значке, выход — там же.",
                QSystemTrayIcon.Information, 4000)
            return

        self.sensor_worker.stop()
        balancer = getattr(self.ctx, "balancer", None)
        if balancer is not None:
            try:
                balancer.stop()
            except Exception:
                pass
        for page in self.pages.values():
            for attr in ("reactor", "blackbox"):
                obj = getattr(page, attr, None)
                if obj is not None:
                    try:
                        obj.stop()
                    except Exception:
                        pass
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            try:
                recorder.flush()          # не теряем последнюю неполную минуту
            except Exception:
                pass
        hotkeys = getattr(self, "hotkeys", None)
        if hotkeys is not None:
            hotkeys.unregister_all()
        self.overlay.close()
        self.tray.hide()
        self.ctx.shutdown()
        super().closeEvent(event)
