"""Кузница — единственная в своём роде страница приложения.

Идея простая и честная: не верить обещаниям, а показать разницу замером.

Обычная «оптимизация» говорит «стало быстрее» и уходит. Кузница делает иначе.
Она снимает живые показатели ДО (мощность процессора и памяти, свободную ОЗУ,
кэш в списке ожидания, число фоновых процессов, отзывчивость системы), потом
на глазах перестраивает компьютер под выбранную задачу и снимает те же
показатели ПОСЛЕ. В конце выдаёт «паспорт ковки» — что именно изменилось,
в цифрах, которые можно проверить.

Замеры лёгкие и быстрые (секунды, не минуты) — это не полный тест
производительности со страницы диагностики, а быстрый срез состояния, чтобы
поймать именно эффект перестройки, а не гонять железо.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import psutil

from . import bench
from .util import log


@dataclass
class ForgeMetrics:
    """Быстрый срез состояния компьютера в один момент времени."""
    cpu_index: float = 0.0          # относительная мощность процессора, эталон 1000
    mem_index: float = 0.0          # относительная скорость памяти
    free_ram_gb: float = 0.0        # свободно оперативной памяти
    standby_gb: float = 0.0         # кэш в списке ожидания (можно освободить)
    processes: int = 0              # активных процессов
    background: int = 0             # из них фоновых (не системных и без окна)
    responsiveness: float = 0.0     # отклик системы, мс задержки таймера (меньше — лучше)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _responsiveness_probe(rounds: int = 30) -> float:
    """Задержка планировщика: просим поспать 1 мс и смотрим, насколько опоздали.

    На занятой или экономной системе Windows будит поток позже — это и есть
    ощущаемая «вязкость». На игровом профиле таймер точнее, задержка падает.
    """
    delays = []
    for _ in range(rounds):
        start = time.perf_counter()
        time.sleep(0.001)
        delays.append((time.perf_counter() - start) * 1000)
    delays.sort()
    # медиана устойчивее к случайным всплескам, чем среднее
    return round(delays[len(delays) // 2], 2)


def _standby_gb() -> float:
    """Объём списка ожидания — файловый кэш, который можно вернуть в свободную ОЗУ."""
    try:
        mem = psutil.virtual_memory()
        # cached ≈ standby + modified; на Windows psutil отдаёт его как .cached
        cached = getattr(mem, "cached", 0)
        return round(cached / (1024 ** 3), 2)
    except Exception:
        return 0.0


def _count_background() -> tuple[int, int]:
    """Всего процессов и сколько из них фоновых пользовательских."""
    total = 0
    background = 0
    for proc in psutil.process_iter(["name", "username"]):
        total += 1
        name = (proc.info.get("name") or "").lower()
        user = proc.info.get("username") or ""
        if not name or name in ("system idle process", "system", "registry"):
            continue
        # грубый признак фоновой пользовательской программы
        if user and "system" not in user.lower() and "service" not in user.lower():
            background += 1
    return total, background


def measure(quick: bool = True) -> ForgeMetrics:
    """Быстрый срез состояния. quick=False делает замеры чуть длиннее и точнее."""
    metrics = ForgeMetrics()
    single_seconds = 0.8 if quick else 1.6
    mem_seconds = 0.5 if quick else 1.0

    single = bench.bench_single_core(single_seconds)
    metrics.cpu_index = round(single / bench._REF_SINGLE * 1000, 1)

    mem = bench.bench_memory(mem_seconds)
    metrics.mem_index = round(mem / bench._REF_MEMORY * 1000, 1)

    vm = psutil.virtual_memory()
    metrics.free_ram_gb = round(vm.available / (1024 ** 3), 2)
    metrics.standby_gb = _standby_gb()
    metrics.processes, metrics.background = _count_background()
    metrics.responsiveness = _responsiveness_probe()
    return metrics


# ---------------------------------------------------------------- сравнение


@dataclass
class ForgeGain:
    """Одна строка паспорта: показатель до, после, насколько лучше."""
    key: str
    title: str
    before: str
    after: str
    delta_percent: float          # положительное — стало лучше
    better: bool
    unit_note: str = ""


def _pct(before: float, after: float, higher_better: bool) -> float:
    if before <= 0:
        return 0.0
    change = (after - before) / before * 100
    return round(change if higher_better else -change, 1)


def compare(before: ForgeMetrics, after: ForgeMetrics) -> list[ForgeGain]:
    """Строит паспорт ковки из двух срезов."""
    gains: list[ForgeGain] = []

    d = _pct(before.cpu_index, after.cpu_index, True)
    gains.append(ForgeGain("cpu", "Мощность процессора",
                           f"{before.cpu_index:.0f}", f"{after.cpu_index:.0f}",
                           d, d >= 0, "индекс, эталон 1000"))

    d = _pct(before.mem_index, after.mem_index, True)
    gains.append(ForgeGain("mem", "Скорость памяти",
                           f"{before.mem_index:.0f}", f"{after.mem_index:.0f}",
                           d, d >= 0, "индекс, эталон 1000"))

    d = _pct(before.free_ram_gb, after.free_ram_gb, True)
    gains.append(ForgeGain("ram", "Свободная оперативка",
                           f"{before.free_ram_gb:.1f} ГБ", f"{after.free_ram_gb:.1f} ГБ",
                           d, after.free_ram_gb >= before.free_ram_gb,
                           "больше — играм есть куда расти"))

    delta_bg = before.background - after.background
    gains.append(ForgeGain("bg", "Фоновых программ",
                           f"{before.background}", f"{after.background}",
                           float(delta_bg), delta_bg >= 0,
                           "меньше — меньше борьбы за ядра"))

    d = _pct(before.responsiveness, after.responsiveness, False)
    gains.append(ForgeGain("resp", "Отклик системы",
                           f"{before.responsiveness:.2f} мс", f"{after.responsiveness:.2f} мс",
                           d, after.responsiveness <= before.responsiveness,
                           "задержка таймера, меньше — резче"))
    return gains


def verdict(gains: list[ForgeGain]) -> str:
    """Короткий честный итог: не приукрашиваем, если разница в пределах погрешности."""
    cpu = next((g for g in gains if g.key == "cpu"), None)
    ram = next((g for g in gains if g.key == "ram"), None)
    bg = next((g for g in gains if g.key == "bg"), None)

    freed = 0.0
    if ram:
        try:
            freed = float(ram.after.split()[0]) - float(ram.before.split()[0])
        except ValueError:
            freed = 0.0

    big_cpu = cpu and cpu.delta_percent >= 4
    freed_ram = freed >= 0.5
    cut_bg = bg and bg.delta_percent >= 2

    if big_cpu or freed_ram or cut_bg:
        parts = []
        if big_cpu:
            parts.append(f"процессор отзывается на {cpu.delta_percent:.0f}% живее")
        if freed_ram:
            parts.append(f"освободилось {freed:.1f} ГБ памяти")
        if cut_bg:
            parts.append(f"фон стал легче на {int(bg.delta_percent)} процессов")
        return "Ковка удалась: " + ", ".join(parts) + "."
    return ("Разница в пределах погрешности — твой компьютер и так был близок "
            "к оптимуму для этой задачи. Это тоже честный результат: гнаться "
            "здесь больше не за чем.")


def passport_text(profile_title: str, gains: list[ForgeGain], applied: list[str]) -> str:
    """Текст «паспорта ковки», который можно скопировать или сохранить."""
    lines = [
        "ПАСПОРТ КОВКИ · КРЕМЕНЬ",
        "=" * 32,
        f"Профиль: {profile_title}",
        f"Дата: {time.strftime('%d.%m.%Y %H:%M')}",
        "",
        "Замерено до и после:",
    ]
    for g in gains:
        mark = "лучше" if g.better else "хуже"
        sign = "+" if g.delta_percent >= 0 else ""
        lines.append(f"  {g.title}: {g.before} -> {g.after} "
                     f"({sign}{g.delta_percent:.0f}%, {mark})")
    lines.append("")
    lines.append(verdict(gains))
    if applied:
        lines.append("")
        lines.append(f"Что сделано ({len(applied)} шагов):")
        for item in applied[:40]:
            lines.append(f"  - {item}")
    log("Кузница: паспорт сформирован")
    return "\n".join(lines)
