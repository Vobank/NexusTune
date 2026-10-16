"""Долговременная история показаний железа.

Живые графики показывают последнюю минуту — этого мало. Вопросы, на которые
хочется ответить, звучат иначе: «когда компьютер грелся сильнее всего?»,
«стало ли хуже после того, как я поменял настройки?», «сколько он на самом
деле ест под нагрузкой, а не в простое?».

Поэтому раз в минуту сюда падает сжатая запись: средние и максимальные
значения за минуту. Час занимает 60 строк, месяц — около 43 тысяч, это
считанные мегабайты. Хранится по дням в обычных JSON-файлах.

Записываются только показания датчиков — ни имён программ, ни чего-либо
личного.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import APP_DIR, log

HISTORY_DIR = APP_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

KEEP_DAYS = 60
WRITE_EVERY = 60.0            # секунд между записями


@dataclass
class Minute:
    """Сжатая запись за одну минуту."""
    stamp: float = 0.0
    cpu: float = 0.0
    cpu_max: float = 0.0
    cpu_temp: int = 0
    gpu: float = 0.0
    gpu_max: float = 0.0
    gpu_temp: int = 0
    gpu_temp_max: int = 0
    ram: float = 0.0
    watts: float = 0.0
    watts_max: float = 0.0
    samples: int = 0

    @property
    def when(self) -> str:
        return time.strftime("%d.%m %H:%M", time.localtime(self.stamp))

    def to_list(self) -> list:
        # компактный формат: список вместо словаря экономит место втрое
        return [round(self.stamp), round(self.cpu, 1), round(self.cpu_max, 1),
                self.cpu_temp, round(self.gpu, 1), round(self.gpu_max, 1),
                self.gpu_temp, self.gpu_temp_max, round(self.ram, 1),
                round(self.watts, 1), round(self.watts_max, 1), self.samples]

    @classmethod
    def from_list(cls, row: list) -> "Minute":
        def get(index, default=0):
            try:
                return row[index]
            except IndexError:
                return default
        return cls(stamp=get(0), cpu=get(1), cpu_max=get(2), cpu_temp=int(get(3)),
                   gpu=get(4), gpu_max=get(5), gpu_temp=int(get(6)),
                   gpu_temp_max=int(get(7)), ram=get(8), watts=get(9),
                   watts_max=get(10), samples=int(get(11)))


class Recorder:
    """Копит показания и раз в минуту сбрасывает сжатую запись на диск."""

    def __init__(self):
        self._reset()
        self._last_write = time.time()

    def _reset(self) -> None:
        self._cpu: list[float] = []
        self._gpu: list[float] = []
        self._ram: list[float] = []
        self._watts: list[float] = []
        self._cpu_temp: list[int] = []
        self._gpu_temp: list[int] = []

    def feed(self, reading: dict) -> None:
        """Принимает очередной снимок датчиков."""
        if reading.get("cpu_load") is not None:
            self._cpu.append(float(reading["cpu_load"]))
        if reading.get("gpu_load") is not None:
            self._gpu.append(float(reading["gpu_load"]))
        if reading.get("ram_percent") is not None:
            self._ram.append(float(reading["ram_percent"]))
        if reading.get("wall_w"):
            self._watts.append(float(reading["wall_w"]))
        if reading.get("cpu_temp"):
            self._cpu_temp.append(int(reading["cpu_temp"]))
        if reading.get("gpu_temp"):
            self._gpu_temp.append(int(reading["gpu_temp"]))

        if time.time() - self._last_write >= WRITE_EVERY:
            self.flush()

    def flush(self) -> Minute | None:
        """Сбрасывает накопленное в файл текущего дня."""
        self._last_write = time.time()
        if not self._cpu:
            return None

        def avg(values):
            return sum(values) / len(values) if values else 0.0

        minute = Minute(
            stamp=time.time(),
            cpu=round(avg(self._cpu), 1), cpu_max=max(self._cpu, default=0.0),
            cpu_temp=int(avg(self._cpu_temp)) if self._cpu_temp else 0,
            gpu=round(avg(self._gpu), 1), gpu_max=max(self._gpu, default=0.0),
            gpu_temp=int(avg(self._gpu_temp)) if self._gpu_temp else 0,
            gpu_temp_max=max(self._gpu_temp, default=0),
            ram=round(avg(self._ram), 1),
            watts=round(avg(self._watts), 1),
            watts_max=max(self._watts, default=0.0),
            samples=len(self._cpu))
        self._reset()
        _append(minute)
        return minute


def _day_file(stamp: float) -> Path:
    return HISTORY_DIR / (time.strftime("%Y-%m-%d", time.localtime(stamp)) + ".json")


def _append(minute: Minute) -> None:
    path = _day_file(minute.stamp)
    rows = []
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = []
    rows.append(minute.to_list())
    try:
        path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        log(f"История датчиков: не удалось записать — {exc}")
    _trim()


def _trim() -> None:
    files = sorted(HISTORY_DIR.glob("*.json"))
    for old in files[:-KEEP_DAYS]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- чтение


def load_range(days: int = 7) -> list[Minute]:
    """Записи за последние N дней, от старых к новым."""
    cutoff = time.time() - days * 86400
    minutes: list[Minute] = []
    for path in sorted(HISTORY_DIR.glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows:
            minute = Minute.from_list(row)
            if minute.stamp >= cutoff:
                minutes.append(minute)
    minutes.sort(key=lambda m: m.stamp)
    return minutes


@dataclass
class Summary:
    """Итог по промежутку времени."""
    minutes: int = 0
    hours: float = 0.0
    cpu_avg: float = 0.0
    cpu_peak: float = 0.0
    gpu_avg: float = 0.0
    gpu_peak: float = 0.0
    gpu_temp_peak: int = 0
    cpu_temp_peak: int = 0
    watts_avg: float = 0.0
    watts_peak: float = 0.0
    energy_kwh: float = 0.0
    busiest: Minute | None = None
    hottest: Minute | None = None
    notes: list = field(default_factory=list)


def summarize(minutes: list[Minute]) -> Summary:
    """Считает сводку и делает человеческие выводы."""
    summary = Summary(minutes=len(minutes))
    if not minutes:
        summary.notes.append(
            "История пока пуста. Она копится, пока приложение открыто: "
            "запись появляется раз в минуту.")
        return summary

    summary.hours = round(len(minutes) / 60, 1)
    loaded = [m for m in minutes if m.gpu_max >= 30 or m.cpu_max >= 50]

    def avg(values):
        return round(sum(values) / len(values), 1) if values else 0.0

    summary.cpu_avg = avg([m.cpu for m in minutes])
    summary.cpu_peak = round(max(m.cpu_max for m in minutes), 1)
    summary.gpu_avg = avg([m.gpu for m in minutes])
    summary.gpu_peak = round(max(m.gpu_max for m in minutes), 1)
    summary.gpu_temp_peak = max((m.gpu_temp_max for m in minutes), default=0)
    summary.cpu_temp_peak = max((m.cpu_temp for m in minutes), default=0)
    summary.watts_avg = avg([m.watts for m in minutes if m.watts])
    summary.watts_peak = round(max((m.watts_max for m in minutes), default=0.0), 1)
    # каждая запись покрывает минуту, поэтому ватт-минуты делим на 60
    summary.energy_kwh = round(
        sum(m.watts for m in minutes) / 60 / 1000, 3)

    summary.busiest = max(minutes, key=lambda m: m.gpu_max + m.cpu_max)
    if summary.gpu_temp_peak:
        summary.hottest = max(minutes, key=lambda m: m.gpu_temp_max)

    if loaded:
        share = round(len(loaded) / len(minutes) * 100)
        summary.notes.append(
            f"Под настоящей нагрузкой компьютер провёл {share}% времени "
            f"({round(len(loaded) / 60, 1)} ч из {summary.hours} ч наблюдения).")

    if summary.gpu_temp_peak >= 84:
        summary.notes.append(
            f"Максимум по видеокарте — {summary.gpu_temp_peak} °C "
            f"({summary.hottest.when}). На таких температурах карта начинает "
            "сама сбрасывать частоты.")
    elif summary.gpu_temp_peak:
        summary.notes.append(
            f"Самая горячая минута видеокарты — {summary.gpu_temp_peak} °C "
            f"({summary.hottest.when}). Это в пределах нормы.")

    if summary.watts_peak:
        summary.notes.append(
            f"Пик потребления — {summary.watts_peak:.0f} Вт, в среднем "
            f"{summary.watts_avg:.0f} Вт. За период набежало "
            f"{summary.energy_kwh} кВт·ч.")

    if summary.minutes < 60:
        summary.notes.append(
            "Наблюдений пока меньше часа — выводы предварительные.")
    return summary


def daily_breakdown(minutes: list[Minute]) -> list[dict]:
    """Разбивка по дням для графика."""
    days: dict[str, list[Minute]] = {}
    for minute in minutes:
        key = time.strftime("%d.%m", time.localtime(minute.stamp))
        days.setdefault(key, []).append(minute)

    result = []
    for day, rows in days.items():
        result.append({
            "day": day,
            "minutes": len(rows),
            "hours": round(len(rows) / 60, 1),
            "cpu": round(sum(m.cpu for m in rows) / len(rows), 1),
            "gpu": round(sum(m.gpu for m in rows) / len(rows), 1),
            "gpu_temp_peak": max((m.gpu_temp_max for m in rows), default=0),
            "watts_peak": round(max((m.watts_max for m in rows), default=0.0)),
            "kwh": round(sum(m.watts for m in rows) / 60 / 1000, 3),
        })
    return result
