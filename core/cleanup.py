"""Уборка системы: мусорные задачи планировщика, автозагрузка и лишние файлы.

Всё, что здесь отключается, отключается обратимо и записывается в журнал
приложения, чтобы можно было вернуть ровно то, что тронули.
"""
from __future__ import annotations

import json
import os
import shutil
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from .util import APP_DIR, is_admin, log, ps_json, run, run_ps

STATE_FILE = APP_DIR / "cleanup_state.json"

HKLM = winreg.HKEY_LOCAL_MACHINE
HKCU = winreg.HKEY_CURRENT_USER


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"tasks": [], "startup": []}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить состояние уборки: {exc}")


# ================================================================ задачи планировщика


@dataclass
class JunkTask:
    path: str
    name: str
    title: str
    why: str


# Задачи, которые безопасно усыпить. Ни одна из них не нужна для работы Windows.
JUNK_TASKS: list[JunkTask] = [
    JunkTask(r"\Microsoft\Windows\Application Experience\\", "Microsoft Compatibility Appraiser",
             "Оценщик совместимости",
             "Собирает данные о программах для Microsoft. Регулярно грузит диск на минуты."),
    JunkTask(r"\Microsoft\Windows\Application Experience\\", "ProgramDataUpdater",
             "Сборщик данных о программах",
             "Часть той же телеметрии совместимости."),
    JunkTask(r"\Microsoft\Windows\Application Experience\\", "StartupAppTask",
             "Анализ автозагрузки",
             "Считает, как долго стартуют программы, и отправляет отчёт."),
    JunkTask(r"\Microsoft\Windows\Customer Experience Improvement Program\\", "Consolidator",
             "Программа улучшения качества",
             "Отправляет статистику использования в Microsoft."),
    JunkTask(r"\Microsoft\Windows\Customer Experience Improvement Program\\", "UsbCeip",
             "Телеметрия USB",
             "Собирает данные о подключённых USB-устройствах."),
    JunkTask(r"\Microsoft\Windows\Customer Experience Improvement Program\\", "KernelCeipTask",
             "Телеметрия ядра",
             "Собирает данные о работе ядра системы."),
    JunkTask(r"\Microsoft\Windows\DiskDiagnostic\\",
             "Microsoft-Windows-DiskDiagnosticDataCollector",
             "Сбор данных о дисках",
             "Отправляет статистику дисков. Проверка SMART работает и без неё."),
    JunkTask(r"\Microsoft\Windows\Feedback\Siuf\\", "DmClient",
             "Запросы обратной связи",
             "Показывает опросы «как вам Windows». Больше ничего не делает."),
    JunkTask(r"\Microsoft\Windows\Feedback\Siuf\\", "DmClientOnScenarioDownload",
             "Загрузка сценариев опросов",
             "Догружает шаблоны для тех же опросов."),
    JunkTask(r"\Microsoft\Windows\Windows Error Reporting\\", "QueueReporting",
             "Очередь отчётов об ошибках",
             "Отправляет дампы сбоев. Сами дампы остаются на диске."),
    JunkTask(r"\Microsoft\Windows\CloudExperienceHost\\", "CreateObjectTask",
             "Облачный помощник входа",
             "Нужен только для первичной настройки учётной записи."),
    JunkTask(r"\Microsoft\Windows\Maps\\", "MapsToastTask",
             "Уведомления Карт",
             "Реклама офлайн-карт. Полезно, только если пользуешься Картами Windows."),
    JunkTask(r"\Microsoft\Windows\Maps\\", "MapsUpdateTask",
             "Обновление Карт",
             "Качает обновления карт в фоне, даже если ты ими не пользуешься."),
    JunkTask(r"\Microsoft\Windows\Retail Demo\\", "CleanupOfflineContent",
             "Очистка демо-режима",
             "Нужна только компьютерам-витринам в магазинах."),
    JunkTask(r"\Microsoft\Windows\Autochk\\", "Proxy",
             "Сбор данных Autochk",
             "Отправляет данные проверки диска в программу улучшения качества."),
    JunkTask(r"\Microsoft\Windows\PI\\", "Sqm-Tasks",
             "Телеметрия платформы",
             "Ещё один сборщик статистики."),
    JunkTask(r"\Microsoft\Windows\Shell\\", "FamilySafetyMonitor",
             "Родительский контроль",
             "Нужен, только если настроен семейный доступ."),
    JunkTask(r"\Microsoft\Windows\Shell\\", "FamilySafetyRefreshTask",
             "Обновление родительского контроля",
             "Часть того же семейного доступа."),
]


def task_states() -> dict[str, str]:
    """Состояние всех известных мусорных задач: имя -> Ready/Disabled/нет."""
    rows = ps_json(
        "Get-ScheduledTask -ErrorAction SilentlyContinue | "
        "Select-Object TaskName,TaskPath,State", timeout=45)
    if isinstance(rows, dict):
        rows = [rows]
    found: dict[str, str] = {}
    if not isinstance(rows, list):
        return found
    # State: 1 Disabled, 3 Ready, 4 Running
    names = {3: "Ready", 1: "Disabled", 4: "Running"}
    for row in rows:
        name = str(row.get("TaskName") or "")
        state = row.get("State")
        found[name] = names.get(state, str(state))
    return found


def set_task(task: JunkTask, enabled: bool) -> bool:
    """Включает или отключает одну задачу планировщика."""
    verb = "Enable-ScheduledTask" if enabled else "Disable-ScheduledTask"
    path = task.path.replace("\\\\", "\\")
    code, _out, err = run_ps(
        f"{verb} -TaskPath '{path}' -TaskName '{task.name}' "
        "-ErrorAction SilentlyContinue | Out-Null; if ($?) {'OK'} else {'FAIL'}",
        timeout=25)
    return code == 0


def set_junk_tasks(enabled: bool) -> tuple[int, int]:
    """Массово усыпляет или будит мусорные задачи. Возвращает (сделано, всего)."""
    if not is_admin():
        return 0, len(JUNK_TASKS)
    state = _load_state()
    done = 0
    touched: list[str] = []
    for task in JUNK_TASKS:
        if not enabled and task.name in state.get("tasks", []):
            done += 1                                   # уже усыплена нами
            touched.append(task.name)
            continue
        if set_task(task, enabled):
            done += 1
            if not enabled:
                touched.append(task.name)
    state["tasks"] = touched if not enabled else []
    _save_state(state)
    log(f"Задачи планировщика: {'включено' if enabled else 'усыплено'} {done}/{len(JUNK_TASKS)}")
    return done, len(JUNK_TASKS)


# ================================================================ автозагрузка


@dataclass
class StartupItem:
    name: str
    command: str
    location: str                    # где прописан: HKCU/HKLM/папка
    enabled: bool = True
    heavy: bool = False              # известен как прожорливый
    hint: str = ""


_RUN_KEYS = [
    (HKCU, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU", "Run"),
    (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "HKLM", "Run"),
    (HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM32", "Run32"),
]

_APPROVED_BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

# Программы, которые спокойно живут без автозагрузки: запускаются вручную за секунду
HEAVY_STARTUP = {
    "spotify": "Музыку можно открыть вручную, когда она нужна.",
    "epicgameslauncher": "Лаунчер запустится сам при старте игры.",
    "eadm": "Приложение EA стартует вместе с игрой.",
    "ea desktop": "Приложение EA стартует вместе с игрой.",
    "steam": "Steam запустится сам, когда откроешь игру.",
    "discordptb": "Тестовая сборка Discord — вторая копия того же клиента.",
    "tidal": "Музыкальный клиент, нужен только когда слушаешь.",
    "robloxplayerbeta": "Игровой клиент в автозагрузке не нужен.",
    "wallpaperengine": "Живые обои постоянно рисуются видеокартой.",
    "onedrive": "Синхронизацию можно включать по необходимости.",
    "skype": "Мессенджер, который редко нужен сразу после включения.",
    "teams": "Тяжёлый клиент, стартует минуту и ест память.",
    "cortana": "Голосовой помощник, почти никем не используется.",
    "adobe": "Служебные модули Adobe, обновления найдутся и без них.",
    "ccleaner": "Фоновый монитор чистильщика.",
    "utorrent": "Торрент-клиент грузит диск и сеть с самого старта.",
    "qbittorrent": "Торрент-клиент грузит диск и сеть с самого старта.",
}

# Их лучше не трогать: они нужны для работы железа и ввода
PROTECTED_STARTUP = ("lghub", "icue", "synapse", "armoury", "mystic", "nvidia",
                     "realtek", "nahimic", "audio", "securityhealth", "windowsdefender",
                     "wacom", "logi", "steelseries", "hidpp", "touchpad", "synaptics")


def _approved_status(subkey: str, name: str) -> bool | None:
    """Читает отметку включения из StartupApproved. None — записи нет."""
    for hive in (HKCU, HKLM):
        try:
            with winreg.OpenKey(hive, f"{_APPROVED_BASE}\\{subkey}") as key:
                value, _kind = winreg.QueryValueEx(key, name)
                if isinstance(value, bytes) and value:
                    return value[0] in (2, 6)
        except OSError:
            continue
    return None


def _set_approved(subkey: str, name: str, enabled: bool) -> bool:
    """Ставит отметку включения так же, как это делает диспетчер задач."""
    flag = 2 if enabled else 3
    payload = bytes([flag] + [0] * 11)
    try:
        with winreg.CreateKeyEx(HKCU, f"{_APPROVED_BASE}\\{subkey}", 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, payload)
        return True
    except OSError as exc:
        log(f"Не удалось изменить автозагрузку {name}: {exc}")
        return False


def _classify(name: str, command: str) -> tuple[bool, str]:
    """Тяжёлая ли это программа и почему её можно убрать."""
    blob = f"{name} {command}".lower()
    if any(word in blob for word in PROTECTED_STARTUP):
        return False, "Нужна для работы устройства или ввода — лучше оставить."
    for word, hint in HEAVY_STARTUP.items():
        if word in blob:
            return True, hint
    return False, ""


def startup_items() -> list[StartupItem]:
    """Всё, что стартует вместе с Windows."""
    items: list[StartupItem] = []

    for hive, path, label, approved_key in _RUN_KEYS:
        try:
            with winreg.OpenKey(hive, path) as key:
                count = winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    name, value, _kind = winreg.EnumValue(key, i)
                    enabled = _approved_status(approved_key, name)
                    heavy, hint = _classify(name, str(value))
                    items.append(StartupItem(
                        name=name, command=str(value), location=label,
                        enabled=True if enabled is None else enabled,
                        heavy=heavy, hint=hint))
        except OSError:
            continue

    # ярлыки в папке автозагрузки
    folders = [
        (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
         "Start Menu" / "Programs" / "Startup", "Папка"),
        (Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" /
         "Start Menu" / "Programs" / "Startup", "Папка (все)"),
    ]
    for folder, label in folders:
        if not folder.exists():
            continue
        for link in folder.iterdir():
            if link.suffix.lower() not in (".lnk", ".url", ".exe", ".bat"):
                continue
            enabled = _approved_status("StartupFolder", link.name)
            heavy, hint = _classify(link.stem, str(link))
            items.append(StartupItem(
                name=link.stem, command=str(link), location=label,
                enabled=True if enabled is None else enabled,
                heavy=heavy, hint=hint))

    items.sort(key=lambda x: (not x.heavy, x.name.lower()))
    return items


def _approved_key_for(item: StartupItem) -> tuple[str, str]:
    """Куда писать отметку включения для этого элемента."""
    if item.location.startswith("Папка"):
        return "StartupFolder", Path(item.command).name
    if item.location == "HKLM32":
        return "Run32", item.name
    return "Run", item.name


def set_startup(item: StartupItem, enabled: bool) -> bool:
    """Включает или отключает один элемент автозагрузки."""
    subkey, name = _approved_key_for(item)
    ok = _set_approved(subkey, name, enabled)
    if ok:
        state = _load_state()
        disabled = set(state.get("startup", []))
        if enabled:
            disabled.discard(item.name)
        else:
            disabled.add(item.name)
        state["startup"] = sorted(disabled)
        _save_state(state)
    return ok


def trim_startup() -> int:
    """Отключает известные тяжёлые программы из автозагрузки."""
    done = 0
    for item in startup_items():
        if item.heavy and item.enabled and set_startup(item, False):
            done += 1
    log(f"Автозагрузка: отключено {done} программ")
    return done


def restore_startup() -> int:
    """Возвращает всё, что отключало приложение."""
    state = _load_state()
    names = set(state.get("startup", []))
    if not names:
        return 0
    done = 0
    for item in startup_items():
        if item.name in names and set_startup(item, True):
            done += 1
    state["startup"] = []
    _save_state(state)
    log(f"Автозагрузка: восстановлено {done} программ")
    return done


# ================================================================ мусорные файлы


@dataclass
class JunkCategory:
    key: str
    title: str
    why: str
    paths: list = field(default_factory=list)
    risk: str = "низкий"
    default: bool = True


def _env_path(*parts) -> Path | None:
    base = os.environ.get(parts[0])
    if not base:
        return None
    return Path(base).joinpath(*parts[1:])


def junk_categories() -> list[JunkCategory]:
    """Что именно приложение считает мусором. Ничего личного тут не удаляется."""
    local = os.environ.get("LOCALAPPDATA", "")
    windir = os.environ.get("WINDIR", r"C:\Windows")

    return [
        JunkCategory(
            "temp", "Временные файлы",
            "Хвосты установщиков и распаковщиков. Windows их не чистит сама.",
            [Path(os.environ.get("TEMP", "")), Path(windir) / "Temp"]),
        JunkCategory(
            "shader_cache", "Кэш шейдеров видеокарты",
            "После обновления драйвера старый кэш вызывает рывки в играх. "
            "Он пересоздастся сам за пару минут игры.",
            [Path(local) / "D3DSCache",
             Path(local) / "NVIDIA" / "DXCache",
             Path(local) / "NVIDIA" / "GLCache",
             Path(local) / "AMD" / "DxCache",
             Path(local) / "AMD" / "DxcCache",
             Path(local) / "Intel" / "ShaderCache"]),
        JunkCategory(
            "dumps", "Отчёты о сбоях",
            "Дампы упавших программ. Занимают сотни мегабайт и никому не нужны.",
            [Path(local) / "CrashDumps", Path(windir) / "Minidump"]),
        JunkCategory(
            "update_cache", "Кэш обновлений Windows",
            "Установщики уже применённых обновлений. Часто несколько гигабайт.",
            [Path(windir) / "SoftwareDistribution" / "Download"], risk="низкий"),
        JunkCategory(
            "delivery", "Кэш раздачи обновлений",
            "Куски обновлений, которые Windows раздаёт другим компьютерам в сети.",
            [Path(windir) / "ServiceProfiles" / "NetworkService" / "AppData" / "Local" /
             "Microsoft" / "Windows" / "DeliveryOptimization"]),
        JunkCategory(
            "logs", "Старые системные журналы",
            "Логи установки компонентов и обновлений.",
            [Path(windir) / "Logs" / "CBS", Path(windir) / "Logs" / "DISM",
             Path(windir) / "Panther"], default=False),
        JunkCategory(
            "browser_cache", "Кэш браузеров",
            "Сайты будут грузиться чуть дольше первый раз, пароли и вкладки не тронутся.",
            [Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "Cache",
             Path(local) / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache",
             Path(local) / "Yandex" / "YandexBrowser" / "User Data" / "Default" / "Cache"],
            default=False),
    ]


def _dir_size(path: Path, limit_files: int = 40000) -> int:
    """Размер папки в байтах. Считает быстро и не спотыкается на занятых файлах."""
    total = 0
    count = 0
    if not path or not path.exists():
        return 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda e: None):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                count += 1
                if count >= limit_files:
                    return total
    except OSError:
        pass
    return total


def scan_junk() -> list[dict]:
    """Считает, сколько занимает каждая категория мусора."""
    result = []
    for category in junk_categories():
        size = sum(_dir_size(p) for p in category.paths if p)
        result.append({
            "key": category.key, "title": category.title, "why": category.why,
            "bytes": size, "mb": round(size / (1024 ** 2), 1),
            "default": category.default, "risk": category.risk,
        })
    return result


def _wipe(path: Path) -> int:
    """Удаляет содержимое папки, пропуская занятые файлы. Возвращает освобождённые байты."""
    if not path or not path.exists():
        return 0
    freed = 0
    for entry in path.iterdir():
        try:
            if entry.is_dir():
                size = _dir_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    freed += size
            else:
                size = entry.stat().st_size
                entry.unlink()
                freed += size
        except OSError:
            continue                                   # файл занят — просто пропускаем
    return freed


def clean_categories(keys: list[str]) -> int:
    """Чистит выбранные категории. Возвращает освобождённые мегабайты."""
    wanted = {c.key: c for c in junk_categories()}
    freed = 0
    for key in keys:
        category = wanted.get(key)
        if not category:
            continue
        for path in category.paths:
            freed += _wipe(path)
    mb = round(freed / (1024 ** 2), 1)
    if mb:
        log(f"Уборка: освобождено {mb} МБ ({', '.join(keys)})")
    return int(mb)


# Флаги SHEmptyRecycleBin: без вопросов, без окна прогресса, без звука
SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


def current_user_sid() -> str | None:
    """Строковый SID текущего пользователя.

    Нужен, чтобы чистить только свою корзину. В корзине лежат папки
    всех пользователей компьютера, и трогать чужие нельзя даже с правами
    администратора.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32

    size = wintypes.DWORD(0)
    advapi.GetUserNameW(None, ctypes.byref(size))
    name = ctypes.create_unicode_buffer(size.value or 256)
    if not advapi.GetUserNameW(name, ctypes.byref(size)):
        return None

    sid_size = wintypes.DWORD(0)
    domain_size = wintypes.DWORD(0)
    use = wintypes.DWORD(0)
    advapi.LookupAccountNameW(None, name, None, ctypes.byref(sid_size),
                              None, ctypes.byref(domain_size), ctypes.byref(use))
    if not sid_size.value:
        return None

    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value or 256)
    if not advapi.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size),
                                     domain, ctypes.byref(domain_size),
                                     ctypes.byref(use)):
        return None

    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    result = text.value
    kernel.LocalFree(text)
    return result


def _wipe_recycle_folders(sid: str) -> int:
    """Удаляет содержимое своей корзины на всех дисках. Возвращает МБ."""
    freed = 0
    for letter in "CDEFGH":
        folder = Path(letter + r":\$Recycle.Bin") / sid
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            # desktop.ini — служебный файл самой корзины, его не трогаем
            if entry.name.lower() == "desktop.ini":
                continue
            try:
                if entry.is_dir():
                    size = _dir_size(entry)
                    shutil.rmtree(entry, ignore_errors=True)
                    if not entry.exists():
                        freed += size
                else:
                    size = entry.stat().st_size
                    entry.unlink()
                    freed += size
            except OSError:
                continue                     # файл занят — просто пропускаем
    return round(freed / (1024 ** 2))


def empty_recycle_bin() -> tuple[bool, str]:
    """Очищает корзину.

    Сначала пробуем Shell API — тот же путь, которым идёт проводник. Он
    надёжнее PowerShell-команды Clear-RecycleBin, которая на части систем
    возвращает успех, ничего не очистив.

    Но и Shell API иногда отвечает E_UNEXPECTED — например, если в корзине
    лежит очень крупный файл или её служебные записи разъехались. Тогда
    удаляем файлы напрямую из папки корзины своего пользователя: с точки
    зрения системы они и так уже удалены.
    """
    import ctypes

    before = recycle_bin_size()
    if before <= 0:
        return True, "Корзина уже пуста."

    try:
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags) & 0xFFFFFFFF
    except Exception as exc:
        log(f"Shell API очистки корзины недоступен: {exc}")
        result = 0xFFFFFFFF

    after = recycle_bin_size()
    if result == 0 or after < before:
        freed = max(0, before - after)
        log(f"Корзина очищена через Shell API, освобождено {freed} МБ")
        return True, f"Корзина очищена — освобождено {freed} МБ."

    # Запасной путь: удаляем файлы своей корзины руками
    sid = current_user_sid()
    if not sid:
        return False, ("Windows отклонила очистку корзины, а определить твою "
                       "учётную запись не удалось. Очисти корзину из проводника.")

    log(f"Shell API вернул {result:#010x}, чищу корзину напрямую")
    freed = _wipe_recycle_folders(sid)
    if freed:
        return True, (f"Корзина очищена — освобождено {freed} МБ. "
                      "Windows отказалась делать это своим способом, "
                      "файлы удалены напрямую.")
    return False, ("Очистить корзину не удалось: файлы заняты другой программой. "
                   "Закрой игры и лаунчеры и попробуй снова.")


def recycle_bin_size() -> int:
    """Размер корзины в мегабайтах."""
    total = 0
    for drive in ("C", "D", "E"):
        path = Path(f"{drive}:\\$Recycle.Bin")
        if path.exists():
            total += _dir_size(path, limit_files=8000)
    return round(total / (1024 ** 2))
