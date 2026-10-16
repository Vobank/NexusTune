"""Экспертные настройки Windows — то, до чего не добирается ни одна панель.

Здесь живут глубокие параметры ядра, планировщика, памяти, драйвера видеокарты,
сетевого стека и файловой системы. Многие из них не имеют интерфейса в самой
Windows и меняются только через реестр, powercfg, bcdedit или netsh.

Правила остаются теми же, что и в обычной оптимизации, и здесь они важнее:

* у КАЖДОГО твика есть обратная операция, возвращающая заводское поведение;
* состояние читается из системы, а не запоминается приложением, — если что-то
  изменили мимо нас, галочка это покажет;
* у опасных твиков заполнено поле warn: оно выводится красным прямо в строке;
* перед пакетным применением страница предлагает точку восстановления Windows.

Чего здесь намеренно нет: необратимых операций, удаления системных компонентов
и «разгона» напряжений. Всё, что нельзя вернуть галочкой, сюда не попадает.
"""
from __future__ import annotations

import re
import time
import winreg

from .optimizer import (
    HKCU, HKLM, Tweak, _reg_tweak, reg_delete, reg_read, reg_write,
)
from .util import log, run, run_ps

# Короткий кэш вывода внешних команд.
#
# Состояния читаются пачкой: страница «Эксперт» опрашивает пятьдесят шесть
# настроек подряд. Без кэша получалось девять запусков bcdedit и netsh — по
# секунде каждый, и страница открывалась девять с половиной секунд. Живёт
# кэш пару секунд: этого хватает на один проход и не хватает, чтобы показать
# устаревшее состояние после изменения.
_CACHE_TTL = 2.0
_CACHE: dict[tuple, tuple[float, tuple]] = {}


def _cached_run(args: tuple, timeout: int = 15) -> tuple[int, str, str]:
    """Запускает команду, но не чаще раза в пару секунд для одинаковых команд."""
    now = time.time()
    hit = _CACHE.get(args)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    result = run(list(args), timeout=timeout)
    _CACHE[args] = (now, result)
    return result


def invalidate_cache() -> None:
    """Сбрасывает кэш: вызывать после любого изменения настроек."""
    _CACHE.clear()

# ---------------------------------------------------------------- ветки реестра

_CONTROL = r"SYSTEM\CurrentControlSet\Control"
_MEMORY = _CONTROL + r"\Session Manager\Memory Management"
_KERNEL = _CONTROL + r"\Session Manager\kernel"
_PRIORITY = _CONTROL + r"\PriorityControl"
_GRAPHICS = _CONTROL + r"\GraphicsDrivers"
_FILESYSTEM = _CONTROL + r"\FileSystem"
_TCPIP = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
_MOUSE = r"SYSTEM\CurrentControlSet\Services\mouclass\Parameters"
_KEYBOARD = r"SYSTEM\CurrentControlSet\Services\kbdclass\Parameters"
_MM_PROFILE = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"


# ---------------------------------------------------------------- powercfg


_SCHEME_RE_SUBGROUP = re.compile(r"Subgroup GUID:\s*([0-9a-fA-F-]{36})")
_SCHEME_RE_SETTING = re.compile(r"Power Setting GUID:\s*([0-9a-fA-F-]{36})")
_SCHEME_RE_AC = re.compile(r"AC[^\n:]*:\s*(0x[0-9a-fA-F]+|\d+)")


def _read_power_scheme() -> dict[tuple[str, str], int]:
    """Все значения активной схемы электропитания одним запросом.

    Раньше на каждый параметр шёл свой powercfg — шесть запусков по полсекунды.
    Полный запрос схемы возвращает то же самое за десятые доли секунды, и его
    достаточно разобрать один раз.

    Подписи в выводе переведены на язык Windows, но GUID и слово AC — нет,
    поэтому разбор не зависит от локализации.
    """
    code, out, _err = _cached_run(("powercfg", "/query", "SCHEME_CURRENT"), timeout=30)
    values: dict[tuple[str, str], int] = {}
    if code != 0 or not out:
        return values

    subgroup = setting = ""
    for line in out.splitlines():
        match = _SCHEME_RE_SUBGROUP.search(line)
        if match:
            subgroup = match.group(1).lower()
            setting = ""
            continue
        match = _SCHEME_RE_SETTING.search(line)
        if match:
            setting = match.group(1).lower()
            continue
        if subgroup and setting:
            match = _SCHEME_RE_AC.search(line)
            if match:
                token = match.group(1)
                try:
                    values[(subgroup, setting)] = (
                        int(token, 16) if token.startswith("0x") else int(token))
                except ValueError:
                    pass
                setting = ""
    return values


def _powercfg_value(subgroup: str, setting: str) -> int | None:
    """Текущее значение параметра электропитания активной схемы (сеть 220 В)."""
    return _read_power_scheme().get((subgroup.lower(), setting.lower()))


def _powercfg_set(subgroup: str, setting: str, value: int) -> bool:
    invalidate_cache()
    code, _o, _e = run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                        subgroup, setting, str(value)], timeout=15)
    if code != 0:
        return False
    run(["powercfg", "/setactive", "SCHEME_CURRENT"], timeout=15)
    return True


_UNHIDDEN: set[tuple] = set()


def _powercfg_unhide(subgroup: str, setting: str) -> None:
    """Снимает флаг «скрытый» с параметра.

    Раньше это делалось при сборке каталога — шесть запусков powercfg по
    полсекунды на пустом месте, страница открывалась четыре секунды. Для
    чтения значения снимать флаг не нужно вовсе: он влияет только на то,
    видно ли параметр в панели управления Windows. Поэтому вызываем его
    перед изменением и запоминаем, что уже сняли.
    """
    key = (subgroup, setting)
    if key in _UNHIDDEN:
        return
    _UNHIDDEN.add(key)
    run(["powercfg", "-attributes", subgroup, setting, "-ATTRIB_HIDE"], timeout=15)


def _power_tweak(tid, title, desc, why, subgroup, setting, on_value, off_value,
                 **kw) -> Tweak:
    """Твик поверх параметра схемы электропитания."""
    def apply(value: int) -> bool:
        # снимаем «скрытый» только перед реальным изменением
        _powercfg_unhide(subgroup, setting)
        return _powercfg_set(subgroup, setting, value)

    return Tweak(
        id=tid, title=title, desc=desc, why=why, category=kw.pop("category", "Питание ядра"),
        is_on=lambda: _powercfg_value(subgroup, setting) == on_value,
        turn_on=lambda: apply(on_value),
        turn_off=lambda: apply(off_value),
        expert=True, **kw)


# GUID подгрупп и параметров powercfg (одинаковы во всех версиях Windows)
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
SUB_PCIE = "501a4d13-42af-4429-9fd1-a8218c268e20"
SUB_DISK = "0012ee47-9041-4b5d-9b77-535fba8b1442"
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"

SET_CORE_PARK_MIN = "0cc5b647-c1df-4637-891a-dec35c318583"
SET_CORE_PARK_MAX = "ea062031-0e34-4ff1-9b6d-eb1059334028"
SET_PERF_BOOST = "be337238-0d82-4146-a960-4f3749d470c7"
SET_IDLE_DISABLE = "5d76a2ca-e8c0-402f-a133-2158492d58ad"
SET_LATENCY_HINT = "0822df31-9c83-441c-a079-0de4cf009c7b"
SET_PCIE_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"
SET_DISK_TIMEOUT = "6738e2c4-e8a5-4a42-b16a-e040e769756e"
SET_HIBERNATE_AFTER = "9d7815a6-7ee4-497e-8888-515a05f02364"


# ---------------------------------------------------------------- bcdedit


def _bcd_value(name: str) -> str | None:
    """Значение параметра загрузчика для текущей записи."""
    code, out, _err = _cached_run(("bcdedit", "/enum", "{current}"))
    if code != 0 or not out:
        return None
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].strip().lower() == name.lower():
            return parts[1].strip()
    return None


def _bcd_set(name: str, value: str) -> bool:
    invalidate_cache()
    code, _o, err = run(["bcdedit", "/set", "{current}", name, value], timeout=15)
    if code != 0:
        log(f"bcdedit /set {name} {value}: {err}")
    return code == 0


def _bcd_delete(name: str) -> bool:
    invalidate_cache()
    code, _o, _e = run(["bcdedit", "/deletevalue", "{current}", name], timeout=15)
    return True                       # отсутствие значения — тоже норма


def _bcd_tweak(tid, title, desc, why, name, on_value, **kw) -> Tweak:
    return Tweak(
        id=tid, title=title, desc=desc, why=why,
        category=kw.pop("category", "Загрузка системы"), reboot=True,
        is_on=lambda: (_bcd_value(name) or "").lower() == on_value.lower(),
        turn_on=lambda: _bcd_set(name, on_value),
        turn_off=lambda: _bcd_delete(name),
        expert=True, **kw)


# ---------------------------------------------------------------- netsh


def _netsh_tcp_value(*keywords: str) -> str | None:
    """Значение параметра TCP по любому из ключевых слов подписи.

    netsh печатает человеческие подписи, а они переведены на язык Windows.
    Поэтому ищем по нескольким вариантам сразу — английскому и русскому.
    """
    code, out, _err = _cached_run(("netsh", "int", "tcp", "show", "global"))
    if code != 0 or not out:
        return None
    wanted = [k.lower() for k in keywords]
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        key = key.strip().lower()
        if any(k in key for k in wanted):
            return value.strip().lower()
    return None


_ON_WORDS = ("enabled", "включено", "включен")
_OFF_WORDS = ("disabled", "отключено", "отключен")


def _netsh_tcp_set(name: str, value: str) -> bool:
    invalidate_cache()
    code, _o, _e = run(["netsh", "int", "tcp", "set", "global", f"{name}={value}"],
                       timeout=20)
    return code == 0


# ---------------------------------------------------------------- MSI-режим GPU


def _gpu_device_keys() -> list[str]:
    """Ветки реестра видеоадаптеров в разделе PCI."""
    found: list[str] = []
    base = r"SYSTEM\CurrentControlSet\Enum\PCI"
    try:
        with winreg.OpenKey(HKLM, base, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as root:
            index = 0
            while True:
                try:
                    device = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, device) as dev_key:
                        sub = 0
                        while True:
                            try:
                                instance = winreg.EnumKey(dev_key, sub)
                            except OSError:
                                break
                            sub += 1
                            path = rf"{base}\{device}\{instance}"
                            cls = reg_read(HKLM, path, "ClassGUID")
                            # {4d36e968-e325-11ce-bfc1-08002be10318} — видеоадаптеры
                            if cls and "4d36e968" in str(cls).lower():
                                found.append(path)
                except OSError:
                    continue
    except OSError:
        pass
    return found


_MSI_SUFFIX = (r"\Device Parameters\Interrupt Management"
               r"\MessageSignaledInterruptProperties")


def _gpu_msi_state() -> bool:
    keys = _gpu_device_keys()
    if not keys:
        return False
    for path in keys:
        if reg_read(HKLM, path + _MSI_SUFFIX, "MSISupported") != 1:
            return False
    return True


def _gpu_msi_set(enable: bool) -> bool:
    keys = _gpu_device_keys()
    if not keys:
        log("MSI-режим: видеоадаптер в реестре не найден")
        return False
    ok = True
    for path in keys:
        if not reg_write(HKLM, path + _MSI_SUFFIX, "MSISupported",
                         1 if enable else 0):
            ok = False
    log(f"MSI-режим видеокарты: {'включён' if enable else 'выключен'} "
        f"на {len(keys)} устройствах")
    return ok


# ---------------------------------------------------------------- каталог


def build_expert_tweaks() -> list[Tweak]:
    """Полный каталог экспертных настроек."""
    t: list[Tweak] = []

    # ================================================ Планировщик и отклик ядра
    t.append(Tweak(
        id="x_prio_short", title="Кванты времени: короткие и переменные",
        desc="Win32PrioritySeparation = 0x26 — короткий квант, переменная длина, "
             "тройное усиление активному окну.",
        why="Активная игра получает процессор чаще и на более короткие отрезки. "
            "Отзывчивость растёт, но фоновые задачи считаются медленнее.",
        category="Планировщик и отклик", risk="средний", expert=True, reboot=False,
        is_on=lambda: reg_read(HKLM, _PRIORITY, "Win32PrioritySeparation") == 0x26,
        turn_on=lambda: reg_write(HKLM, _PRIORITY, "Win32PrioritySeparation", 0x26),
        turn_off=lambda: reg_write(HKLM, _PRIORITY, "Win32PrioritySeparation", 2)))

    t.append(_reg_tweak(
        "x_sys_resp_zero", "Резерв под фоновые задачи = 0%",
        "SystemResponsiveness = 0: планировщик мультимедиа перестаёт резервировать "
        "20% процессора под фоновые службы.",
        "Все ресурсы уходят активному приложению. На слабых машинах может дёргать "
        "звук в тяжёлых сценах — тогда верни 10.",
        "Планировщик и отклик", HKLM, _MM_PROFILE, "SystemResponsiveness", 0, 20,
        risk="средний", expert=True))

    t.append(_reg_tweak(
        "x_dpc_threaded", "Отложенные вызовы в отдельных потоках",
        "ThreadDpcEnable = 1 — драйверные DPC исполняются как потоки, а не блокируют ядро.",
        "Сглаживает микрорывки от драйверов сети и звука. На некоторых старых "
        "драйверах даёт обратный эффект — проверь в игре.",
        "Планировщик и отклик", HKLM, _KERNEL, "ThreadDpcEnable", 1, 0,
        risk="средний", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_dist_timers", "Распределённые таймеры ядра",
        "DistributeTimers = 1 — таймеры раскидываются по всем ядрам вместо нулевого.",
        "Снимает нагрузку с ядра 0, на котором обычно висят прерывания. "
        "Полезно на многоядерных процессорах.",
        "Планировщик и отклик", HKLM, _KERNEL, "DistributeTimers", 1, 0,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_serialize_off", "Не сериализовать доступ к таймеру",
        "SerializeTimerExpiration = 0 — истечение таймеров не выстраивается в очередь.",
        "Микроскопически снижает задержку планировщика. Эффект малозаметен, "
        "но в сумме с остальным работает.",
        "Планировщик и отклик", HKLM, _KERNEL, "SerializeTimerExpiration", 0, 1,
        risk="низкий", expert=True, reboot=True))

    # ================================================ Память ядра
    t.append(_reg_tweak(
        "x_no_paging_exec", "Держать ядро в оперативной памяти",
        "DisablePagingExecutive = 1 — код ядра и драйверов не выгружается в файл подкачки.",
        "Убирает подтормаживания, когда система вспоминает выгруженный драйвер. "
        "Имеет смысл от 16 ГБ памяти, на 8 ГБ лучше не трогать.",
        "Память ядра", HKLM, _MEMORY, "DisablePagingExecutive", 1, 0,
        risk="средний", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_large_cache", "Большой системный кэш",
        "LargeSystemCache = 1 — приоритет файловому кэшу перед памятью программ.",
        "Ускоряет работу с большими файлами и загрузку уровней. Играм с большим "
        "аппетитом к памяти может, наоборот, мешать — параметр спорный, поэтому "
        "он выключен по умолчанию.",
        "Память ядра", HKLM, _MEMORY, "LargeSystemCache", 1, 0,
        risk="средний", expert=True, reboot=True, recommended=False))

    t.append(_reg_tweak(
        "x_clear_pagefile", "Очищать файл подкачки при выключении",
        "ClearPageFileAtShutdown = 1 — содержимое подкачки затирается при выключении.",
        "Из файла подкачки нельзя будет достать куски документов и паролей, "
        "которые там осели. Выключение станет заметно дольше.",
        "Память ядра", HKLM, _MEMORY, "ClearPageFileAtShutdown", 1, 0,
        risk="низкий", expert=True, reboot=True, recommended=False))

    t.append(_reg_tweak(
        "x_prefetch_boot", "Предзагрузка только для запуска системы",
        "EnablePrefetcher = 2 — предсказание файлов работает для загрузки Windows, "
        "но не для каждой программы.",
        "На SSD прогнозирование запусков почти бесполезно и лишь пишет на диск. "
        "Загрузка системы при этом не страдает.",
        "Память ядра", HKLM,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters",
        "EnablePrefetcher", 2, 3, risk="низкий", expert=True, reboot=True))

    # ================================================ Видеокарта — глубоко
    t.append(Tweak(
        id="x_gpu_msi", title="MSI-режим прерываний видеокарты",
        desc="MSISupported = 1 для всех видеоадаптеров: карта шлёт прерывания "
             "сообщениями по шине вместо старой линии IRQ.",
        why="Классический приём против микрофризов: снимает конфликт прерываний "
            "с другими устройствами. Почти все современные карты это умеют, но "
            "Windows включает не всегда.",
        category="Видеокарта — глубоко", risk="средний", expert=True, reboot=True,
        warn="Если после перезагрузки картинка пропала — загрузись в безопасном "
             "режиме и выключи этот твик обратно.",
        is_on=_gpu_msi_state,
        turn_on=lambda: _gpu_msi_set(True),
        turn_off=lambda: _gpu_msi_set(False)))

    t.append(_reg_tweak(
        "x_tdr_delay", "Увеличенное время ожидания драйвера (TDR)",
        "TdrDelay = 10 секунд вместо стандартных двух.",
        "Windows перестанет сбрасывать драйвер при долгой отрисовке одного кадра. "
        "Спасает от вылета «драйвер перестал отвечать» при тяжёлых сценах и разгоне.",
        "Видеокарта — глубоко", HKLM, _GRAPHICS, "TdrDelay", 10, None,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_gpu_preempt", "Отключить вытеснение задач видеокарты",
        "DisablePreemption = 1 — длинные задачи GPU не прерываются другими.",
        "Убирает часть микрорывков в играх на DirectX 11. На DirectX 12 эффект "
        "спорный, а на слабых картах может дать обратное.",
        "Видеокарта — глубоко", HKLM, _GRAPHICS, "DisablePreemption", 1, 0,
        risk="средний", expert=True, reboot=True, recommended=False))

    t.append(_reg_tweak(
        "x_gpu_latency", "Терпимость к задержке монитора = минимум",
        "MonitorLatencyTolerance = 0 — видеотракт не уходит в энергосбережение "
        "между кадрами.",
        "Кадры перестают ждать пробуждения конвейера. Немного растёт потребление.",
        "Видеокарта — глубоко", HKLM, _GRAPHICS, "MonitorLatencyTolerance", 0, None,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_gpu_refresh_lat", "Терпимость к задержке обновления = минимум",
        "MonitorRefreshLatencyTolerance = 0 — то же самое для цикла обновления экрана.",
        "Работает в паре с предыдущим параметром.",
        "Видеокарта — глубоко", HKLM, _GRAPHICS, "MonitorRefreshLatencyTolerance", 0, None,
        risk="низкий", expert=True, reboot=True))

    # ================================================ Сеть — глубоко
    t.append(_reg_tweak(
        "x_net_ttl", "Время жизни пакета = 64",
        "DefaultTTL = 64 — стандарт для игровых серверов вместо виндовых 128.",
        "Пакет не крутится по маршрутам дольше нужного. Эффект небольшой, "
        "но это часть классической игровой настройки стека.",
        "Сеть — глубоко", HKLM, _TCPIP, "DefaultTTL", 64, None,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_net_ports", "Расширенный диапазон исходящих портов",
        "MaxUserPort = 65534 — Windows перестаёт упираться в 5000 соединений.",
        "Помогает торрентам, стриминговым программам и играм с большим числом "
        "соединений. Вреда нет.",
        "Сеть — глубоко", HKLM, _TCPIP, "MaxUserPort", 65534, None,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_net_timedwait", "Быстрое освобождение закрытых соединений",
        "TcpTimedWaitDelay = 30 секунд вместо 240.",
        "Порты возвращаются в оборот в восемь раз быстрее — меньше шансов "
        "упереться в лимит при активной сети.",
        "Сеть — глубоко", HKLM, _TCPIP, "TcpTimedWaitDelay", 30, None,
        risk="низкий", expert=True, reboot=True))

    t.append(Tweak(
        id="x_net_autotune", title="Автонастройка окна приёма TCP: обычная",
        desc="netsh int tcp set global autotuninglevel=normal — восстанавливает "
             "штатное масштабирование окна, если его кто-то отключил.",
        why="Многие «оптимизаторы» ставят disabled и режут скорость на быстрых "
            "каналах. Обычный режим — правильный для 99% подключений.",
        category="Сеть — глубоко", risk="низкий", expert=True,
        is_on=lambda: (_netsh_tcp_value("auto-tuning", "автонастройк", "автонастрой")
                       or "") in ("normal", "обычный", "нормальный"),
        turn_on=lambda: _netsh_tcp_set("autotuninglevel", "normal"),
        turn_off=lambda: _netsh_tcp_set("autotuninglevel", "disabled")))

    t.append(Tweak(
        id="x_net_rss", title="Масштабирование приёма по ядрам (RSS)",
        desc="netsh int tcp set global rss=enabled — обработка сетевых прерываний "
             "распределяется по ядрам процессора.",
        why="Одно ядро перестаёт быть узким местом на быстром канале. "
            "На современном железе включено должно быть всегда.",
        category="Сеть — глубоко", risk="низкий", expert=True,
        is_on=lambda: (_netsh_tcp_value("receive-side scaling", "масштабирован",
                                        "rss") or "") in _ON_WORDS,
        turn_on=lambda: _netsh_tcp_set("rss", "enabled"),
        turn_off=lambda: _netsh_tcp_set("rss", "disabled")))

    t.append(Tweak(
        id="x_net_ecn", title="Уведомление о заторе (ECN)",
        desc="netsh int tcp set global ecncapability=enabled — маршрутизатор "
             "сообщает о перегрузке, не отбрасывая пакет.",
        why="На хорошем канале снижает потери при загрузке. На старых или кривых "
            "маршрутизаторах, наоборот, ломает соединение — тогда выключи.",
        category="Сеть — глубоко", risk="средний", expert=True, recommended=False,
        is_on=lambda: (_netsh_tcp_value("ecn") or "") in _ON_WORDS,
        turn_on=lambda: _netsh_tcp_set("ecncapability", "enabled"),
        turn_off=lambda: _netsh_tcp_set("ecncapability", "disabled")))

    t.append(Tweak(
        id="x_net_rsc", title="Отключить объединение сегментов приёма (RSC)",
        desc="netsh int tcp set global rsc=disabled — сетевая карта перестаёт "
             "склеивать пакеты перед передачей в систему.",
        why="Склейка экономит процессор, но добавляет к каждому пакету немного "
            "задержки. В онлайн-играх ровный отклик важнее экономии.",
        category="Сеть — глубоко", risk="низкий", expert=True,
        is_on=lambda: (_netsh_tcp_value("segment coalescing", "объединени",
                                        "rsc") or "") in _OFF_WORDS,
        turn_on=lambda: _netsh_tcp_set("rsc", "disabled"),
        turn_off=lambda: _netsh_tcp_set("rsc", "enabled")))

    t.append(Tweak(
        id="x_net_rto", title="Быстрый повтор потерянного пакета",
        desc="netsh int tcp set global initialRto=300 — первое ожидание ответа "
             "сокращается с 1000 до 300 мс.",
        why="Потерянный пакет переспрашивается втрое быстрее — меньше залипаний "
            "в момент просадки канала. На плохом канале добавляет лишний трафик.",
        category="Сеть — глубоко", risk="средний", expert=True, recommended=False,
        is_on=lambda: (_netsh_tcp_value("initial rto", "начальное",
                                        "rto") or "") == "300",
        turn_on=lambda: _netsh_tcp_set("initialRto", "300"),
        turn_off=lambda: _netsh_tcp_set("initialRto", "1000")))

    # ================================================ Ввод — глубоко
    t.append(_reg_tweak(
        "x_mouse_queue", "Увеличенная очередь мыши",
        "MouseDataQueueSize = 50 вместо 100 — короче буфер, свежее данные.",
        "Указатель перестаёт «доезжать» после резкого движения на высоком DPI. "
        "Классическая настройка для игровых мышей с частотой 1000 Гц.",
        "Ввод — глубоко", HKLM, _MOUSE, "MouseDataQueueSize", 50, 100,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_kbd_queue", "Увеличенная очередь клавиатуры",
        "KeyboardDataQueueSize = 50.",
        "То же самое для клавиатуры: нажатия обрабатываются с более коротким буфером.",
        "Ввод — глубоко", HKLM, _KEYBOARD, "KeyboardDataQueueSize", 50, 100,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_kbd_delay", "Минимальная задержка автоповтора",
        "KeyboardDelay = 0 — символ начинает повторяться сразу после удержания.",
        "Заметно в чатах и консолях. К игровому вводу отношения не имеет, "
        "но многим просто удобнее.",
        "Ввод — глубоко", HKCU, r"Control Panel\Keyboard", "KeyboardDelay",
        "0", "1", vtype=winreg.REG_SZ, risk="низкий", expert=True))

    # ================================================ Файловая система
    t.append(_reg_tweak(
        "x_ntfs_8dot3", "Отключить короткие имена MS-DOS",
        "NtfsDisable8dot3NameCreation = 1 — NTFS перестаёт создавать вторые "
        "имена вида PROGRA~1 для каждого файла.",
        "Каждое создание файла становится дешевле. Заметно в папках с десятками "
        "тысяч файлов — например, в кэше шейдеров и библиотеках игр.",
        "Файловая система", HKLM, _FILESYSTEM, "NtfsDisable8dot3NameCreation", 1, 2,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_ntfs_memory", "Больше памяти под таблицу NTFS",
        "NtfsMemoryUsage = 2 — драйвер файловой системы получает расширенный кэш.",
        "Ускоряет работу с большим количеством файлов. Занимает немного "
        "дополнительной оперативной памяти.",
        "Файловая система", HKLM, _FILESYSTEM, "NtfsMemoryUsage", 2, 1,
        risk="низкий", expert=True, reboot=True))

    t.append(_reg_tweak(
        "x_ntfs_tunnel", "Отключить туннелирование имён",
        "MaximumTunnelEntries = 0 — NTFS не хранит имена только что удалённых файлов.",
        "Мелочь, ускоряющая массовые операции с файлами. Иногда ломает "
        "сохранение в очень старых программах — тогда верни обратно.",
        "Файловая система", HKLM, _FILESYSTEM, "MaximumTunnelEntries", 0, None,
        risk="средний", expert=True, reboot=True, recommended=False))

    # ================================================ Питание ядра (powercfg)
    t.append(_power_tweak(
        "x_park_off", "Полностью отключить парковку ядер",
        "Минимум активных ядер = 100%: планировщик перестаёт усыплять ядра.",
        "Ядра не нужно будить перед каждым всплеском нагрузки. Растёт "
        "потребление в простое на несколько ватт.",
        SUB_PROCESSOR, SET_CORE_PARK_MIN, 100, 5, risk="низкий"))

    t.append(_power_tweak(
        "x_boost_aggr", "Агрессивный режим ускорения процессора",
        "Режим Turbo Boost = «агрессивный» вместо «включён».",
        "Процессор быстрее и охотнее уходит в турбо. Греется сильнее — "
        "следи за температурой.",
        SUB_PROCESSOR, SET_PERF_BOOST, 2, 1, risk="средний"))

    t.append(_power_tweak(
        "x_idle_disable", "Запретить состояния простоя процессора",
        "Idle Disable = 1: ядра не уходят в C-состояния совсем.",
        "Убирает задержку выхода из сна ядра — это заметно в киберспортивных "
        "играх. Плата серьёзная: процессор всегда горячий и жрёт полную мощность.",
        SUB_PROCESSOR, SET_IDLE_DISABLE, 1, 0, risk="высокий", recommended=False,
        warn="Процессор перестанет отдыхать вообще: +10-20 °C в простое и "
             "заметный рост счёта за электричество. Включай только на время матча."))

    t.append(_power_tweak(
        "x_pcie_aspm_off", "Отключить энергосбережение шины PCI Express",
        "ASPM = Off: линия до видеокарты не уходит в пониженное состояние.",
        "Снимает микрозадержки при обращении к видеокарте после простоя. "
        "Стандартная рекомендация для игровых сборок.",
        SUB_PCIE, SET_PCIE_ASPM, 0, 2, risk="низкий"))

    t.append(_power_tweak(
        "x_disk_never_off", "Никогда не выключать жёсткие диски",
        "Тайм-аут отключения диска = 0 (никогда).",
        "Пропадают замирания на пару секунд при первом обращении к диску, "
        "который Windows успела усыпить.",
        SUB_DISK, SET_DISK_TIMEOUT, 0, 1200, risk="низкий"))

    # ================================================ Загрузка системы
    t.append(_bcd_tweak(
        "x_boot_nolog", "Не вести журнал загрузки",
        "bcdedit bootlog = No.",
        "Отключает запись подробного журнала при каждом старте. Пара "
        "сэкономленных операций записи, никакого риска.",
        "bootlog", "No", risk="низкий"))

    t.append(_bcd_tweak(
        "x_boot_quiet", "Тихая загрузка без анимации",
        "bcdedit quietboot = Yes.",
        "Экран загрузки не рисуется — система стартует чуть быстрее и "
        "переключается в рабочий режим раньше.",
        "quietboot", "Yes", risk="низкий", recommended=False))

    t.append(_bcd_tweak(
        "x_boot_dyntick", "Отключить динамический тик таймера",
        "bcdedit disabledynamictick = Yes — ядро перестаёт объединять таймеры "
        "ради экономии энергии.",
        "Прерывания идут строго по расписанию, а не пачками. Это одна из самых "
        "заметных настроек против микрорывков. Растёт потребление в простое.",
        "disabledynamictick", "Yes", risk="средний"))

    t.append(_bcd_tweak(
        "x_boot_hpet", "Принудительно использовать HPET как источник времени",
        "bcdedit useplatformclock = Yes — система берёт время только у "
        "высокоточного таймера платформы.",
        "Есть редкие сборки, где это лечит рассинхрон времени. На подавляющем "
        "большинстве современных машин параметр НАОБОРОТ снижает "
        "производительность — здесь он есть ради полноты и проверки.",
        "useplatformclock", "Yes", risk="высокий", recommended=False,
        warn="На большинстве компьютеров это ухудшает результат: замерь до и "
             "после в Кузнице и оставь только если стало лучше."))

    t.append(Tweak(
        id="x_boot_timeout", title="Мгновенный выбор системы при загрузке",
        desc="bcdedit timeout = 0 — меню выбора ОС не ждёт нажатия.",
        why="Если система на компьютере одна, ожидание бесполезно.",
        category="Загрузка системы", risk="низкий", expert=True, reboot=True,
        is_on=lambda: (_bcd_value("timeout") or "") == "0",
        turn_on=lambda: _bcd_set("timeout", "0"),
        turn_off=lambda: _bcd_set("timeout", "30")))

    # ================================================ Защита процессора (опасно)
    t.append(Tweak(
        id="x_mitigations_off", title="Отключить защиту от Spectre и Meltdown",
        desc="FeatureSettingsOverride = 3 — снимаются программные заплатки "
             "против уязвимостей спекулятивного исполнения.",
        why="Заплатки стоят от 5 до 15% скорости процессора, особенно на "
            "операциях с диском и в старых играх. Это самый крупный прирост "
            "среди всех твиков в этом списке.",
        category="Защита процессора", risk="высокий", expert=True, reboot=True,
        recommended=False,
        warn="Это снижает защиту компьютера. Уязвимости позволяют вредоносной "
             "программе читать чужую память. Осмысленно только на домашней "
             "игровой машине без банковских дел; на рабочем компьютере — нет.",
        is_on=lambda: reg_read(HKLM, _MEMORY, "FeatureSettingsOverride") == 3,
        turn_on=lambda: (reg_write(HKLM, _MEMORY, "FeatureSettingsOverride", 3)
                         and reg_write(HKLM, _MEMORY,
                                       "FeatureSettingsOverrideMask", 3)),
        turn_off=lambda: (reg_delete(HKLM, _MEMORY, "FeatureSettingsOverride")
                          and reg_delete(HKLM, _MEMORY,
                                         "FeatureSettingsOverrideMask"))))

    t.append(Tweak(
        id="x_vbs_off", title="Отключить изоляцию ядра (VBS)",
        desc="EnableVirtualizationBasedSecurity = 0 — выключается защита на "
             "основе виртуализации и целостность памяти.",
        why="VBS отнимает от 5 до 10% кадров в играх, потому что часть работы "
            "процессора идёт через гипервизор. На домашней машине это самая "
            "дорогая из включённых по умолчанию защит.",
        category="Защита процессора", risk="высокий", expert=True, reboot=True,
        recommended=False,
        warn="Снижает защиту от драйверов-вредителей. Некоторым античитам "
             "(Vanguard, Faceit) изоляция ядра нужна — с ней выключенной они "
             "могут не пустить в игру.",
        is_on=lambda: reg_read(
            HKLM, r"SYSTEM\CurrentControlSet\Control\DeviceGuard",
            "EnableVirtualizationBasedSecurity") == 0,
        turn_on=lambda: reg_write(
            HKLM, r"SYSTEM\CurrentControlSet\Control\DeviceGuard",
            "EnableVirtualizationBasedSecurity", 0),
        turn_off=lambda: reg_write(
            HKLM, r"SYSTEM\CurrentControlSet\Control\DeviceGuard",
            "EnableVirtualizationBasedSecurity", 1)))

    return t


def categories(tweaks: list[Tweak]) -> dict:
    """Группирует твики по категориям, сохраняя порядок появления."""
    groups: dict[str, list[Tweak]] = {}
    for tweak in tweaks:
        groups.setdefault(tweak.category, []).append(tweak)
    return groups
