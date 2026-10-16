"""Диагностика компьютера: что мешает жить и как это починить одной кнопкой.

Каждая проверка объясняет, почему это важно, и по возможности предлагает
готовое действие. Проверки только читают систему — меняет что-то лишь
явно вызванная починка.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import psutil

from . import cleanup, memory, optimizer
from .context import AppContext
from .util import is_admin, log, ps_json, run_ps

OK = "ok"
WARN = "warn"
BAD = "bad"


@dataclass
class Check:
    """Результат одной проверки."""
    key: str
    title: str
    status: str                       # ok / warn / bad
    value: str                        # что показал замер
    why: str                          # почему это важно
    fix_label: str = ""               # подпись кнопки починки
    fix: Callable[[], tuple[bool, str]] | None = None
    weight: int = 1                   # вклад в общую оценку здоровья


# ---------------------------------------------------------------- отдельные проверки


def _check_ram_channels(ctx: AppContext) -> Check:
    machine = ctx.machine
    if machine.ram_sticks >= 2:
        return Check("ram_channels", "Режим работы памяти", OK,
                     f"{machine.ram_sticks} планки — двухканальный режим",
                     "Двухканальный режим удваивает пропускную способность памяти.")
    return Check("ram_channels", "Режим работы памяти", BAD,
                 "одна планка — одноканальный режим",
                 "Вторая такая же планка даёт до 20% кадров в играх. "
                 "Это самое дешёвое ускорение, какое вообще бывает.", weight=3)


def _check_xmp(ctx: AppContext) -> Check:
    machine = ctx.machine
    speed, kind = machine.ram_speed, machine.ram_type
    baseline = 4800 if kind == "DDR5" else (2133 if kind == "DDR4" else 1600)
    if not speed:
        return Check("xmp", "Профиль разгона памяти", WARN, "не удалось прочитать",
                     "Проверь в BIOS, включён ли профиль XMP или EXPO.")
    if speed <= baseline:
        return Check("xmp", "Профиль разгона памяти", BAD,
                     f"{speed} МГц — заводской минимум",
                     f"Память {kind} работает на минимальной частоте. Профиль XMP/EXPO "
                     "в BIOS поднимет её до паспортной — это бесплатные 5-15% кадров "
                     "в играх, упирающихся в процессор.", weight=3)
    return Check("xmp", "Профиль разгона памяти", OK, f"{speed} МГц",
                 "Профиль памяти активен, частота выше заводского минимума.")


def _check_disk_space(ctx: AppContext) -> Check:
    try:
        usage = psutil.disk_usage("C:\\")
    except OSError:
        return Check("disk_space", "Свободное место", WARN, "не удалось прочитать", "")
    free_gb = usage.free / (1024 ** 3)
    percent_free = 100 - usage.percent

    def fix():
        keys = [c["key"] for c in cleanup.scan_junk() if c["default"]]
        freed = cleanup.clean_categories(keys)
        return True, f"Освобождено {freed} МБ."

    if percent_free < 8:
        return Check("disk_space", "Свободное место на диске C:", BAD,
                     f"{free_gb:.0f} ГБ ({percent_free:.0f}%)",
                     "Меньше 10% свободного места — Windows начинает тормозить, "
                     "а SSD теряет скорость записи. Играм негде разворачивать "
                     "временные файлы.", "Убрать мусор", fix, weight=3)
    if percent_free < 15:
        return Check("disk_space", "Свободное место на диске C:", WARN,
                     f"{free_gb:.0f} ГБ ({percent_free:.0f}%)",
                     "Запас подходит к концу. SSD любит, когда свободно хотя бы 15%.",
                     "Убрать мусор", fix, weight=2)
    return Check("disk_space", "Свободное место на диске C:", OK,
                 f"{free_gb:.0f} ГБ свободно ({percent_free:.0f}%)",
                 "Места достаточно.")


def _check_junk(ctx: AppContext) -> Check:
    scan = cleanup.scan_junk()
    total_mb = sum(item["mb"] for item in scan)

    def fix():
        keys = [c["key"] for c in scan if c["default"]]
        freed = cleanup.clean_categories(keys)
        return True, f"Удалено {freed} МБ мусора."

    if total_mb > 5000:
        return Check("junk", "Мусорные файлы", BAD, f"{total_mb / 1024:.1f} ГБ",
                     "Временные файлы, старый кэш шейдеров и отчёты о сбоях. "
                     "Кэш шейдеров после обновления драйвера ещё и вызывает рывки "
                     "в играх.", "Убрать всё", fix, weight=2)
    if total_mb > 1500:
        return Check("junk", "Мусорные файлы", WARN, f"{total_mb:.0f} МБ",
                     "Накопился кэш и временные файлы. Ничего личного среди них нет.",
                     "Убрать", fix)
    return Check("junk", "Мусорные файлы", OK, f"{total_mb:.0f} МБ",
                 "Система чистая.")


def _check_startup(ctx: AppContext) -> Check:
    items = cleanup.startup_items()
    enabled = [i for i in items if i.enabled]
    heavy = [i for i in enabled if i.heavy]

    def fix():
        done = cleanup.trim_startup()
        return True, f"Отключено программ: {done}."

    if len(heavy) >= 4:
        return Check("startup", "Автозагрузка", BAD,
                     f"{len(enabled)} программ, из них {len(heavy)} тяжёлых",
                     "Каждая программа в автозагрузке отодвигает момент, когда "
                     "компьютером можно пользоваться, и держит память занятой. "
                     f"Лишними выглядят: {', '.join(i.name for i in heavy[:5])}.",
                     "Отключить лишнее", fix, weight=2)
    if len(enabled) > 8:
        return Check("startup", "Автозагрузка", WARN, f"{len(enabled)} программ",
                     "Многовато для быстрого старта. Посмотри список на странице «Уборка».",
                     "Отключить лишнее", fix)
    return Check("startup", "Автозагрузка", OK, f"{len(enabled)} программ",
                 "Автозагрузка в порядке.")


def _check_junk_tasks(ctx: AppContext) -> Check:
    states = cleanup.task_states()
    active = [t for t in cleanup.JUNK_TASKS if states.get(t.name) in ("Ready", "Running")]

    def fix():
        done, total = cleanup.set_junk_tasks(enabled=False)
        return done > 0, f"Усыплено задач: {done} из {total}."

    if len(active) >= 8:
        return Check("junk_tasks", "Мусорные задачи планировщика", WARN,
                     f"{len(active)} активных",
                     "Задачи телеметрии и сбора статистики просыпаются в самый "
                     "неподходящий момент и грузят диск. Ни одна из них не нужна "
                     "для работы Windows.", "Усыпить", fix, weight=2)
    if active:
        return Check("junk_tasks", "Мусорные задачи планировщика", OK,
                     f"{len(active)} активных",
                     "Большая часть мусорных задач уже усыплена.")
    return Check("junk_tasks", "Мусорные задачи планировщика", OK, "все усыплены",
                 "Планировщик не тревожит систему зря.")


def _check_power_plan(ctx: AppContext) -> Check:
    guid, name = optimizer.active_power_plan()
    good = guid.lower() in (optimizer.ULTIMATE_GUID, optimizer.HIGH_PERF_GUID)

    def fix():
        ok = optimizer.set_power_plan(optimizer.ensure_ultimate_plan())
        return ok, "План переключён на максимальную производительность." if ok else \
            "Не удалось переключить план."

    if good:
        return Check("power_plan", "План электропитания", OK, name,
                     "Процессор не сбрасывает частоты в простое.")
    return Check("power_plan", "План электропитания", WARN, name or "неизвестен",
                 "На сбалансированном плане процессор постоянно засыпает и просыпается — "
                 "это лишние микрозадержки в играх.", "Переключить", fix, weight=2)


def _check_pagefile(ctx: AppContext) -> Check:
    state = memory.read_pagefile()
    ram_gb = ctx.machine.ram_gb
    try:
        free_gb = psutil.disk_usage("C:\\").free / (1024 ** 3)
    except OSError:
        free_gb = 100
    initial, maximum, _why = memory.recommend_pagefile(ram_gb, free_gb)

    def fix():
        return memory.set_pagefile("C:", initial, maximum)

    if state.automatic:
        return Check("pagefile", "Файл подкачки", WARN, "управляется системой",
                     f"Windows расширяет файл прямо во время игры, и это заметно. "
                     f"Для {ram_gb:.0f} ГБ памяти лучше задать фиксированный "
                     f"{initial}-{maximum} МБ.", "Задать размер", fix)
    if not state.entries:
        return Check("pagefile", "Файл подкачки", BAD, "отключён",
                     "Без подкачки некоторые игры падают с ошибкой нехватки памяти, "
                     "даже когда её физически хватает.", "Включить", fix, weight=2)

    current = state.entries[0]
    if current["initial_mb"] > maximum * 1.6:
        return Check("pagefile", "Файл подкачки", WARN,
                     f"{current['initial_mb']} МБ — больше необходимого",
                     f"Файл занимает лишние гигабайты на диске без пользы. "
                     f"Для {ram_gb:.0f} ГБ памяти достаточно {initial}-{maximum} МБ.",
                     "Задать разумный размер", fix)
    return Check("pagefile", "Файл подкачки", OK, state.summary,
                 "Размер задан вручную — Windows не тратит время на расширение файла.")


def _check_ssd(ctx: AppContext) -> Check:
    rows = ps_json("Get-PhysicalDisk | Select-Object FriendlyName,HealthStatus,MediaType",
                   timeout=25)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        return Check("ssd", "Состояние накопителей", WARN, "не удалось прочитать",
                     "Windows не отдала данные о здоровье дисков.")
    bad = [r for r in rows if str(r.get("HealthStatus")) not in ("0", "Healthy")]
    names = ", ".join(str(r.get("FriendlyName", "")) for r in rows)
    if bad:
        return Check("ssd", "Состояние накопителей", BAD,
                     f"проблема: {bad[0].get('FriendlyName')}",
                     "Windows сообщает о проблеме с диском. Сделай резервную копию "
                     "важных файлов прямо сейчас.", weight=3)
    return Check("ssd", "Состояние накопителей", OK, names[:60],
                 "Все диски здоровы по данным Windows.")


def _check_system_disk(ctx: AppContext) -> Check:
    if ctx.machine.system_disk_ssd:
        return Check("system_disk", "Системный диск", OK, "SSD",
                     "Windows и игры стоят на быстром носителе.")
    return Check("system_disk", "Системный диск", BAD, "жёсткий диск",
                 "Windows на HDD — главный тормоз современного компьютера. "
                 "Любой SSD ускорит систему сильнее, чем разгон процессора.", weight=3)


def _check_gpu_driver(ctx: AppContext) -> Check:
    value = ps_json(
        "Get-CimInstance Win32_PnPSignedDriver | "
        "Where-Object { $_.DeviceClass -eq 'DISPLAY' } | "
        "Select-Object -First 1 -ExpandProperty DriverDate", timeout=30)
    if not value:
        return Check("gpu_driver", "Драйвер видеокарты", OK,
                     ctx.machine.gpu.driver or "неизвестно",
                     "Дату выпуска драйвера прочитать не удалось.")
    text = str(value)
    stamp = None
    # WMI отдаёт дату либо как строку CIM, либо как /Date(миллисекунды)/
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        if "Date(" in text and len(digits) >= 12:
            stamp = int(digits[:13]) / 1000
        elif len(digits) >= 8:
            stamp = time.mktime(time.strptime(digits[:8], "%Y%m%d"))
    except (ValueError, OverflowError):
        stamp = None
    if not stamp:
        return Check("gpu_driver", "Драйвер видеокарты", OK,
                     ctx.machine.gpu.driver or "неизвестно", "Дату разобрать не удалось.")

    months = (time.time() - stamp) / (30 * 24 * 3600)
    when = time.strftime("%m.%Y", time.localtime(stamp))
    if months > 12:
        return Check("gpu_driver", "Драйвер видеокарты", WARN,
                     f"версия {ctx.machine.gpu.driver}, от {when}",
                     f"Драйверу больше года. Новые версии часто дают 5-15% кадров "
                     "в свежих играх и чинят вылеты.", weight=2)
    if months > 6:
        return Check("gpu_driver", "Драйвер видеокарты", WARN,
                     f"версия {ctx.machine.gpu.driver}, от {when}",
                     "Драйверу больше полугода — стоит обновиться перед новой игрой.")
    return Check("gpu_driver", "Драйвер видеокарты", OK,
                 f"версия {ctx.machine.gpu.driver}, от {when}", "Драйвер свежий.")


def _check_defender_exclusions(ctx: AppContext) -> Check:
    from .games import scan_all

    def fix():
        if not is_admin():
            return False, "Нужны права администратора."
        games = scan_all()
        folders = sorted({g.install_dir for g in games if g.install_dir})[:12]
        if not folders:
            return False, "Папки игр не найдены."
        added = 0
        for folder in folders:
            safe = folder.replace("'", "''")
            code, _out, _err = run_ps(
                f"Add-MpPreference -ExclusionPath '{safe}' -ErrorAction SilentlyContinue",
                timeout=30)
            if code == 0:
                added += 1
        return added > 0, f"Папок игр добавлено в исключения антивируса: {added}."

    current = ps_json("(Get-MpPreference).ExclusionPath", timeout=30)
    count = len(current) if isinstance(current, list) else (1 if current else 0)
    if count:
        return Check("defender", "Исключения антивируса", OK,
                     f"{count} папок в исключениях",
                     "Защитник не проверяет файлы игр при каждом чтении.")
    return Check("defender", "Исключения антивируса", WARN, "исключений нет",
                 "Защитник Windows проверяет каждый файл, который читает игра. "
                 "На тяжёлых играх это заметные подтормаживания при подгрузке. "
                 "Папки игр можно исключить — на безопасность системы это почти "
                 "не влияет, лаунчеры и так проверяют целостность файлов.",
                 "Добавить папки игр", fix, weight=2)


def _check_temperature(ctx: AppContext) -> Check:
    reading = ctx.last_reading or {}
    gpu_temp = reading.get("gpu_temp")
    cpu_temp = reading.get("cpu_temp")
    hottest = max([t for t in (gpu_temp, cpu_temp) if t] or [0])
    if not hottest:
        return Check("temps", "Температуры", OK, "нет данных",
                     "Запусти LibreHardwareMonitor, чтобы видеть температуру процессора.")
    if hottest >= 85:
        return Check("temps", "Температуры", BAD, f"до {hottest} °C",
                     "Железо перегревается и сбрасывает частоты. Проверь пыль в "
                     "радиаторах и обороты вентиляторов.", weight=2)
    if hottest >= 75:
        return Check("temps", "Температуры", WARN, f"до {hottest} °C",
                     "Тепловато для простоя. Под нагрузкой может дойти до сброса частот.")
    return Check("temps", "Температуры", OK, f"до {hottest} °C", "Всё в порядке.")


def _check_memory_pressure(ctx: AppContext) -> Check:
    vm = psutil.virtual_memory()

    def fix():
        result = memory.clean_memory()
        return True, f"Освобождено {result['freed_mb']} МБ."

    if vm.percent > 80:
        return Check("memory", "Занятость памяти", WARN, f"{vm.percent:.0f}%",
                     "Свободной памяти мало — игре придётся выгружать данные на диск. "
                     "Помогает очистка и закрытие фоновых программ.",
                     "Очистить память", fix, weight=2)
    return Check("memory", "Занятость памяти", OK, f"{vm.percent:.0f}%",
                 "Памяти хватает с запасом.")


def _check_hags(ctx: AppContext) -> Check:
    tweak = ctx.tweak("hags")
    if not tweak:
        return Check("hags", "Аппаратное планирование GPU", OK, "недоступно", "")

    def fix():
        ok = tweak.apply(True)
        return ok, ("Включено. Заработает после перезагрузки." if ok
                    else "Не удалось включить — нужны права администратора.")

    if tweak.state():
        return Check("hags", "Аппаратное планирование GPU", OK, "включено",
                     "Видеокарта сама управляет очередью задач — меньше задержка ввода.")
    return Check("hags", "Аппаратное планирование GPU", WARN, "выключено",
                 "Функция снижает задержку ввода и сглаживает кадр в играх на DirectX 12.",
                 "Включить", fix)


def _check_game_dvr(ctx: AppContext) -> Check:
    tweak = ctx.tweak("game_dvr")
    if not tweak:
        return Check("game_dvr", "Фоновая запись игр", OK, "недоступно", "")

    def fix():
        ok = tweak.apply(True)
        return ok, "Фоновая запись выключена." if ok else "Не удалось выключить."

    if tweak.state():
        return Check("game_dvr", "Фоновая запись игр", OK, "выключена",
                     "Windows не пишет видео в фоне.")
    return Check("game_dvr", "Фоновая запись игр", WARN, "включена",
                 "Game DVR постоянно пишет последние минуты игры и съедает 3-8% кадров.",
                 "Выключить", fix, weight=2)


_CHECKS = [
    _check_system_disk, _check_ram_channels, _check_xmp, _check_disk_space,
    _check_junk, _check_startup, _check_junk_tasks, _check_power_plan,
    _check_pagefile, _check_ssd, _check_gpu_driver, _check_defender_exclusions,
    _check_temperature, _check_memory_pressure, _check_hags, _check_game_dvr,
]


def run_checks(ctx: AppContext, progress=None) -> list[Check]:
    """Прогоняет всю диагностику."""
    results: list[Check] = []
    for index, check_fn in enumerate(_CHECKS, 1):
        if progress:
            progress(index, len(_CHECKS))
        try:
            results.append(check_fn(ctx))
        except Exception as exc:
            log(f"Проверка {check_fn.__name__} упала: {exc}")
    order = {BAD: 0, WARN: 1, OK: 2}
    results.sort(key=lambda c: (order.get(c.status, 3), -c.weight))
    return results


def health_score(checks: list[Check]) -> tuple[int, str]:
    """Общая оценка здоровья системы из 100."""
    if not checks:
        return 0, "нет данных"
    total_weight = sum(c.weight for c in checks)
    earned = sum(c.weight * {OK: 1.0, WARN: 0.5, BAD: 0.0}[c.status] for c in checks)
    score = int(earned / total_weight * 100) if total_weight else 0
    if score >= 90:
        label = "Отличное состояние"
    elif score >= 75:
        label = "Хорошее состояние"
    elif score >= 55:
        label = "Есть что починить"
    else:
        label = "Требует внимания"
    return score, label
