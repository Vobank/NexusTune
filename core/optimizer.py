"""Оптимизация Windows: набор твиков, каждый с описанием, риском и откатом.

Правило модуля: ничего не меняется без сохранения прежнего значения.
Всё, что применено, можно вернуть кнопкой «Откатить».
"""
from __future__ import annotations

import json
import re
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import SNAPSHOT_DIR, is_admin, log, run, run_ps

HKLM = winreg.HKEY_LOCAL_MACHINE
HKCU = winreg.HKEY_CURRENT_USER

_HIVE_NAMES = {HKLM: "HKLM", HKCU: "HKCU"}

# ---------------------------------------------------------------- работа с реестром


def reg_read(hive, path: str, name: str):
    """Значение из реестра или None, если его нет."""
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def reg_write(hive, path: str, name: str, value, vtype=winreg.REG_DWORD) -> bool:
    """Записывает значение, создавая ветку при необходимости."""
    try:
        with winreg.CreateKeyEx(hive, path, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, name, 0, vtype, value)
        return True
    except OSError as exc:
        log(f"Не удалось записать {_HIVE_NAMES.get(hive, '?')}\\{path}\\{name}: {exc}")
        return False


def reg_delete(hive, path: str, name: str) -> bool:
    """Удаляет значение; отсутствие значения тоже считается успехом."""
    try:
        with winreg.OpenKey(hive, path, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log(f"Не удалось удалить {path}\\{name}: {exc}")
        return False


# ---------------------------------------------------------------- модель твика


@dataclass
class Tweak:
    """Одна настройка системы."""
    id: str
    title: str
    desc: str                       # что делает
    why: str                        # зачем это игроку
    category: str
    risk: str = "низкий"            # низкий / средний / высокий
    reboot: bool = False            # нужна ли перезагрузка
    admin: bool = True
    recommended: bool = True
    expert: bool = False            # продвинутая/опасная настройка
    warn: str = ""                  # особое предупреждение для опасных твиков

    is_on: Callable[[], bool] = None
    turn_on: Callable[[], bool] = None
    turn_off: Callable[[], bool] = None

    def state(self) -> bool:
        try:
            return bool(self.is_on())
        except Exception as exc:
            log(f"Твик {self.id}: не удалось прочитать состояние: {exc}")
            return False

    def apply(self, enable: bool) -> bool:
        try:
            return bool(self.turn_on() if enable else self.turn_off())
        except Exception as exc:
            log(f"Твик {self.id}: ошибка применения: {exc}")
            return False


def _reg_tweak(tid, title, desc, why, category, hive, path, name, on_value,
               off_value=None, vtype=winreg.REG_DWORD, **kw) -> Tweak:
    """Твик, который сводится к одному значению реестра.

    off_value=None означает, что для отката значение удаляется.
    """
    def is_on():
        return reg_read(hive, path, name) == on_value

    def turn_on():
        return reg_write(hive, path, name, on_value, vtype)

    def turn_off():
        if off_value is None:
            return reg_delete(hive, path, name)
        return reg_write(hive, path, name, off_value, vtype)

    return Tweak(id=tid, title=title, desc=desc, why=why, category=category,
                 is_on=is_on, turn_on=turn_on, turn_off=turn_off, **kw)


# ---------------------------------------------------------------- планы электропитания

ULTIMATE_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"


# GUID плана и его название в скобках; звёздочка в конце помечает активный план.
# Разбираем регулярным выражением, потому что подпись строки зависит от языка Windows.
_PLAN_LINE = re.compile(
    r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s*\((.+?)\)\s*(\*)?\s*$")


def list_power_plans() -> list[tuple[str, str, bool]]:
    """Список планов: (guid, название, активен ли)."""
    code, out, _err = run(["powercfg", "/L"], timeout=15)
    plans: list[tuple[str, str, bool]] = []
    if code != 0:
        return plans
    for line in out.splitlines():
        match = _PLAN_LINE.search(line.strip())
        if match:
            plans.append((match.group(1), match.group(2).strip(), bool(match.group(3))))
    return plans


def active_power_plan() -> tuple[str, str]:
    """GUID и название активного плана."""
    for guid, name, active in list_power_plans():
        if active:
            return guid, name
    return "", "неизвестно"


def ensure_ultimate_plan() -> str:
    """Создаёт (если нужно) и возвращает GUID плана максимальной производительности."""
    for guid, _name, _a in list_power_plans():
        if guid.lower() == ULTIMATE_GUID:
            return ULTIMATE_GUID
    code, out, _ = run(["powercfg", "-duplicatescheme", ULTIMATE_GUID], timeout=15)
    if code == 0:
        for token in out.split():
            if len(token) == 36 and token.count("-") == 4:
                return token
    return HIGH_PERF_GUID


def set_power_plan(guid: str) -> bool:
    code, _o, _e = run(["powercfg", "/setactive", guid], timeout=15)
    return code == 0


# ---------------------------------------------------------------- службы


def service_state(name: str) -> str:
    """Тип запуска службы: Automatic / Manual / Disabled / отсутствует."""
    val = reg_read(HKLM, rf"SYSTEM\CurrentControlSet\Services\{name}", "Start")
    return {2: "Automatic", 3: "Manual", 4: "Disabled", 0: "Boot", 1: "System"}.get(val, "нет")


def set_service_start(name: str, mode: str) -> bool:
    """Меняет тип запуска службы (disabled / demand / auto)."""
    code, _o, err = run(["sc", "config", name, f"start={mode}"], timeout=15)
    if code != 0:
        log(f"sc config {name} start={mode}: {err}")
        return False
    if mode == "disabled":
        run(["sc", "stop", name], timeout=20)
    return True


def _service_tweak(tid, title, desc, why, service, category="Фоновые службы",
                   risk="низкий", **kw) -> Tweak:
    return Tweak(
        id=tid, title=title, desc=desc, why=why, category=category, risk=risk,
        is_on=lambda: service_state(service) == "Disabled",
        turn_on=lambda: set_service_start(service, "disabled"),
        turn_off=lambda: set_service_start(service, "demand"),
        **kw)


# ---------------------------------------------------------------- каталог твиков

_MM_PROFILE = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
_MM_GAMES = _MM_PROFILE + r"\Tasks\Games"


def build_tweaks() -> list[Tweak]:
    """Полный каталог доступных твиков."""
    tweaks: list[Tweak] = []

    # ---- Производительность
    tweaks.append(Tweak(
        id="power_plan", title="План электропитания «Максимальная производительность»",
        desc="Переключает Windows на схему без снижения частот процессора в простое.",
        why="Убирает микрозадержки, когда процессор просыпается из энергосберегающего состояния.",
        category="Производительность", risk="низкий",
        is_on=lambda: active_power_plan()[0].lower() in (ULTIMATE_GUID, HIGH_PERF_GUID),
        turn_on=lambda: set_power_plan(ensure_ultimate_plan()),
        turn_off=lambda: set_power_plan(BALANCED_GUID),
    ))

    tweaks.append(_reg_tweak(
        "hags", "Аппаратное ускорение планирования GPU",
        "Включает HAGS — видеокарта сама управляет очередью задач.",
        "Немного снижает задержку ввода и сглаживает кадр в играх на DirectX 12.",
        "Производительность", HKLM,
        r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2, 1,
        reboot=True))

    tweaks.append(_reg_tweak(
        "game_mode", "Игровой режим Windows",
        "Включает Game Mode: система отдаёт игре приоритет и придерживает фоновые задачи.",
        "Меньше просадок при фоновых обновлениях и антивирусных проверках.",
        "Производительность", HKCU, r"Software\Microsoft\GameBar",
        "AutoGameModeEnabled", 1, 0, admin=False))

    tweaks.append(_reg_tweak(
        "gpu_priority", "Приоритет GPU для игр",
        "Ставит играм высокий приоритет планировщика мультимедиа (GPU Priority = 8).",
        "Игра получает ресурсы раньше фоновых приложений вроде браузера и Discord.",
        "Производительность", HKLM, _MM_GAMES, "GPU Priority", 8, 8))

    tweaks.append(_reg_tweak(
        "cpu_priority_games", "Приоритет CPU для игр",
        "Ставит играм приоритет 6 и категорию планирования High.",
        "Помогает, когда в фоне работают браузер, стриминг или загрузки.",
        "Производительность", HKLM, _MM_GAMES, "Priority", 6, 2))

    tweaks.append(_reg_tweak(
        "sys_responsiveness", "Резерв системы под фон — 10%",
        "SystemResponsiveness = 10 вместо стандартных 20.",
        "Windows резервирует меньше мощности под фоновые задачи, отдавая её игре.",
        "Производительность", HKLM, _MM_PROFILE, "SystemResponsiveness", 10, 20))

    tweaks.append(_reg_tweak(
        "prio_separation", "Приоритет активного окна",
        "Win32PrioritySeparation = 38 — короткие кванты времени для активной программы.",
        "Активная игра реже уступает процессор фоновым процессам.",
        "Производительность", HKLM,
        r"SYSTEM\CurrentControlSet\Control\PriorityControl",
        "Win32PrioritySeparation", 38, 2))

    # ---- Отклик и ввод
    tweaks.append(Tweak(
        id="mouse_accel", title="Отключить ускорение мыши",
        desc="Убирает «повышенную точность указателя» Windows.",
        why="Прицел становится предсказуемым: одинаковое движение руки — одинаковый поворот.",
        category="Отклик и ввод", risk="низкий", admin=False,
        is_on=lambda: reg_read(HKCU, r"Control Panel\Mouse", "MouseSpeed") == "0",
        turn_on=lambda: all([
            reg_write(HKCU, r"Control Panel\Mouse", "MouseSpeed", "0", winreg.REG_SZ),
            reg_write(HKCU, r"Control Panel\Mouse", "MouseThreshold1", "0", winreg.REG_SZ),
            reg_write(HKCU, r"Control Panel\Mouse", "MouseThreshold2", "0", winreg.REG_SZ)]),
        turn_off=lambda: all([
            reg_write(HKCU, r"Control Panel\Mouse", "MouseSpeed", "1", winreg.REG_SZ),
            reg_write(HKCU, r"Control Panel\Mouse", "MouseThreshold1", "6", winreg.REG_SZ),
            reg_write(HKCU, r"Control Panel\Mouse", "MouseThreshold2", "10", winreg.REG_SZ)]),
    ))

    tweaks.append(_reg_tweak(
        "net_throttle", "Снять сетевой троттлинг",
        "NetworkThrottlingIndex = отключён (0xFFFFFFFF).",
        "Windows перестаёт искусственно ограничивать сетевые пакеты — меньше пинг в онлайне.",
        "Отклик и ввод", HKLM, _MM_PROFILE, "NetworkThrottlingIndex", 0xFFFFFFFF, 10))

    tweaks.append(_reg_tweak(
        "fso", "Отключить полноэкранную оптимизацию",
        "GlobalUserDisabled = 1 для полноэкранных оптимизаций Windows.",
        "Возвращает настоящий эксклюзивный полный экран — заметно меньше задержка ввода.",
        "Отклик и ввод", HKCU, r"System\GameConfigStore",
        "GameDVR_FSEBehaviorMode", 2, 0, admin=False, recommended=False))

    # ---- Мусор в фоне
    tweaks.append(_reg_tweak(
        "game_dvr", "Отключить запись игр (Game DVR)",
        "Отключает фоновую запись последних минут игры.",
        "Game DVR постоянно пишет видео в фоне и отъедает 3-8% кадров.",
        "Фоновые службы", HKCU, r"System\GameConfigStore", "GameDVR_Enabled", 0, 1,
        admin=False))

    tweaks.append(_reg_tweak(
        "xbox_gamebar", "Отключить игровую панель Xbox",
        "AppCaptureEnabled = 0 — панель Xbox перестаёт висеть в памяти.",
        "Освобождает память и убирает всплывающий оверлей по Win+G.",
        "Фоновые службы", HKCU, r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
        "AppCaptureEnabled", 0, 1, admin=False))

    tweaks.append(_reg_tweak(
        "bg_apps", "Отключить фоновые приложения Microsoft Store",
        "GlobalUserDisabled = 1 для UWP-приложений.",
        "Погода, Почта и прочие плитки перестают просыпаться и есть ресурсы.",
        "Фоновые службы", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
        "GlobalUserDisabled", 1, 0, admin=False))

    tweaks.append(_reg_tweak(
        "telemetry", "Свести телеметрию к минимуму",
        "AllowTelemetry = 0 в политике сбора данных.",
        "Меньше фоновой отправки данных, меньше внезапных всплесков нагрузки на диск.",
        "Фоновые службы", HKLM,
        r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0, 3))

    tweaks.append(_service_tweak(
        "svc_diagtrack", "Отключить службу телеметрии DiagTrack",
        "Переводит «Функциональные возможности для подключённых пользователей» в отключённые.",
        "Служба периодически нагружает диск и сеть без пользы для игр.",
        "DiagTrack"))

    tweaks.append(_service_tweak(
        "svc_sysmain", "Отключить SysMain (Superfetch)",
        "Отключает предзагрузку программ в память.",
        "На SSD пользы почти нет, а фоновые чтения диска мешают подгрузке уровней.",
        "SysMain"))

    tweaks.append(_service_tweak(
        "svc_wsearch", "Отключить индексацию поиска Windows",
        "Останавливает службу Windows Search.",
        "Индексатор любит просыпаться в разгар игры и грузить диск.",
        "WSearch", risk="средний", recommended=False))

    # ---- Интерфейс
    tweaks.append(Tweak(
        id="visual_fx", title="Визуальные эффекты — на производительность",
        desc="Отключает анимации окон и тени, сглаживание шрифтов остаётся.",
        why="Рабочий стол отзывается мгновенно, освобождается немного видеопамяти.",
        category="Интерфейс", risk="низкий", admin=False,
        is_on=lambda: reg_read(
            HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
            "VisualFXSetting") == 2,
        turn_on=lambda: reg_write(
            HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
            "VisualFXSetting", 2),
        turn_off=lambda: reg_write(
            HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
            "VisualFXSetting", 0),
    ))

    tweaks.append(_reg_tweak(
        "transparency", "Отключить прозрачность интерфейса",
        "EnableTransparency = 0 в персонализации.",
        "Эффект акрила рисуется видеокартой постоянно, даже поверх игры в окне.",
        "Интерфейс", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        "EnableTransparency", 0, 1, admin=False))

    tweaks.append(_reg_tweak(
        "fast_startup", "Отключить быстрый запуск",
        "HiberbootEnabled = 0 — Windows выключается полностью.",
        "Гибридное выключение копит ошибки драйверов; без него система стабильнее.",
        "Интерфейс", HKLM,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
        "HiberbootEnabled", 0, 1, reboot=True, recommended=False))

    # ---- Скорость системы
    tweaks.append(_reg_tweak(
        "power_throttling", "Отключить придушивание фоновых программ",
        "PowerThrottlingOff = 1 — Windows перестаёт снижать частоты неактивным окнам.",
        "Свёрнутая игра, античит и голосовой чат не уходят в замедленный режим.",
        "Производительность", HKLM,
        r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
        "PowerThrottlingOff", 1, 0))

    tweaks.append(_reg_tweak(
        "startup_delay", "Убрать задержку автозагрузки",
        "StartupDelayInMSec = 0 вместо стандартных десяти секунд.",
        "Рабочий стол становится рабочим сразу, а не через минуту после входа.",
        "Производительность", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize",
        "StartupDelayInMSec", 0, None, admin=False))

    tweaks.append(Tweak(
        id="menu_delay", title="Убрать задержку меню",
        desc="MenuShowDelay = 0 вместо 400 миллисекунд.",
        why="Меню и подменю раскрываются мгновенно — интерфейс кажется быстрее.",
        category="Производительность", risk="низкий", admin=False,
        is_on=lambda: reg_read(HKCU, r"Control Panel\Desktop", "MenuShowDelay") == "0",
        turn_on=lambda: reg_write(HKCU, r"Control Panel\Desktop", "MenuShowDelay",
                                  "0", winreg.REG_SZ),
        turn_off=lambda: reg_write(HKCU, r"Control Panel\Desktop", "MenuShowDelay",
                                   "400", winreg.REG_SZ),
    ))

    tweaks.append(Tweak(
        id="ntfs_lastaccess", title="Не записывать время последнего доступа",
        desc="Отключает обновление метки последнего доступа к файлам в NTFS.",
        why="Каждое чтение файла перестаёт вызывать запись на диск. "
            "Заметно на играх с тысячами мелких файлов.",
        category="Производительность", risk="низкий",
        is_on=lambda: "1" in (run(["fsutil", "behavior", "query",
                                   "disablelastaccess"], timeout=15)[1] or ""),
        turn_on=lambda: run(["fsutil", "behavior", "set",
                             "disablelastaccess", "1"], timeout=15)[0] == 0,
        turn_off=lambda: run(["fsutil", "behavior", "set",
                              "disablelastaccess", "0"], timeout=15)[0] == 0,
    ))

    # ---- Сеть
    tweaks.append(_reg_tweak(
        "qos_reserve", "Снять резерв канала под QoS",
        "NonBestEffortLimit = 0 в политике планировщика пакетов.",
        "Windows по умолчанию придерживает часть канала под служебный трафик.",
        "Отклик и ввод", HKLM,
        r"SOFTWARE\Policies\Microsoft\Windows\Psched", "NonBestEffortLimit", 0, 20))

    tweaks.append(Tweak(
        id="nagle", title="Отключить алгоритм Нейгла",
        desc="TcpAckFrequency и TCPNoDelay для всех сетевых интерфейсов.",
        why="Мелкие пакеты перестают копиться перед отправкой — "
            "в онлайн-играх это несколько миллисекунд отклика.",
        category="Отклик и ввод", risk="низкий",
        is_on=lambda: _nagle_state(),
        turn_on=lambda: _set_nagle(True),
        turn_off=lambda: _set_nagle(False),
    ))

    # ---- Реклама и лишнее
    tweaks.append(_reg_tweak(
        "win_tips", "Отключить советы и рекламу Windows",
        "SubscribedContent-338389Enabled = 0 в доставке контента.",
        "Пропадают всплывающие подсказки, реклама приложений и предложения Microsoft.",
        "Фоновые службы", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
        "SubscribedContent-338389Enabled", 0, 1, admin=False))

    tweaks.append(_reg_tweak(
        "web_search", "Убрать поиск в интернете из меню Пуск",
        "BingSearchEnabled = 0 — меню Пуск ищет только по компьютеру.",
        "Меню открывается мгновенно и не ждёт ответа от серверов Microsoft.",
        "Фоновые службы", HKCU,
        r"Software\Microsoft\Windows\CurrentVersion\Search",
        "BingSearchEnabled", 0, 1, admin=False))

    tweaks.append(_reg_tweak(
        "widgets", "Отключить виджеты и новости",
        "AllowNewsAndInterests = 0 в политике панели задач.",
        "Панель виджетов держит в памяти отдельный браузерный процесс.",
        "Фоновые службы", HKLM,
        r"SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", 0, 1))

    tweaks.append(_reg_tweak(
        "edge_preload", "Запретить Edge грузиться в фоне",
        "StartupBoostEnabled = 0 в политике браузера Edge.",
        "Edge перестаёт держать процессы в памяти, даже когда закрыт.",
        "Фоновые службы", HKLM,
        r"SOFTWARE\Policies\Microsoft\Edge", "StartupBoostEnabled", 0, 1))

    # ---- Звук
    tweaks.append(_reg_tweak(
        "mmcss_audio", "Повысить приоритет звука",
        "Priority = 6 для мультимедийной задачи Audio.",
        "Голос в Discord и звук в игре перестают заикаться под нагрузкой.",
        "Отклик и ввод", HKLM,
        _MM_PROFILE + r"\Tasks\Audio", "Priority", 6, 6, recommended=False))

    return tweaks


# ---------------------------------------------------------------- алгоритм Нейгла

_TCPIP_INTERFACES = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"


def _active_interfaces() -> list[str]:
    """Ветки реестра сетевых интерфейсов, у которых есть IP-адрес."""
    found = []
    try:
        with winreg.OpenKey(HKLM, _TCPIP_INTERFACES) as root:
            count = winreg.QueryInfoKey(root)[0]
            for i in range(count):
                guid = winreg.EnumKey(root, i)
                path = f"{_TCPIP_INTERFACES}\\{guid}"
                has_ip = reg_read(HKLM, path, "DhcpIPAddress") or reg_read(
                    HKLM, path, "IPAddress")
                if has_ip and str(has_ip) not in ("0.0.0.0", "['0.0.0.0']"):
                    found.append(path)
    except OSError:
        pass
    return found


def _nagle_state() -> bool:
    paths = _active_interfaces()
    if not paths:
        return False
    return all(reg_read(HKLM, p, "TcpAckFrequency") == 1 for p in paths)


def _set_nagle(enabled: bool) -> bool:
    paths = _active_interfaces()
    if not paths:
        return False
    ok = True
    for path in paths:
        if enabled:
            ok &= reg_write(HKLM, path, "TcpAckFrequency", 1)
            ok &= reg_write(HKLM, path, "TCPNoDelay", 1)
        else:
            ok &= reg_delete(HKLM, path, "TcpAckFrequency")
            ok &= reg_delete(HKLM, path, "TCPNoDelay")
    return ok


# ---------------------------------------------------------------- снимки состояния


def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.json"


def save_snapshot(tweaks: list[Tweak], name: str = "before_optimize") -> Path:
    """Сохраняет текущее состояние всех твиков, чтобы можно было откатиться."""
    data = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "power_plan": active_power_plan()[0],
        "states": {t.id: t.state() for t in tweaks},
    }
    path = snapshot_path(name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Снимок настроек сохранён: {path}")
    return path


def load_snapshot(name: str = "before_optimize") -> dict | None:
    path = snapshot_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def restore_snapshot(tweaks: list[Tweak], name: str = "before_optimize") -> tuple[int, int]:
    """Возвращает систему к сохранённому состоянию. Отдаёт (успешно, всего)."""
    snap = load_snapshot(name)
    if not snap:
        return 0, 0
    states = snap.get("states", {})
    ok = 0
    targets = [t for t in tweaks if t.id in states]
    for t in targets:
        want = bool(states[t.id])
        if t.state() != want and t.apply(want):
            ok += 1
        elif t.state() == want:
            ok += 1
    if snap.get("power_plan"):
        set_power_plan(snap["power_plan"])
    log(f"Откат по снимку {name}: {ok}/{len(targets)}")
    return ok, len(targets)


def create_restore_point(description: str = "Kremen — перед оптимизацией") -> bool:
    """Создаёт точку восстановления Windows (нужны права администратора)."""
    if not is_admin():
        return False
    run_ps("Enable-ComputerRestore -Drive 'C:\\' -ErrorAction SilentlyContinue", timeout=60)
    code, _o, err = run_ps(
        f'Checkpoint-Computer -Description "{description}" '
        '-RestorePointType MODIFY_SETTINGS', timeout=180)
    if code != 0:
        log(f"Точка восстановления не создана: {err}")
    return code == 0
