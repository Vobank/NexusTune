"""Энергопотребление компьютера: оценка мощности, пики, расход и деньги.

Честно о точности: розеточного ваттметра у приложения нет. Мощность видеокарты
берётся с датчика драйвера (это измерение), мощность процессора — с датчика
LibreHardwareMonitor, если он запущен, иначе считается по модели нагрузки.
Остальное железо оценивается по конфигурации. Каждая цифра помечена: измерена
она или посчитана.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .hwinfo import Machine
from .util import APP_DIR, log

HISTORY_FILE = APP_DIR / "power_history.json"


# ---------------------------------------------------------------- модель процессора


@dataclass
class CpuPowerProfile:
    idle_w: float = 12.0
    sustained_w: float = 65.0      # длительный предел (PL1)
    peak_w: float = 120.0          # кратковременный турбо-предел (PL2)
    source: str = "оценка по модели"


# Известные семейства: (регулярка, простой, PL1, PL2)
_CPU_POWER_TABLE = [
    (r"i9-14\d{3}k|i9-13\d{3}k", 18, 125, 253),
    (r"i7-14\d{3}k|i7-13\d{3}k", 16, 125, 253),
    (r"i5-1[34]\d{3}k", 14, 125, 181),
    (r"i5-13400|i5-12400|i5-14400", 10, 65, 148),
    (r"i9-1[12]\d{3}k", 18, 125, 241),
    (r"i7-1[12]\d{3}k", 16, 125, 190),
    (r"i9-\d{4,5}", 16, 95, 180),
    (r"i7-\d{4,5}", 14, 80, 150),
    (r"i5-\d{4,5}", 10, 65, 120),
    (r"i3-\d{4,5}", 8, 58, 90),
    (r"ryzen\s*9\s*7950|ryzen\s*9\s*9950", 22, 170, 230),
    (r"ryzen\s*9\s*[579]900", 20, 105, 160),
    (r"ryzen\s*9", 20, 105, 160),
    (r"ryzen\s*7\s*\d*x3d", 15, 120, 162),
    (r"ryzen\s*7", 14, 105, 142),
    (r"ryzen\s*5", 12, 65, 105),
    (r"ryzen\s*3", 10, 65, 88),
    (r"pentium|celeron|atom", 6, 35, 58),
]


def cpu_power_profile(machine: Machine) -> CpuPowerProfile:
    """Оценка энергетического профиля процессора по его модели."""
    low = machine.cpu_name.lower()
    for pattern, idle, pl1, pl2 in _CPU_POWER_TABLE:
        if re.search(pattern, low):
            return CpuPowerProfile(float(idle), float(pl1), float(pl2))
    # неизвестный процессор: считаем по числу ядер
    pl1 = max(35.0, min(machine.cores * 8.0, 150.0))
    return CpuPowerProfile(max(8.0, machine.cores * 1.2), pl1, pl1 * 1.8)


# ---------------------------------------------------------------- модель видеокарты

# Штатное потребление по классу карты, если драйвер не отдаёт датчик
_GPU_TDP_BY_TIER = {1: 75, 2: 120, 3: 170, 4: 220, 5: 320}


def gpu_tdp_estimate(machine: Machine) -> float:
    return float(_GPU_TDP_BY_TIER.get(machine.gpu.tier, 180))


# ---------------------------------------------------------------- прочее железо


def base_system_watts(machine: Machine) -> float:
    """Материнская плата, память, накопители, вентиляторы, USB."""
    watts = 28.0                                   # плата с чипсетом, сеть, звук
    watts += max(machine.ram_sticks, 1) * 3.0      # DDR5 греется сильнее DDR4
    for disk in machine.disks or []:
        watts += 2.5 if "SSD" in disk.get("type", "") else 6.5
    watts += 8.0                                   # корпусные вентиляторы и кулер
    watts += 4.0                                   # периферия по USB
    return watts


PSU_EFFICIENCY = 0.88                              # типичный блок 80 PLUS Gold


# ---------------------------------------------------------------- снимок мощности


@dataclass
class PowerSample:
    gpu_w: float = 0.0
    cpu_w: float = 0.0
    other_w: float = 0.0
    total_w: float = 0.0           # потребление компонентов
    wall_w: float = 0.0            # из розетки, с учётом КПД блока питания
    gpu_measured: bool = False
    cpu_measured: bool = False
    timestamp: float = field(default_factory=time.time)


class PowerMeter:
    """Считает мощность по показаниям датчиков и ведёт статистику сессии."""

    def __init__(self, machine: Machine):
        self.machine = machine
        self.cpu_profile = cpu_power_profile(machine)
        self.base_w = base_system_watts(machine)
        self.gpu_tdp = gpu_tdp_estimate(machine)

        self.samples: list[float] = []
        self.peak_w = 0.0
        self.peak_at: float = 0.0
        self.min_w = 0.0
        self.started = time.time()
        self.energy_wh = 0.0                       # накопленная энергия за сессию
        self._last_time = time.time()
        self.last: PowerSample | None = None
        self.peak_breakdown: PowerSample | None = None

    # ------------------------------------------------------------ расчёт

    def _cpu_watts(self, reading: dict) -> tuple[float, bool]:
        """Мощность процессора: с датчика или по модели нагрузки."""
        measured = reading.get("cpu_power")
        if measured:
            return float(measured), True
        load = (reading.get("cpu_load") or 0) / 100.0
        prof = self.cpu_profile
        # короткие всплески уходят в турбо-предел, длительная нагрузка живёт около PL1
        ceiling = prof.sustained_w + (prof.peak_w - prof.sustained_w) * min(load * 1.4, 1.0) * 0.5
        watts = prof.idle_w + (ceiling - prof.idle_w) * (load ** 0.85)
        return round(watts, 1), False

    def _gpu_watts(self, reading: dict) -> tuple[float, bool]:
        measured = reading.get("gpu_power")
        if measured:
            return float(measured), True
        load = reading.get("gpu_load")
        if load is None:
            return 0.0, False
        idle = self.gpu_tdp * 0.09
        watts = idle + (self.gpu_tdp - idle) * ((load / 100.0) ** 0.9)
        return round(watts, 1), False

    def update(self, reading: dict) -> PowerSample:
        """Принимает показания датчиков и возвращает разложение по мощности."""
        gpu_w, gpu_measured = self._gpu_watts(reading)
        cpu_w, cpu_measured = self._cpu_watts(reading)

        other = self.base_w
        # диск под нагрузкой ест заметно больше, чем в простое
        other += (reading.get("disk_percent") or 0) / 100.0 * 3.0

        total = gpu_w + cpu_w + other
        sample = PowerSample(
            gpu_w=round(gpu_w, 1), cpu_w=round(cpu_w, 1), other_w=round(other, 1),
            total_w=round(total, 1), wall_w=round(total / PSU_EFFICIENCY, 1),
            gpu_measured=gpu_measured, cpu_measured=cpu_measured,
        )

        now = time.time()
        elapsed_h = max(0.0, min(now - self._last_time, 60)) / 3600.0
        self._last_time = now
        self.energy_wh += sample.wall_w * elapsed_h

        self.samples.append(sample.wall_w)
        if len(self.samples) > 5000:
            self.samples = self.samples[-3000:]
        if sample.wall_w > self.peak_w:
            self.peak_w = sample.wall_w
            self.peak_at = now
            self.peak_breakdown = sample
        if self.min_w == 0.0 or sample.wall_w < self.min_w:
            self.min_w = sample.wall_w

        self.last = sample
        return sample

    # ------------------------------------------------------------ статистика

    @property
    def average_w(self) -> float:
        return round(sum(self.samples) / len(self.samples), 1) if self.samples else 0.0

    @property
    def session_hours(self) -> float:
        return (time.time() - self.started) / 3600.0

    def cost(self, tariff: float) -> float:
        """Стоимость потреблённой за сессию энергии."""
        return self.energy_wh / 1000.0 * tariff

    def projected_month(self, hours_per_day: float, tariff: float) -> tuple[float, float]:
        """Прогноз на месяц: (кВт·ч, деньги) при текущем среднем потреблении."""
        kwh = self.average_w * hours_per_day * 30 / 1000.0
        return round(kwh, 1), round(kwh * tariff, 2)

    def reset_peak(self) -> None:
        self.peak_w = 0.0
        self.peak_breakdown = None
        self.min_w = 0.0
        self.samples.clear()

    # ------------------------------------------------------------ рекомендации

    def psu_advice(self) -> tuple[int, str]:
        """Рекомендуемая мощность блока питания и пояснение."""
        prof = self.cpu_profile
        worst = self.gpu_tdp + prof.peak_w + self.base_w
        # запас на переходные всплески видеокарты и старение блока
        recommended = int(round((worst * 1.35) / 50.0) * 50)
        text = (
            f"В самом тяжёлом сценарии железо запросит около {worst:.0f} Вт: "
            f"видеокарта до {self.gpu_tdp:.0f} Вт, процессор в турбо до {prof.peak_w:.0f} Вт, "
            f"остальное {self.base_w:.0f} Вт. С запасом на кратковременные всплески "
            f"видеокарты и старение конденсаторов нужен блок от {recommended} Вт.")
        return recommended, text

    def verdict(self) -> tuple[str, str]:
        """Словесная оценка текущего потребления."""
        if not self.last:
            return "нет данных", "Датчики ещё не опрошены."
        wall = self.last.wall_w
        idle_estimate = self.base_w + self.cpu_profile.idle_w + self.gpu_tdp * 0.09
        if wall < idle_estimate / PSU_EFFICIENCY * 1.25:
            return "простой", "Компьютер отдыхает — это фоновое потребление."
        if wall < 250:
            return "лёгкая нагрузка", "Браузер, музыка, лёгкие приложения."
        if wall < 400:
            return "игра или работа", "Обычный игровой режим."
        return "пиковая нагрузка", "Железо работает почти на пределе."


# ---------------------------------------------------------------- история по дням


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"days": {}, "all_time_peak": 0.0}


def save_session(meter: PowerMeter) -> dict:
    """Дописывает итоги сессии в историю по дням."""
    history = load_history()
    day = time.strftime("%Y-%m-%d")
    entry = history["days"].setdefault(day, {"kwh": 0.0, "hours": 0.0, "peak": 0.0})
    entry["kwh"] = round(entry["kwh"] + meter.energy_wh / 1000.0, 4)
    entry["hours"] = round(entry["hours"] + meter.session_hours, 2)
    entry["peak"] = round(max(entry["peak"], meter.peak_w), 1)
    history["all_time_peak"] = round(max(history.get("all_time_peak", 0), meter.peak_w), 1)

    # держим историю за последние 90 дней
    days = sorted(history["days"].keys())
    for old in days[:-90]:
        history["days"].pop(old, None)

    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить историю потребления: {exc}")
    return history


def history_summary(tariff: float) -> dict:
    """Сводка за неделю и месяц по сохранённой истории."""
    history = load_history()
    days = history.get("days", {})
    today = time.strftime("%Y-%m-%d")

    def total(count: int) -> tuple[float, float]:
        keys = sorted(days.keys())[-count:]
        kwh = sum(days[k]["kwh"] for k in keys)
        hours = sum(days[k]["hours"] for k in keys)
        return round(kwh, 2), round(hours, 1)

    week_kwh, week_hours = total(7)
    month_kwh, month_hours = total(30)
    return {
        "today_kwh": round(days.get(today, {}).get("kwh", 0.0), 3),
        "week_kwh": week_kwh, "week_hours": week_hours, "week_cost": round(week_kwh * tariff, 2),
        "month_kwh": month_kwh, "month_hours": month_hours,
        "month_cost": round(month_kwh * tariff, 2),
        "all_time_peak": history.get("all_time_peak", 0.0),
        "days_tracked": len(days),
    }
