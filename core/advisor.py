"""Советник: приложение само смотрит на компьютер и говорит, что делать.

Это не список проверок из «Диагностики» — там технический разбор системы.
Здесь другое: короткий человеческий ответ на вопрос «что мне сделать прямо
сейчас, чтобы стало лучше». Каждый совет знает, насколько он важен, почему
он появился и куда нажать.

Правило: если делать нечего — так и написать. Совет ради совета не выдумывается.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

import psutil

from .util import human_bytes, is_admin

# Важность совета: чем больше, тем выше в списке
CRITICAL = 3
IMPORTANT = 2
NICE = 1


@dataclass
class Advice:
    """Один совет."""
    key: str
    icon: str
    title: str
    text: str
    weight: int = NICE
    action_label: str = ""
    action_page: str = ""          # на какую страницу ведёт кнопка
    tone: str = "accent"           # accent / ok / warn / danger


def _gb(value_bytes: float) -> float:
    return value_bytes / (1024 ** 3)


def build_advice(ctx, reading: dict | None = None) -> list[Advice]:
    """Собирает советы под конкретную машину и её текущее состояние."""
    advice: list[Advice] = []
    machine = ctx.machine
    reading = reading or ctx.last_reading or {}

    # ---------------------------------------------------------- права
    if not is_admin():
        advice.append(Advice(
            "admin", "🔑", "Запусти с правами администратора",
            "Без них недоступны системные твики, файл подкачки, задачи "
            "планировщика, предел мощности видеокарты и вся страница «Эксперт». "
            "Это примерно половина возможностей приложения.",
            CRITICAL, "Перезапустить", "", "warn"))

    # ---------------------------------------------------------- температуры
    gpu_temp = reading.get("gpu_temp")
    if gpu_temp and gpu_temp >= 84:
        advice.append(Advice(
            "gpu_hot", "🌡", f"Видеокарта греется до {gpu_temp} °C",
            "На таких температурах карта начинает сама сбрасывать частоты — "
            "кадры проседают именно тогда, когда нагрузка максимальная. "
            "Профиль «Тихий» снизит предел мощности, а заодно и шум.",
            CRITICAL, "Открыть разгон", "overclock", "danger"))
    elif gpu_temp and gpu_temp >= 78:
        advice.append(Advice(
            "gpu_warm", "🌡", f"Видеокарта на {gpu_temp} °C",
            "Пока в норме, но запас до троттлинга небольшой. Стоит проверить "
            "пыль в радиаторе и кривую вентиляторов.",
            NICE, "Посмотреть", "overclock", "warn"))

    cpu_temp = reading.get("cpu_temp")
    if cpu_temp and cpu_temp >= 90:
        advice.append(Advice(
            "cpu_hot", "🔥", f"Процессор в {cpu_temp} °C",
            "Это уже предел: дальше он сбрасывает частоту. Обычно виноваты "
            "засохшая термопаста или забитый пылью кулер.",
            IMPORTANT, "Открыть разгон", "overclock", "danger"))

    # ---------------------------------------------------------- память
    ram_percent = reading.get("ram_percent") or 0
    if ram_percent >= 88:
        advice.append(Advice(
            "ram_full", "▤", f"Оперативка занята на {ram_percent:.0f}%",
            "Когда памяти не хватает, Windows начинает выгружать куски игры на "
            "диск — это ощущается как рывки на ровном месте. Очистка памяти "
            "и закрытие фона освободят пару гигабайт за секунды.",
            IMPORTANT, "Освободить память", "memory", "warn"))

    # ---------------------------------------------------------- место на диске
    try:
        usage = shutil.disk_usage("C:\\")
        free_gb = _gb(usage.free)
        if free_gb < 15:
            advice.append(Advice(
                "disk_full", "💽", f"На системном диске всего {free_gb:.0f} ГБ",
                "Windows нужно место под обновления и файл подкачки. Ниже 10 ГБ "
                "система начинает вести себя странно. Анализатор покажет, что "
                "занимает больше всего.",
                CRITICAL, "Найти, что занимает", "storage", "danger"))
        elif free_gb < 40:
            advice.append(Advice(
                "disk_low", "💽", f"Свободно {free_gb:.0f} ГБ на системном диске",
                "Запаса хватает, но новая игра его съест. Уборка и удаление "
                "лишних программ обычно возвращают десятки гигабайт.",
                NICE, "Прибраться", "cleanup", "warn"))
    except OSError:
        pass

    # ---------------------------------------------------------- видеопамять
    vram_total = reading.get("gpu_mem_total")
    vram_used = reading.get("gpu_mem_used")
    if vram_total and vram_used and vram_used / vram_total >= 0.9:
        advice.append(Advice(
            "vram_full", "🎮", "Видеопамять почти забита",
            f"Занято {vram_used} из {vram_total} МБ. Когда память кончается, "
            "текстуры подгружаются с диска прямо в бою — это самые заметные "
            "рывки. Снизь качество текстур на ступень: на кадры почти не влияет.",
            IMPORTANT, "Подобрать настройки", "games", "warn"))

    # ---------------------------------------------------------- железо
    if machine.ram_sticks == 1:
        advice.append(Advice(
            "single_stick", "🧩", "Память работает в один канал",
            "У тебя одна планка. Вторая такая же даёт до 20% кадров в играх, "
            "которые упираются в процессор. Это самое дешёвое ускорение, "
            "которое вообще существует, — дешевле любого разгона.",
            IMPORTANT, "Подробнее", "overclock", "warn"))

    if machine.ram_type == "DDR5" and machine.ram_speed and machine.ram_speed <= 4800:
        advice.append(Advice(
            "xmp", "⚡", "Профиль памяти XMP не включён",
            f"Память идёт на {machine.ram_speed} МГц — это заводской минимум. "
            "Один пункт в BIOS поднимет её до паспортной частоты: бесплатные "
            "5-15% кадров.",
            IMPORTANT, "Как включить", "overclock", "warn"))

    # ---------------------------------------------------------- счётчик кадров
    from .fps import rtss_installed
    if not rtss_installed():
        advice.append(Advice(
            "rtss", "📊", "Счётчик кадров пока молчит",
            "Оверлей умеет показывать реальный FPS и просадки по нижнему 1%, "
            "а умный авторазгон — опираться на них. Для этого нужен бесплатный "
            "RivaTuner Statistics Server. Без него эти функции спят.",
            NICE, "Что это", "overclock"))

    # ---------------------------------------------------------- автопилот
    if not ctx.config.get("autopilot"):
        advice.append(Advice(
            "autopilot", "🤖", "Автопилот выключен",
            "С ним не нужно ничего нажимать: приложение само заметит запуск "
            "игры, включит игровой профиль, а после выхода вернёт всё обратно.",
            NICE, "Включить", "settings"))

    # ---------------------------------------------------------- уборка
    try:
        from .cleanup import recycle_bin_size
        bin_mb = recycle_bin_size()
        if bin_mb > 2048:
            advice.append(Advice(
                "recycle", "🗑", f"В корзине {bin_mb / 1024:.1f} ГБ",
                "Файлы в корзине занимают место точно так же, как обычные. "
                "Проверь и очисти — это самое быстрое освобождение места.",
                NICE, "Прибраться", "cleanup"))
    except Exception:
        pass

    # ---------------------------------------------------------- экспертные твики
    expert_on = sum(1 for t in getattr(ctx, "expert_tweaks", []) if t.state())
    if is_admin() and expert_on == 0 and ctx.expert_tweaks:
        advice.append(Advice(
            "expert", "☢", "Экспертные настройки не тронуты",
            "Там 40 глубоких параметров ядра, драйвера и сети, которых нет в "
            "самой Windows. Начни с кнопки «Включить безопасные» — это только "
            "настройки с низким риском, все обратимы.",
            NICE, "Посмотреть", "expert"))

    # ---------------------------------------------------------- энергия
    wall = reading.get("wall_w")
    if wall and wall > 450:
        advice.append(Advice(
            "power", "🔌", f"Компьютер тянет {wall:.0f} Вт",
            "Под такой нагрузкой стоит убедиться, что блок питания с запасом. "
            "На странице энергии есть расчёт и рекомендация по мощности.",
            NICE, "Посмотреть расход", "power"))

    # ---------------------------------------------------------- фон
    try:
        heavy = sum(1 for p in psutil.process_iter(["cpu_percent"])
                    if (p.info.get("cpu_percent") or 0) > 5)
        if heavy >= 6:
            advice.append(Advice(
                "background", "🧠", "Фон заметно нагружает процессор",
                f"Сейчас {heavy} программ отъедают процессорное время. "
                "Умный баланс сгонит их на экономичные ядра, пока идёт игра, — "
                "ничего закрывать не придётся.",
                NICE, "Настроить", "processes"))
    except Exception:
        pass

    advice.sort(key=lambda a: -a.weight)
    return advice


def summary_line(advice: list[Advice]) -> str:
    """Короткая строка-итог для шапки карточки."""
    if not advice:
        return ("Замечаний нет. Компьютер настроен и ведёт себя ровно — "
                "делать прямо сейчас нечего.")
    critical = sum(1 for a in advice if a.weight == CRITICAL)
    important = sum(1 for a in advice if a.weight == IMPORTANT)
    parts = []
    if critical:
        parts.append(f"срочных {critical}")
    if important:
        parts.append(f"важных {important}")
    rest = len(advice) - critical - important
    if rest:
        parts.append(f"мелочей {rest}")
    return "Есть что улучшить: " + ", ".join(parts) + "."
