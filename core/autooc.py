"""Умный самонастраивающийся разгон.

Слепой разгон — это подобрать цифры один раз и надеяться. Здесь по-другому:
приложение постоянно смотрит на карту во время игры и подстраивается.

Что оно видит:
  * температуру, частоту и мощность карты — из драйвера (nvidia-smi);
  * причины троттлинга — почему карта сбрасывает частоту прямо сейчас;
  * реальные кадры и просадки (нижний 1%) — из RivaTuner, если он есть.

Как решает:
  * карта упирается в предел мощности, а температура низкая → поднять предел,
    это честный прирост частоты без риска;
  * пошли просадки кадров или карта греется под потолок → откатить назад;
  * всё ровно и есть запас → маленький шаг вверх и снова проверка.

Что оно меняет само: только предел мощности (nvidia-smi -pl) — это официальная
функция драйвера, полностью обратимая, не способная повредить карту. Смещения
частот ядра вслепую не трогаются: их по-прежнему выдаём планом для Afterburner.

На картах AMD и Intel официального способа менять предел из командной строки
нет, поэтому там модуль честно отключается и объясняет это.
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field

from .fps import FpsTracker
from .overclock import _smi, apply_power_limit, read_gpu_caps
from .util import log


@dataclass
class Step:
    """Одно решение автонастройки — для журнала на экране."""
    when: float
    text: str
    power_limit: int
    temp: int
    reason: str


@dataclass
class AutoState:
    """Текущее состояние для отрисовки."""
    running: bool = False
    limit_now: int = 0
    limit_min: int = 0
    limit_max: int = 0
    limit_default: int = 0
    temp: int = 0
    power: float = 0.0
    clock: int = 0
    fps: float = 0.0
    fps_low: float = 0.0
    verdict: str = "готов"
    steps: list = field(default_factory=list)


class SmartOverclock:
    """Фоновая автонастройка предела мощности видеокарты под живую нагрузку."""

    # пороги подобраны консервативно: цель — стабильность, а не рекорд
    TEMP_CEILING = 80          # выше — откатываемся, карте жарко
    TEMP_HEADROOM = 72         # ниже — есть куда поднимать
    STEP_WATTS = 8             # шаг изменения предела
    SETTLE_SECONDS = 12        # ждём столько после каждого шага, прежде чем судить
    STUTTER_DROP = 0.75        # 1%-низкий упал ниже 75% от разгона — это рывки

    def __init__(self, interval: float = 3.0):
        self.interval = interval
        self.caps = None
        self.state = AutoState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fps = FpsTracker()
        self._baseline_low = 0.0
        self._last_change = 0.0

    # ------------------------------------------------------------ доступность

    def available(self) -> tuple[bool, str]:
        """Можно ли вообще запускать автонастройку на этой машине."""
        if not shutil.which("nvidia-smi"):
            return False, ("Автонастройка предела работает только на видеокартах "
                           "NVIDIA — она опирается на nvidia-smi из драйвера. "
                           "Для AMD используй Adrenalin, для Intel — Arc Control.")
        caps = read_gpu_caps()
        self.caps = caps
        if not caps.available:
            return False, caps.note or "Драйвер не отдаёт параметры мощности."
        if not caps.can_raise_power:
            return True, ("Прошивка карты держит предел мощности зафиксированным, "
                          "поднять его нельзя. Автонастройка сможет только "
                          "аккуратно снижать предел, если карта перегревается.")
        return True, ("Готово. Приложение будет держать предел мощности на "
                      "максимуме, пока карта холодная, и отступать при перегреве "
                      "или рывках.")

    # ------------------------------------------------------------ управление

    def start(self) -> tuple[bool, str]:
        if self.state.running:
            return True, "Уже работает."
        ok, message = self.available()
        if not ok:
            return False, message
        caps = self.caps
        self.state.limit_min = int(caps.power_min)
        self.state.limit_max = int(caps.power_max)
        self.state.limit_default = int(caps.power_default)
        self.state.limit_now = int(caps.power_now)
        self._baseline_low = 0.0
        self._last_change = 0.0
        self._stop.clear()
        self.state.running = True
        self.state.verdict = "разогреваюсь"
        self._add_step("Автонастройка включена. Запусти игру — дальше сама.",
                       "старт")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("Умный разгон: запущен")
        return True, message

    def stop(self, restore: bool = True) -> None:
        if not self.state.running:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        self.state.running = False
        self.state.verdict = "остановлен"
        if restore and self.caps and self.state.limit_default:
            apply_power_limit(self.state.limit_default)
            self._add_step(f"Предел возвращён к штатному "
                           f"{self.state.limit_default} Вт.", "стоп")
        log("Умный разгон: остановлен")

    # ------------------------------------------------------------ цикл

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log(f"Умный разгон: сбой шага — {exc}")
            self._stop.wait(self.interval)

    def _read(self) -> bool:
        parts = _smi("temperature.gpu,clocks.current.graphics,power.draw,"
                     "power.limit,utilization.gpu", timeout=5)
        if not parts or len(parts) < 5:
            return False
        try:
            self.state.temp = int(float(parts[0]))
            self.state.clock = int(float(parts[1]))
            self.state.power = round(float(parts[2]), 1)
            self.state.limit_now = int(float(parts[3]))
            self._util = int(float(parts[4]))
        except ValueError:
            return False
        return True

    def _power_capped(self) -> bool:
        """Карта прямо сейчас упирается в предел мощности?"""
        for prefix in ("clocks_event_reasons", "clocks_throttle_reasons"):
            parts = _smi(f"{prefix}.sw_power_cap", timeout=4)
            if parts and "not supported" not in parts[0].lower():
                return parts[0].lower().startswith("active")
        # запасной признак: мощность вплотную к пределу
        return self.state.power >= self.state.limit_now - 3

    def _tick(self) -> None:
        if not self._read():
            self.state.verdict = "нет данных от драйвера"
            return

        stats = self._fps.poll()
        if stats:
            self.state.fps = stats.fps
            summary = self._fps.summary()
            self.state.fps_low = summary.get("low_1", 0.0)

        under_load = getattr(self, "_util", 0) >= 40
        if not under_load:
            self.state.verdict = "жду нагрузку (запусти игру)"
            return

        now = time.time()
        # даём пределу «устояться» после каждого изменения
        if now - self._last_change < self.SETTLE_SECONDS:
            self.state.verdict = "проверяю после изменения…"
            return

        # 1. Перегрев — приоритет над всем: отступаем.
        if self.state.temp >= self.TEMP_CEILING:
            self._nudge(-self.STEP_WATTS,
                        f"{self.state.temp} °C — жарко, снижаю предел",
                        "перегрев")
            return

        # 2. Рывки: 1%-низкий заметно просел относительно лучшего — отступаем.
        low = self.state.fps_low
        if low > 0:
            self._baseline_low = max(self._baseline_low, low)
            if self._baseline_low and low < self._baseline_low * self.STUTTER_DROP:
                self._nudge(-self.STEP_WATTS,
                            f"просадки кадров (1% упал до {low:.0f}), откатываю",
                            "рывки")
                self._baseline_low = low       # принимаем новый уровень за опорный
                return

        # 3. Есть запас и карта упирается в мощность — поднимаем.
        if (self.state.temp <= self.TEMP_HEADROOM
                and self.state.limit_now < self.state.limit_max
                and self._power_capped()):
            self._nudge(self.STEP_WATTS,
                        f"{self.state.temp} °C и упор в мощность — добавляю предел",
                        "есть запас")
            return

        self.state.verdict = "держу оптимум"

    def _nudge(self, delta: int, human: str, reason: str) -> None:
        target = self.state.limit_now + delta
        target = max(self.state.limit_min, min(self.state.limit_max, target))
        if target == self.state.limit_now:
            self.state.verdict = ("предел уже на максимуме" if delta > 0
                                  else "предел уже на минимуме")
            return
        ok, _msg = apply_power_limit(target)
        if ok:
            self.state.limit_now = target
            self._last_change = time.time()
            self.state.verdict = human
            self._add_step(f"{human}. Предел → {target} Вт.", reason)

    def _add_step(self, text: str, reason: str) -> None:
        self.state.steps.insert(0, Step(
            when=time.time(), text=text, power_limit=self.state.limit_now,
            temp=self.state.temp, reason=reason))
        del self.state.steps[40:]
