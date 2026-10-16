"""Проводник Windows: контекстное меню и поведение окон.

Две вещи, до которых Windows не даёт добраться нормально.

Первая — контекстное меню. Каждая установленная программа считает своим долгом
влезть в меню правой кнопки. Через полгода оно открывается с задержкой, потому
что Windows опрашивает десяток обработчиков, часть из которых принадлежит давно
удалённым программам. Отключаются они только правкой реестра.

Вторая — само поведение проводника. Скрытые расширения файлов (на них до сих
пор ловят вирусы вида «фото.jpg.exe»), урезанное меню Windows 11 с вечным
«Показать дополнительные параметры», история недавних файлов на виду.

Всё здесь обратимо. Отключение обработчика — это дефис перед его идентификатором,
ровно так же делает известная утилита ShellExView; чтобы вернуть, дефис убирается.
Ничего не удаляется безвозвратно.
"""
from __future__ import annotations

import winreg
from dataclasses import dataclass, field

from .optimizer import HKCU, HKLM, Tweak, _reg_tweak, reg_delete, reg_read, reg_write
from .util import log, run

HKCR = winreg.HKEY_CLASSES_ROOT

# Где Windows держит обработчики контекстного меню
_HANDLER_ROOTS = [
    (r"*\shellex\ContextMenuHandlers", "для файлов"),
    (r"Directory\shellex\ContextMenuHandlers", "для папок"),
    (r"Directory\Background\shellex\ContextMenuHandlers", "для пустого места в папке"),
    (r"Drive\shellex\ContextMenuHandlers", "для дисков"),
    (r"AllFilesystemObjects\shellex\ContextMenuHandlers", "для всего сразу"),
]

# Обработчики, без которых ломается сама работа с файлами. Список намеренно
# короткий: всё остальное — это просто пункты меню, их отключение ничего не
# ломает, а решать должен пользователь. Пункт Защитника, например, отключает
# только строку в меню, но не антивирус, поэтому здесь его нет.
_KEEP = {
    "open with", "openwith", "new", "sendto",
    "{a2a9545d-a0c2-42b4-9708-a0b2badd77c8}",   # «Отправить»
}

# Что это такое человеческим языком
_KNOWN_HANDLERS = {
    "EPP": ("Проверка Защитником Windows",
            "Пункт «Проверить с помощью Microsoft Defender». "
            "Отключение не выключает сам антивирус — только пункт меню."),
    "EncryptionMenu": ("Шифрование EFS",
                       "Пункты шифрования файлов. Нужен, только если ты "
                       "пользуешься шифрованием EFS."),
    "Open With EncryptionMenu": ("Шифрование EFS",
                                 "То же самое, вариант для файлов."),
    "Offline Files": ("Автономные файлы",
                      "Функция для корпоративных сетей. Дома не нужна."),
    "WorkFolders": ("Рабочие папки",
                    "Синхронизация с корпоративным сервером. Дома не нужна."),
    "Sharing": ("Общий доступ",
                "Пункты расшаривания папок по сети."),
    "NvAppDesktopContext": ("Панель NVIDIA",
                            "Пункт «Панель управления NVIDIA» на рабочем столе."),
    "NvCplDesktopContext": ("Панель NVIDIA (старая)",
                            "Старый пункт панели NVIDIA."),
    "New": ("Создать…", "Подменю создания новых файлов. Нужно почти всем."),
    "AnyCode": ("Открыть в Visual Studio", "Пункт от Visual Studio."),
    "igfxcui": ("Панель Intel", "Пункт панели управления графикой Intel."),
    "GitExtensions": ("Git Extensions", "Пункты системы контроля версий."),
    "TortoiseGit": ("TortoiseGit", "Пункты системы контроля версий."),
    "7-Zip": ("7-Zip", "Архиватор. Нужен, если пользуешься им."),
    "WinRAR": ("WinRAR", "Архиватор. Нужен, если пользуешься им."),
}


@dataclass
class Handler:
    """Один обработчик контекстного меню."""
    name: str
    clsid: str
    scope: str                  # где появляется
    path: str                   # путь в реестре
    enabled: bool = True
    title: str = ""
    what: str = ""
    protected: bool = False

    @property
    def display(self) -> str:
        return self.title or self.name


def _enum_keys(root, path: str) -> list[str]:
    try:
        handle = winreg.OpenKey(root, path)
    except OSError:
        return []
    names = []
    index = 0
    try:
        while True:
            names.append(winreg.EnumKey(handle, index))
            index += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(handle)
    return names


def read_handlers() -> list[Handler]:
    """Все обработчики контекстного меню, что нашлись в реестре."""
    found: list[Handler] = []
    seen: set[tuple] = set()

    for root_path, scope in _HANDLER_ROOTS:
        for name in _enum_keys(HKCR, root_path):
            full = f"{root_path}\\{name}"
            try:
                with winreg.OpenKey(HKCR, full) as key:
                    value = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            value = str(value).strip()
            if not value:
                continue

            # дефис в начале — наш способ отключения, ровно как в ShellExView
            enabled = not value.startswith("-")
            clsid = value.lstrip("-")
            key_id = (name.lower(), clsid.lower())
            if key_id in seen:
                continue
            seen.add(key_id)

            title, what = _KNOWN_HANDLERS.get(name, ("", ""))
            protected = name.lower() in _KEEP or clsid.lower() in _KEEP
            found.append(Handler(
                name=name, clsid=clsid, scope=scope, path=full,
                enabled=enabled, title=title, what=what, protected=protected))

    found.sort(key=lambda h: (not h.enabled, h.display.lower()))
    return found


def set_handler(handler: Handler, enabled: bool) -> tuple[bool, str]:
    """Включает или отключает обработчик, не удаляя его."""
    if handler.protected and not enabled:
        return False, ("Этот обработчик нужен для нормальной работы с файлами — "
                       "приложение его не трогает.")
    value = handler.clsid if enabled else "-" + handler.clsid
    try:
        with winreg.OpenKey(HKCR, handler.path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)
    except PermissionError:
        return False, "Нужны права администратора."
    except OSError as exc:
        return False, f"Не удалось изменить: {exc}"

    handler.enabled = enabled
    log(f"Контекстное меню: {handler.display} -> "
        f"{'включён' if enabled else 'отключён'}")
    return True, ("Обработчик включён." if enabled else
                  "Обработчик отключён. Он не удалён — вернуть можно этой же галочкой.")


def restart_explorer() -> tuple[bool, str]:
    """Перезапускает проводник, чтобы изменения меню вступили в силу."""
    run(["taskkill", "/f", "/im", "explorer.exe"], timeout=20)
    code, _out, err = run(["cmd", "/c", "start", "", "explorer.exe"], timeout=20)
    if code == 0:
        log("Проводник перезапущен")
        return True, "Проводник перезапущен, меню обновилось."
    return False, err or "Проводник не удалось перезапустить — перезайди в систему."


# ---------------------------------------------------------------- поведение проводника

_ADVANCED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_CLSID_W11_MENU = (r"Software\Classes\CLSID"
                   r"\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32")


def _classic_menu_state() -> bool:
    """Включено ли классическое меню Windows 11."""
    try:
        with winreg.OpenKey(HKCU, _CLSID_W11_MENU) as key:
            return winreg.QueryValueEx(key, "")[0] == ""
    except OSError:
        return False


def _set_classic_menu(enable: bool) -> bool:
    """Возвращает полное меню правой кнопки вместо урезанного.

    Windows 11 прячет половину пунктов за «Показать дополнительные параметры».
    Пустое значение по этому пути отключает новый обработчик, и система
    возвращается к обычному меню. Удаление ветки возвращает как было.
    """
    if enable:
        ok = reg_write(HKCU, _CLSID_W11_MENU, "", "", winreg.REG_SZ)
    else:
        ok = _delete_tree(HKCU, r"Software\Classes\CLSID"
                                r"\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}")
    return ok


def _delete_tree(root, path: str) -> bool:
    """Удаляет ветку реестра вместе с подветками."""
    for child in _enum_keys(root, path):
        _delete_tree(root, f"{path}\\{child}")
    try:
        winreg.DeleteKey(root, path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log(f"Не удалось удалить ветку {path}: {exc}")
        return False


def build_explorer_tweaks() -> list[Tweak]:
    """Настройки поведения проводника."""
    tweaks: list[Tweak] = []

    tweaks.append(Tweak(
        id="exp_classic_menu",
        title="Вернуть полное меню правой кнопки (Windows 11)",
        desc="Отключает урезанное меню Windows 11 с пунктом «Показать "
             "дополнительные параметры».",
        why="Все пункты снова видны сразу, без второго клика. Это самая "
            "востребованная правка Windows 11.",
        category="Проводник", risk="низкий", admin=False, reboot=False,
        is_on=_classic_menu_state,
        turn_on=lambda: _set_classic_menu(True),
        turn_off=lambda: _set_classic_menu(False)))

    tweaks.append(_reg_tweak(
        "exp_extensions", "Показывать расширения файлов",
        "HideFileExt = 0 — видно, что файл на самом деле .exe, а не .jpg.",
        "На скрытых расширениях до сих пор ловят вирусы: «фото.jpg.exe» "
        "выглядит как «фото.jpg». Это вопрос безопасности, а не удобства.",
        "Проводник", HKCU, _ADVANCED, "HideFileExt", 0, 1, admin=False))

    tweaks.append(_reg_tweak(
        "exp_hidden", "Показывать скрытые файлы",
        "Hidden = 1 — скрытые файлы и папки видны.",
        "Нужно, чтобы добраться до папок настроек игр и AppData без "
        "ручного ввода пути.",
        "Проводник", HKCU, _ADVANCED, "Hidden", 1, 2,
        admin=False, recommended=False))

    tweaks.append(_reg_tweak(
        "exp_this_pc", "Открывать проводник на «Этом компьютере»",
        "LaunchTo = 1 вместо «Быстрого доступа».",
        "Сразу видно диски и их заполнение, а не список недавних файлов.",
        "Проводник", HKCU, _ADVANCED, "LaunchTo", 1, 2, admin=False))

    tweaks.append(_reg_tweak(
        "exp_recent_files", "Не показывать недавние файлы в быстром доступе",
        "ShowRecent = 0.",
        "Список последних открытых файлов не будет висеть на виду у любого, "
        "кто подошёл к компьютеру.",
        "Проводник", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer", "ShowRecent", 0, 1,
        admin=False, recommended=False))

    tweaks.append(_reg_tweak(
        "exp_recent_folders", "Не показывать частые папки в быстром доступе",
        "ShowFrequent = 0.",
        "То же самое для папок.",
        "Проводник", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer", "ShowFrequent", 0, 1,
        admin=False, recommended=False))

    tweaks.append(_reg_tweak(
        "exp_compact", "Компактный вид списка файлов",
        "UseCompactMode = 1 — строки в списке становятся плотнее.",
        "На экран помещается заметно больше файлов. Полезно на больших "
        "библиотеках игр и папках со скриншотами.",
        "Проводник", HKCU, _ADVANCED, "UseCompactMode", 1, 0,
        admin=False, recommended=False))

    tweaks.append(_reg_tweak(
        "exp_full_path", "Показывать полный путь в заголовке",
        "FullPath = 1.",
        "Видно, где именно ты находишься, — удобно, когда открыто "
        "несколько похожих папок.",
        "Проводник", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\CabinetState",
        "FullPath", 1, 0, admin=False, recommended=False))

    tweaks.append(_reg_tweak(
        "exp_no_sync_provider", "Убрать рекламу OneDrive из проводника",
        "ShowSyncProviderNotifications = 0.",
        "Windows перестанет показывать в проводнике баннеры с предложением "
        "купить место в OneDrive.",
        "Проводник", HKCU, _ADVANCED, "ShowSyncProviderNotifications", 0, 1,
        admin=False))

    tweaks.append(_reg_tweak(
        "exp_no_thumbs_db", "Не создавать файлы Thumbs.db в сетевых папках",
        "DisableThumbnailCache = 1.",
        "Windows перестанет засорять сетевые папки служебными файлами "
        "с миниатюрами.",
        "Проводник", HKCU, _ADVANCED, "DisableThumbnailCache", 1, 0,
        admin=False, recommended=False))

    return tweaks


def summary(handlers: list[Handler]) -> str:
    """Короткий итог по контекстному меню."""
    if not handlers:
        return "Обработчики контекстного меню прочитать не удалось."
    disabled = sum(1 for h in handlers if not h.enabled)
    unknown = sum(1 for h in handlers if not h.title)
    text = f"Найдено обработчиков: {len(handlers)}"
    if disabled:
        text += f" · отключено {disabled}"
    if unknown:
        text += (f" · не опознано {unknown} — это чужие программы, "
                 "приложение о них ничего не утверждает")
    return text + "."
