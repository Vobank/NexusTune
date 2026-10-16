"""Глобальные горячие клавиши — работают, даже когда окно свёрнуто.

Обычные QShortcut срабатывают только когда окно приложения активно. Толку от
них в игре ноль: там активна игра. Поэтому здесь регистрируются настоящие
системные горячие клавиши через RegisterHotKey — Windows доставит нажатие
приложению, даже если оно в трее, а на экране полноэкранная игра.

Ограничение честное: если комбинацию уже занял кто-то другой (Steam, Discord,
GeForce Experience), Windows просто откажет в регистрации. Тогда приложение
скажет об этом, а не сделает вид, что всё работает.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

from core.util import log

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Виртуальные коды клавиш, которые нам нужны
VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "HOME": 0x24, "END": 0x23, "INSERT": 0x2D, "DELETE": 0x2E,
    "PGUP": 0x21, "PGDN": 0x22,
}
for _c in "0123456789":
    VK[_c] = ord(_c)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_c] = ord(_c)

_MOD_NAMES = {"CTRL": MOD_CONTROL, "ALT": MOD_ALT, "SHIFT": MOD_SHIFT,
              "WIN": MOD_WIN}


def parse_combo(text: str) -> tuple[int, int] | None:
    """«Ctrl+Alt+F9» -> (модификаторы, код клавиши). None, если не разобрать."""
    if not text:
        return None
    parts = [p.strip().upper() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for part in parts:
        if part in _MOD_NAMES:
            mods |= _MOD_NAMES[part]
        elif part in VK:
            key = VK[part]
        else:
            return None
    if key is None:
        return None
    return mods | MOD_NOREPEAT, key


@dataclass
class Hotkey:
    """Одна зарегистрированная комбинация."""
    key: str                 # внутреннее имя действия
    combo: str               # как её записал пользователь
    title: str               # что она делает — для интерфейса
    callback: Callable[[], None]
    registered: bool = False
    error: str = ""


class HotkeyManager(QAbstractNativeEventFilter):
    """Регистрирует системные горячие клавиши и раздаёт нажатия."""

    def __init__(self):
        super().__init__()
        self._user32 = ctypes.windll.user32
        self._hotkeys: dict[int, Hotkey] = {}
        self._next_id = 1
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        app = QCoreApplication.instance()
        if app is None:
            return
        app.installNativeEventFilter(self)
        self._installed = True

    def register(self, key: str, combo: str, title: str,
                 callback: Callable[[], None]) -> Hotkey:
        """Регистрирует комбинацию. Результат честно говорит, получилось ли."""
        hotkey = Hotkey(key=key, combo=combo, title=title, callback=callback)
        parsed = parse_combo(combo)
        if parsed is None:
            hotkey.error = "Не понимаю такую комбинацию."
            return hotkey

        mods, vk = parsed
        hotkey_id = self._next_id
        self._next_id += 1
        # RegisterHotKey(hWnd=None) отправляет сообщение в очередь потока
        if self._user32.RegisterHotKey(None, hotkey_id, mods, vk):
            hotkey.registered = True
            self._hotkeys[hotkey_id] = hotkey
            log(f"Горячая клавиша {combo} -> {title}")
        else:
            hotkey.error = ("Комбинацию уже занял кто-то другой — например Steam, "
                            "Discord или панель NVIDIA. Выбери другую.")
            log(f"Горячая клавиша {combo} занята другим приложением")
        return hotkey

    def unregister_all(self) -> None:
        for hotkey_id in list(self._hotkeys):
            try:
                self._user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
        self._hotkeys.clear()

    # ------------------------------------------------------------ приём событий

    def nativeEventFilter(self, event_type, message):
        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        except (ValueError, TypeError):
            return False, 0
        if msg.message != WM_HOTKEY:
            return False, 0

        hotkey = self._hotkeys.get(int(msg.wParam))
        if hotkey is not None:
            try:
                hotkey.callback()
            except Exception as exc:
                log(f"Горячая клавиша {hotkey.combo}: ошибка — {exc}")
            return True, 0
        return False, 0


# Действия по умолчанию: комбинации выбраны так, чтобы не конфликтовать
# с распространёнными игровыми и оверлейными сочетаниями.
DEFAULT_HOTKEYS = {
    "overlay": ("Ctrl+Alt+O", "Показать или скрыть оверлей"),
    "profile_gaming": ("Ctrl+Alt+G", "Включить профиль «Игры»"),
    "profile_stock": ("Ctrl+Alt+S", "Вернуть «Как с завода»"),
    "clean_memory": ("Ctrl+Alt+M", "Освободить оперативную память"),
    "show_window": ("Ctrl+Alt+K", "Показать окно КРЕМНЯ"),
}
