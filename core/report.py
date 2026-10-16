"""Полный отчёт о компьютере одним файлом.

Зачем: когда помогаешь другу разобраться с его машиной, первое, что нужно, —
понять, что у него вообще стоит. Здесь всё собирается в один читаемый файл:
железо, драйверы, накопители, сеть, состояние настроек, автозагрузка, тяжёлые
процессы и результаты замеров.

Отчёт намеренно текстовый: его можно отправить в мессенджер, вставить в
сообщение на форуме и прочитать без единой программы.

Что в отчёт НЕ попадает: имена файлов, названия документов, содержимое буфера
обмена, история браузера, ключи и пароли. Только состав железа и настройки
системы. В шапке об этом написано прямо, чтобы человек понимал, чем делится.
"""
from __future__ import annotations

import platform
import time
from pathlib import Path

import psutil

from .util import human_bytes, is_admin, log

_WIDTH = 74


def _rule(char: str = "─") -> str:
    return char * _WIDTH


def _head(title: str) -> str:
    return f"\n{_rule('━')}\n  {title.upper()}\n{_rule('━')}"


def _row(key: str, value) -> str:
    return f"  {key:<32} {value}"


def _safe(func, default=None):
    """Любая часть отчёта может не собраться — это не повод терять весь отчёт."""
    try:
        return func()
    except Exception as exc:
        log(f"Отчёт: раздел не собрался — {exc}")
        return default


def build_report(ctx, include_benchmarks: bool = True) -> str:
    """Собирает полный текстовый отчёт о системе."""
    lines: list[str] = []
    machine = ctx.machine

    # ---------------------------------------------------------- шапка
    lines.append(_rule("═"))
    lines.append("  ОТЧЁТ О КОМПЬЮТЕРЕ · КРЕМЕНЬ")
    lines.append(f"  Собран {time.strftime('%d.%m.%Y в %H:%M')}")
    lines.append(_rule("═"))
    lines.append("")
    lines.append("  В отчёте только состав железа и настройки системы.")
    lines.append("  Личных данных здесь нет: ни имён файлов, ни истории браузера,")
    lines.append("  ни паролей, ни содержимого документов.")

    # ---------------------------------------------------------- железо
    lines.append(_head("Железо"))
    lines.append(_row("Процессор", machine.cpu_name))
    lines.append(_row("Ядер / потоков", f"{machine.cores} / {machine.threads}"))
    lines.append(_row("Видеокарта", machine.gpu.name))
    if machine.gpu.vram_mb:
        lines.append(_row("Видеопамять", f"{machine.gpu.vram_mb} МБ"))
    lines.append(_row("Оперативная память",
                      f"{machine.ram_gb:.1f} ГБ {machine.ram_type} "
                      f"{machine.ram_speed} МГц, планок: {machine.ram_sticks}"))
    lines.append(_row("Разрешение экрана",
                      f"{machine.resolution} @ {machine.refresh} Гц"))
    if getattr(machine, "board", ""):
        lines.append(_row("Материнская плата", machine.board))
    lines.append(_row("Система", f"{machine.os_name}"))
    lines.append(_row("Сборка Windows", platform.version()))
    lines.append(_row("Права администратора", "да" if is_admin() else "нет"))

    topology = _safe(lambda: __import__("core.cpuinfo", fromlist=["read_topology"])
                     .read_topology())
    if topology and topology.cores:
        lines.append(_row("Топология ядер", topology.describe().split(".")[0] + "."))

    # ---------------------------------------------------------- датчики
    reading = ctx.last_reading or _safe(ctx.sensors.read, {}) or {}
    if reading:
        lines.append(_head("Датчики на момент сборки отчёта"))
        pairs = [
            ("Загрузка процессора", f"{reading.get('cpu_load', 0):.0f}%"),
            ("Температура процессора",
             f"{reading['cpu_temp']} °C" if reading.get("cpu_temp") else "нет датчика"),
            ("Загрузка видеокарты",
             f"{reading['gpu_load']}%" if reading.get("gpu_load") is not None else "нет данных"),
            ("Температура видеокарты",
             f"{reading['gpu_temp']} °C" if reading.get("gpu_temp") else "нет данных"),
            ("Видеопамять занято",
             f"{reading.get('gpu_mem_used')} / {reading.get('gpu_mem_total')} МБ"
             if reading.get("gpu_mem_total") else "нет данных"),
            ("Оперативная память",
             f"{reading.get('ram_used_gb', 0)} / {reading.get('ram_total_gb', 0)} ГБ"),
            ("Потребление из розетки",
             f"≈{reading['wall_w']:.0f} Вт" if reading.get("wall_w") else "не посчитано"),
        ]
        for key, value in pairs:
            lines.append(_row(key, value))
        sources = _safe(lambda: ", ".join(ctx.sensors.sources)) or "нет"
        lines.append(_row("Источники данных", sources))

    # ---------------------------------------------------------- накопители
    volumes = _safe(lambda: __import__("core.storage", fromlist=["volumes"]).volumes(), [])
    if volumes:
        lines.append(_head("Накопители"))
        for volume in volumes:
            letter = volume.get("device") or "?"
            total = volume.get("total_gb") or 0
            free = volume.get("free_gb") or 0
            percent = volume.get("percent") or 0
            lines.append(_row(f"Диск {letter} ({volume.get('fstype', '')})",
                              f"{free:.0f} свободно из {total:.0f} ГБ "
                              f"(занято {percent:.0f}%)"))

    drives = _safe(lambda: __import__("core.storage", fromlist=["read_drives"])
                   .read_drives(), [])
    for drive in drives or []:
        name = getattr(drive, "name", "") or "накопитель"
        temp = getattr(drive, "temperature", None)
        wear = getattr(drive, "wear_percent", None)
        hours = getattr(drive, "power_on_hours", None)
        parts = []
        if temp:
            parts.append(f"{temp} °C")
        if wear is not None:
            parts.append(f"износ {wear}%")
        if hours:
            parts.append(f"наработка {hours} ч")
        if parts:
            lines.append(_row(name[:32], ", ".join(parts)))

    # ---------------------------------------------------------- драйверы
    driver_list = _safe(lambda: __import__("core.drivers", fromlist=["read_drivers"])
                        .read_drivers(), [])
    if driver_list:
        lines.append(_head("Драйверы"))
        for driver in driver_list:
            verdict, _tone = driver.verdict
            lines.append(_row(f"{driver.group}: {driver.device[:26]}",
                              f"{driver.version} от {driver.date} ({verdict})"))

    # ---------------------------------------------------------- настройки Windows
    lines.append(_head("Настройки Windows"))
    on = [t for t in ctx.tweaks if _safe(t.state, False)]
    lines.append(_row("Обычных твиков включено", f"{len(on)} из {len(ctx.tweaks)}"))
    for tweak in on:
        lines.append(f"    + {tweak.title}")

    expert_on = [t for t in getattr(ctx, "expert_tweaks", []) if _safe(t.state, False)]
    lines.append(_row("Экспертных включено",
                      f"{len(expert_on)} из {len(getattr(ctx, 'expert_tweaks', []))}"))
    for tweak in expert_on:
        mark = " ⚠" if tweak.risk == "высокий" else ""
        lines.append(f"    + {tweak.title}{mark}")

    plan = _safe(lambda: __import__("core.optimizer", fromlist=["active_power_plan"])
                 .active_power_plan())
    if plan:
        lines.append(_row("План электропитания", plan[1]))

    # ---------------------------------------------------------- автозагрузка
    startup = _safe(lambda: __import__("core.cleanup", fromlist=["startup_items"])
                    .startup_items(), [])
    if startup:
        enabled = [s for s in startup if s.enabled]
        lines.append(_head("Автозагрузка"))
        lines.append(_row("Включено программ", f"{len(enabled)} из {len(startup)}"))
        for item in enabled:
            mark = "  (тяжёлая)" if item.heavy else ""
            lines.append(f"    · {item.name}{mark}")

    # ---------------------------------------------------------- процессы
    lines.append(_head("Самые прожорливые по памяти"))
    procs = _safe(_top_processes, [])
    for name, mem in procs or []:
        lines.append(_row(name[:32], human_bytes(mem)))

    # ---------------------------------------------------------- сеть
    adapters = _safe(lambda: __import__("core.network", fromlist=["active_adapter"])
                     .active_adapter())
    if adapters:
        lines.append(_head("Сеть"))
        lines.append(_row("Подключение", adapters.name))
        lines.append(_row("Тип", "Wi-Fi" if adapters.wifi else (adapters.kind or "кабель")))
        if adapters.speed:
            lines.append(_row("Скорость канала", adapters.speed))
        if adapters.driver_date:
            lines.append(_row("Дата драйвера адаптера", adapters.driver_date))

    # ---------------------------------------------------------- замеры
    if include_benchmarks:
        results = _safe(lambda: __import__("core.bench", fromlist=["load_results"])
                        .load_results(), [])
        if results:
            lines.append(_head("Замеры производительности"))
            for result in results[-5:]:
                lines.append(_row(result.when,
                                  f"{result.total_score} очков "
                                  f"(ЦП {result.single_score}/{result.multi_score}, "
                                  f"память {result.memory_score}, диск {result.disk_score})"))

    # ---------------------------------------------------------- загрузка
    boot = _safe(lambda: __import__("core.boottime", fromlist=["uptime_summary"])
                 .uptime_summary(), {})
    if boot and boot.get("last_boot"):
        lines.append(_head("Загрузка системы"))
        lines.append(_row("Последняя загрузка", boot["last_boot"]))
        lines.append(_row("Работает без перезагрузки", f"{boot['uptime_hours']} ч"))

    lines.append("")
    lines.append(_rule("═"))
    lines.append("  Отчёт собран приложением КРЕМЕНЬ")
    lines.append(_rule("═"))
    return "\n".join(lines)


def _top_processes(limit: int = 12) -> list[tuple]:
    """Самые прожорливые процессы по памяти.

    Загрузку процессора здесь не показываем намеренно: честно измерить её
    можно только двумя замерами с паузой, а отчёт и так собирается небыстро.
    Память же читается мгновенно и точно.
    """
    totals: dict[str, int] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        name = proc.info.get("name") or ""
        info = proc.info.get("memory_info")
        if not name or not info:
            continue
        if name.lower() in ("system idle process", "idle"):
            continue
        # браузеры и мессенджеры плодят десятки процессов — складываем их вместе
        totals[name] = totals.get(name, 0) + info.rss
    rows = sorted(totals.items(), key=lambda row: -row[1])
    return rows[:limit]


def save_report(ctx, path: Path) -> tuple[bool, str]:
    """Сохраняет отчёт в файл."""
    try:
        text = build_report(ctx)
        Path(path).write_text(text, encoding="utf-8")
        log(f"Отчёт о системе сохранён: {path}")
        return True, f"Отчёт сохранён: {path}"
    except OSError as exc:
        return False, f"Не удалось сохранить отчёт: {exc}"
