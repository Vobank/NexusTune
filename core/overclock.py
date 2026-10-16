"""Разгон и тонкая настройка видеокарты и процессора.

Принцип модуля: приложение само меняет только то, что официально поддерживается
драйвером и Windows (предел мощности, политики процессора). Смещения частот
рассчитываются и отдаются пошаговым планом для MSI Afterburner — вслепую
их не применяет ни одна честная программа.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .hwinfo import Machine
from .util import is_admin, log, run, run_ps

# ---------------------------------------------------------------- возможности видеокарты


@dataclass
class GpuCaps:
    available: bool = False
    power_now: float = 0.0
    power_min: float = 0.0
    power_max: float = 0.0
    power_default: float = 0.0
    temp: int = 0
    max_core_mhz: int = 0
    max_mem_mhz: int = 0
    can_raise_power: bool = False
    note: str = ""


def _smi(query: str, timeout: int = 10) -> list[str] | None:
    code, out, _ = run(["nvidia-smi", f"--query-gpu={query}",
                        "--format=csv,noheader,nounits"], timeout=timeout)
    if code != 0 or not out:
        return None
    return [p.strip() for p in out.splitlines()[0].split(",")]


def read_gpu_caps() -> GpuCaps:
    """Что реально можно менять на этой видеокарте."""
    caps = GpuCaps()
    if not shutil.which("nvidia-smi"):
        caps.note = ("Управление доступно только для видеокарт NVIDIA. "
                     "Для AMD используй Adrenalin, для Intel — Arc Control.")
        return caps
    parts = _smi("power.limit,power.min_limit,power.max_limit,power.default_limit,"
                 "temperature.gpu,clocks.max.graphics,clocks.max.memory")
    if not parts or len(parts) < 7:
        caps.note = "Драйвер не отдал параметры мощности."
        return caps

    def f(i):
        try:
            return float(parts[i])
        except (ValueError, IndexError):
            return 0.0

    caps.available = True
    caps.power_now, caps.power_min = f(0), f(1)
    caps.power_max, caps.power_default = f(2), f(3)
    caps.temp = int(f(4))
    caps.max_core_mhz, caps.max_mem_mhz = int(f(5)), int(f(6))
    caps.can_raise_power = caps.power_max > caps.power_default + 1
    if not caps.can_raise_power:
        caps.note = (f"Предел мощности зафиксирован на {caps.power_default:.0f} Вт — "
                     "прошивка карты не даёт поднять его выше. Снижать можно.")
    return caps


def apply_power_limit(watts: int) -> tuple[bool, str]:
    """Меняет предел мощности видеокарты (официальная функция драйвера)."""
    if not is_admin():
        return False, "Нужны права администратора."
    code, out, err = run(["nvidia-smi", "-pl", str(int(watts))], timeout=30)
    text = (out + " " + err).strip()
    if code == 0 and "not supported" not in text.lower():
        log(f"Предел мощности видеокарты: {watts} Вт")
        return True, f"Предел мощности установлен: {watts} Вт."
    return False, text or "Драйвер отклонил изменение предела мощности."


# ---------------------------------------------------------------- профили разгона


@dataclass
class OcProfile:
    key: str
    title: str
    subtitle: str
    core_offset: int          # МГц к ядру
    mem_offset: int           # МГц к памяти (в шкале Afterburner)
    power_percent: int        # процент от штатного предела
    temp_limit: int
    risk: str
    expected: str
    steps: list[str] = field(default_factory=list)


def _arch(gpu_name: str) -> str:
    low = gpu_name.lower()
    if re.search(r"rtx\s*50", low):
        return "blackwell"
    if re.search(r"rtx\s*40", low):
        return "ada"
    if re.search(r"rtx\s*30", low):
        return "ampere"
    if re.search(r"rtx\s*20|gtx\s*16", low):
        return "turing"
    if re.search(r"gtx\s*10", low):
        return "pascal"
    if re.search(r"rx\s*7\d00", low):
        return "rdna3"
    if re.search(r"rx\s*6\d00", low):
        return "rdna2"
    return "unknown"


# Типичные безопасные смещения по архитектурам: (ядро, память)
_OC_BASE = {
    "blackwell": (150, 1500), "ada": (150, 1200), "ampere": (110, 900),
    "turing": (120, 700), "pascal": (140, 500),
    "rdna3": (100, 100), "rdna2": (100, 100), "unknown": (80, 500),
}


def gpu_profiles(machine: Machine, caps: GpuCaps) -> list[OcProfile]:
    """Три профиля: тихий, разумный разгон и максимум."""
    arch = _arch(machine.gpu.name)
    core, mem = _OC_BASE.get(arch, _OC_BASE["unknown"])
    max_power_pct = int(caps.power_max / caps.power_default * 100) if caps.power_default else 100

    quiet = OcProfile(
        key="quiet", title="Тихий и холодный",
        subtitle="Меньше шума и температуры почти без потери кадров",
        core_offset=0, mem_offset=0, power_percent=80, temp_limit=75, risk="нулевой",
        expected="Потеря 3-6% FPS, минус 8-12 °C и заметно тише вентиляторы.",
        steps=[
            "Снизить предел мощности до 80% — это приложение сделает само.",
            "В Afterburner задать кривую вентиляторов помягче: 60 °C → 45%, 75 °C → 65%.",
            "Проверить в игре: частота почти не упадёт, карта станет холоднее.",
        ])

    balanced = OcProfile(
        key="balanced", title="Разумный разгон",
        subtitle="Проверенные значения, которые держит почти любой экземпляр",
        core_offset=core, mem_offset=mem,
        power_percent=min(max_power_pct, 100), temp_limit=83, risk="низкий",
        expected=f"Прирост 4-8% кадров. Память даёт больше, чем ядро, на {machine.gpu.name}.",
        steps=[
            f"Открыть MSI Afterburner и поднять Power Limit до {min(max_power_pct, 100)}%.",
            f"Core Clock: +{core} МГц. Применить и погонять игру 20 минут.",
            f"Memory Clock: +{mem} МГц. Применить и снова проверить.",
            "Если появились артефакты или вылет драйвера — снизить память на 100 МГц.",
            "Сохранить в профиль 1 и включить «применять при запуске Windows».",
        ])

    limit = OcProfile(
        key="max", title="Поиск предела",
        subtitle="Пошаговый разгон до личного максимума карты",
        core_offset=int(core * 1.6), mem_offset=int(mem * 1.6),
        power_percent=max_power_pct, temp_limit=87, risk="средний",
        expected="Прирост 7-12% при удаче. Каждый экземпляр карты разгоняется по-своему.",
        steps=[
            f"Начать с профиля «Разумный разгон»: +{core} / +{mem}.",
            "Поднимать ядро шагами по 15 МГц, каждый раз 15 минут теста.",
            "После первого артефакта или вылета откатиться на два шага назад.",
            "То же самое с памятью, шаг 100 МГц. Память падает тише — следи за FPS: "
            "если он начал снижаться, коррекция ошибок уже съедает прирост.",
            "Финальную связку прогнать час в самой тяжёлой игре.",
        ])

    if not caps.can_raise_power:
        for p in (balanced, limit):
            p.power_percent = 100
            p.steps.insert(0, "Предел мощности поднять нельзя — прошивка карты его фиксирует. "
                              "Разгон пойдёт за счёт запаса частот, а не ватт.")
    return [quiet, balanced, limit]


# ---------------------------------------------------------------- MSI Afterburner


def afterburner_path() -> str | None:
    """Путь к MSI Afterburner, если он установлен."""
    candidates = [
        r"C:\Program Files (x86)\MSI Afterburner\MSIAfterburner.exe",
        r"C:\Program Files\MSI Afterburner\MSIAfterburner.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    found = shutil.which("MSIAfterburner.exe")
    return found


def launch_afterburner() -> bool:
    path = afterburner_path()
    if not path:
        return False
    run(["cmd", "/c", "start", "", path], timeout=10)
    return True


# ---------------------------------------------------------------- контроль стабильности


@dataclass
class StabilityReport:
    samples: int = 0
    max_temp: int = 0
    avg_temp: float = 0.0
    max_power: float = 0.0
    min_clock: int = 0
    max_clock: int = 0
    throttle_power: int = 0
    throttle_thermal: int = 0
    throttle_reliability: int = 0
    verdict: str = ""
    advice: list[str] = field(default_factory=list)


class StabilityWatch:
    """Следит за видеокартой во время нагрузки и выносит вердикт.

    Приложение не умеет само нагружать видеокарту трёхмерной сценой, поэтому
    честный путь такой: запусти игру или FurMark, а этот наблюдатель соберёт
    температуры, частоты и причины троттлинга и объяснит результат.
    """

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.temps: list[int] = []
        self.clocks: list[int] = []
        self.powers: list[float] = []
        self.throttles = {"power": 0, "thermal": 0, "reliability": 0}
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.temps.clear()
        self.clocks.clear()
        self.powers.clear()
        self.throttles = {"power": 0, "thermal": 0, "reliability": 0}
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> StabilityReport:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.running = False
        return self.report()

    def _loop(self) -> None:
        while not self._stop.is_set():
            parts = _smi("temperature.gpu,clocks.current.graphics,power.draw", timeout=5)
            if parts and len(parts) >= 3:
                try:
                    self.temps.append(int(float(parts[0])))
                    self.clocks.append(int(float(parts[1])))
                    self.powers.append(float(parts[2]))
                except ValueError:
                    pass
            self._read_throttle()
            self._stop.wait(self.interval)

    def _read_throttle(self) -> None:
        # в новых драйверах поля переименованы в clocks_event_reasons
        for prefix in ("clocks_event_reasons", "clocks_throttle_reasons"):
            parts = _smi(f"{prefix}.sw_power_cap,{prefix}.hw_thermal_slowdown,"
                         f"{prefix}.hw_slowdown", timeout=5)
            if parts and len(parts) >= 3 and "not supported" not in parts[0].lower():
                if parts[0].lower().startswith("active"):
                    self.throttles["power"] += 1
                if parts[1].lower().startswith("active"):
                    self.throttles["thermal"] += 1
                if parts[2].lower().startswith("active"):
                    self.throttles["reliability"] += 1
                return

    def report(self) -> StabilityReport:
        rep = StabilityReport(samples=len(self.temps))
        if not self.temps:
            rep.verdict = "Данных нет — наблюдение шло слишком мало."
            return rep
        rep.max_temp = max(self.temps)
        rep.avg_temp = round(sum(self.temps) / len(self.temps), 1)
        rep.max_power = round(max(self.powers), 1) if self.powers else 0.0
        rep.min_clock = min(self.clocks) if self.clocks else 0
        rep.max_clock = max(self.clocks) if self.clocks else 0
        rep.throttle_power = self.throttles["power"]
        rep.throttle_thermal = self.throttles["thermal"]
        rep.throttle_reliability = self.throttles["reliability"]

        if rep.max_temp >= 85:
            rep.verdict = "Горячо"
            rep.advice.append("Температура выше 85 °C. Подними обороты вентиляторов "
                              "или снизь предел мощности до 85%.")
        elif rep.max_temp >= 78:
            rep.verdict = "В норме, но тепло"
        else:
            rep.verdict = "Отлично"

        if rep.throttle_thermal:
            rep.advice.append("Карта сбрасывала частоты из-за нагрева — разгон здесь "
                              "бесполезен, сначала охлаждение.")
        if rep.throttle_power and rep.throttle_power > rep.samples * 0.5:
            rep.advice.append("Карта почти всё время упирается в предел мощности. "
                              "Прирост от разгона ядра будет минимальным.")
        if rep.max_clock and rep.min_clock and rep.max_clock - rep.min_clock > 400:
            rep.advice.append(f"Частота гуляет от {rep.min_clock} до {rep.max_clock} МГц — "
                              "это нормально в меню, но не под постоянной нагрузкой.")
        if not rep.advice:
            rep.advice.append("Карта держит частоты ровно, троттлинга нет. "
                              "Можно пробовать следующий шаг разгона.")
        return rep


# ---------------------------------------------------------------- процессор


def _set_proc(setting: str, value: int) -> bool:
    code, _o, _e = run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                        "SUB_PROCESSOR", setting, str(value)], timeout=20)
    if code != 0:
        return False
    run(["powercfg", "/setactive", "SCHEME_CURRENT"], timeout=20)
    return True


def apply_cpu_profile(profile: str) -> tuple[bool, str]:
    """Применяет политику процессора: performance / balanced / quiet."""
    if not is_admin():
        return False, "Нужны права администратора."
    presets = {
        # (макс. состояние, мин. состояние, режим ускорения, минимум активных ядер)
        "performance": (100, 100, 2, 100),
        "balanced": (100, 5, 1, 0),
        "quiet": (95, 5, 3, 0),
    }
    if profile not in presets:
        return False, "Неизвестный профиль."
    max_s, min_s, boost, parking = presets[profile]
    ok = all([
        _set_proc("PROCTHROTTLEMAX", max_s),
        _set_proc("PROCTHROTTLEMIN", min_s),
        _set_proc("PERFBOOSTMODE", boost),
        _set_proc("CPMINCORES", parking),
    ])
    names = {"performance": "Максимальная производительность",
             "balanced": "Сбалансированный", "quiet": "Тихий"}
    if ok:
        log(f"Профиль процессора: {profile}")
        return True, f"Профиль процессора «{names[profile]}» применён."
    return False, "Часть параметров не применилась — проверь права администратора."


def cpu_advice(machine: Machine) -> list[tuple[str, str]]:
    """Советы по процессору и памяти, которые можно сделать только в BIOS."""
    tips: list[tuple[str, str]] = []
    if machine.ram_type == "DDR5" and machine.ram_speed and machine.ram_speed <= 4800:
        tips.append(("Включи XMP/EXPO в BIOS",
                     f"Память работает на {machine.ram_speed} МГц — это заводской минимум JEDEC. "
                     "Профиль XMP поднимет её до паспортной частоты, это бесплатные 5-15% "
                     "кадров в играх, упирающихся в процессор."))
    elif machine.ram_type == "DDR4" and machine.ram_speed and machine.ram_speed <= 2400:
        tips.append(("Включи XMP в BIOS",
                     f"Память на {machine.ram_speed} МГц работает вполсилы. "
                     "XMP включается одним пунктом в BIOS."))
    else:
        tips.append(("Профиль памяти активен",
                     f"Память идёт на {machine.ram_speed} МГц — заводской профиль включён. "
                     "Дальнейший разгон памяти даёт единицы процентов и требует "
                     "долгой проверки стабильности."))

    if machine.ram_sticks == 1:
        tips.append(("Поставь вторую планку памяти",
                     "Одна планка — одноканальный режим. Вторая такая же планка даёт "
                     "до 20% кадров в играх. Это дешевле любого разгона."))

    if machine.cpu_vendor == "intel" and machine.cpu_tier >= 4:
        tips.append(("Проверь лимиты мощности в BIOS",
                     "На платах с чипсетом Z можно снять ограничения PL1/PL2 — "
                     "процессор будет дольше держать турбо. Следи за температурой: "
                     "выше 95 °C он всё равно сбросит частоту."))
        tips.append(("Андервольт вместо разгона",
                     "Смещение напряжения на -0,05 В обычно снижает температуру на 8-10 °C "
                     "и позволяет держать турбо дольше. Прирост больше, чем от разгона."))
    if machine.cpu_vendor == "amd":
        tips.append(("Включи PBO с ограничением по температуре",
                     "Precision Boost Overdrive с лимитом 85 °C даёт прирост без риска. "
                     "Curve Optimizer со смещением -15 добавит ещё немного."))
    return tips
