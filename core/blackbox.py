"""ЧЁРНЫЙ ЯЩИК — бортовой самописец и автоматический откат при сбое.

Разгон убивает не жарой. Он убивает тем, что происходит через два часа игры,
когда ты уже забыл, что вчера подвинул ползунок: вылет драйвера, синий экран,
повреждённое сохранение. И самое обидное — потом невозможно понять, что именно
случилось, потому что показания в момент сбоя никто не записывал.

Чёрный ящик пишет их постоянно. Кольцевой буфер держит последние минуты
показаний с высоким разрешением; при срабатывании любого «датчика смерти»
буфер сбрасывается на диск целиком — получается запись последних секунд перед
происшествием, как у самолёта.

Датчики смерти (порог срабатывания настраивается):
  * температура видеокарты или процессора выше предела;
  * аварийный троттлинг по надёжности (карта сама режет частоту);
  * обвал кадров: нижний 1% рухнул относительно устоявшегося уровня;
  * вылет драйвера видеокарты — событие 4101 в журнале Windows;
  * синий экран в прошлой сессии — событие 1001 BugCheck.

Что происходит при срабатывании:
  1. буфер сбрасывается в файл происшествия;
  2. если включён автооткат — предел мощности видеокарты возвращается к
     штатному, экспертные настройки высокого риска выключаются, профиль
     переводится в безопасный;
  3. запись остаётся, чтобы потом посмотреть, что именно произошло.

Честно: «пересобрать драйверный стек на лету» невозможно. Выгрузка и загрузка
графического драйвера при работающей игре — это гарантированный чёрный экран
или синий. Что можно сделать по-настоящему: снять разгон, вернуть штатные
пределы и записать улики. Именно это здесь и делается.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .util import APP_DIR, log, ps_json

INCIDENT_DIR = APP_DIR / "incidents"
INCIDENT_DIR.mkdir(parents=True, exist_ok=True)

# Сколько секунд показаний держим в кольцевом буфере
BUFFER_SECONDS = 300


@dataclass
class Trigger:
    """Один датчик смерти."""
    key: str
    title: str
    why: str
    enabled: bool = True
    threshold: float = 0.0
    unit: str = ""


@dataclass
class Incident:
    """Зафиксированное происшествие."""
    when: float
    trigger: str
    detail: str
    reading: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    file: str = ""

    @property
    def stamp(self) -> str:
        return time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(self.when))


def default_triggers() -> list[Trigger]:
    return [
        Trigger("gpu_temp", "Перегрев видеокарты",
                "Выше этой температуры карта режет частоты сама, а разгон "
                "становится источником вылетов.", True, 88, "°C"),
        Trigger("cpu_temp", "Перегрев процессора",
                "На пределе температуры процессор сбрасывает турбо, и кадры "
                "проваливаются рывками.", True, 97, "°C"),
        Trigger("throttle", "Аварийный троттлинг видеокарты",
                "Карта сообщает, что режет частоту по надёжности, а не по "
                "мощности или температуре. Это уже тревога.", True, 3, "раз подряд"),
        Trigger("fps_collapse", "Обвал кадров",
                "Нижний 1% рухнул больше чем вдвое относительно устоявшегося "
                "уровня. Обычно это первый признак нестабильного разгона.", True,
                50, "% падения"),
        Trigger("driver_crash", "Вылет драйвера видеокарты",
                "Windows записала событие 4101: драйвер перестал отвечать и был "
                "восстановлен. Прямое следствие слишком агрессивного разгона.",
                True, 1, "событие"),
    ]


class BlackBox:
    """Пишет показания и следит за датчиками смерти."""

    def __init__(self, ctx=None, interval: float = 1.0):
        self.ctx = ctx
        self.interval = interval
        self.triggers = {t.key: t for t in default_triggers()}
        self.auto_rollback = True
        self.running = False
        self.buffer: deque = deque(maxlen=int(BUFFER_SECONDS / interval))
        self.incidents: list[Incident] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._throttle_streak = 0
        self._fps_baseline = 0.0
        self._last_driver_check = 0.0
        self._driver_events_seen = 0
        self._armed_at = 0.0

    # ------------------------------------------------------------ управление

    def start(self, auto_rollback: bool = True) -> tuple[bool, str]:
        if self.running:
            return True, "Чёрный ящик уже пишет."
        self.auto_rollback = auto_rollback
        self._stop.clear()
        self.running = True
        self._armed_at = time.time()
        self._driver_events_seen = self._count_driver_crashes()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log(f"Чёрный ящик включён, автооткат "
            f"{'включён' if auto_rollback else 'выключен'}")
        return True, (
            f"Пишу показания. Буфер держит последние {BUFFER_SECONDS // 60} минут. "
            + ("При срабатывании датчика разгон снимается автоматически."
               if auto_rollback else
               "Автооткат выключен: при срабатывании только запишу происшествие."))

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self.running = False
        log(f"Чёрный ящик выключен, происшествий за сессию: {len(self.incidents)}")

    @property
    def buffered_seconds(self) -> float:
        return round(len(self.buffer) * self.interval, 1)

    # ------------------------------------------------------------ цикл

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log(f"Чёрный ящик: сбой шага — {exc}")
            self._stop.wait(self.interval)

    def _tick(self) -> None:
        reading = dict(getattr(self.ctx, "last_reading", None) or {})
        if not reading:
            return
        reading["t"] = round(time.time(), 1)
        self.buffer.append(reading)

        self._check_temperature(reading)
        self._check_throttle()
        self._check_fps()
        self._check_driver_crash()

    # ------------------------------------------------------------ датчики

    def _check_temperature(self, reading: dict) -> None:
        for key, field_name, what in (("gpu_temp", "gpu_temp", "видеокарты"),
                                      ("cpu_temp", "cpu_temp", "процессора")):
            trigger = self.triggers[key]
            if not trigger.enabled:
                continue
            value = reading.get(field_name)
            if value and value >= trigger.threshold:
                self._fire(trigger,
                           f"Температура {what} достигла {value} °C "
                           f"при пороге {trigger.threshold:.0f} °C.", reading)

    def _check_throttle(self) -> None:
        trigger = self.triggers["throttle"]
        if not trigger.enabled:
            return
        from .overclock import _smi
        parts = None
        for prefix in ("clocks_event_reasons", "clocks_throttle_reasons"):
            parts = _smi(f"{prefix}.hw_slowdown", timeout=4)
            if parts and "not supported" not in parts[0].lower():
                break
            parts = None
        if not parts:
            return

        if parts[0].lower().startswith("active"):
            self._throttle_streak += 1
            if self._throttle_streak >= trigger.threshold:
                self._fire(trigger,
                           f"Карта режет частоту по надёжности "
                           f"{self._throttle_streak} замера подряд.",
                           self.buffer[-1] if self.buffer else {})
                self._throttle_streak = 0
        else:
            self._throttle_streak = 0

    def _check_fps(self) -> None:
        trigger = self.triggers["fps_collapse"]
        if not trigger.enabled:
            return
        from .fps import FpsReader
        if not hasattr(self, "_fps_reader"):
            self._fps_reader = FpsReader()
        stats = self._fps_reader.read_active()
        if not stats or stats.fps <= 0:
            return

        low = stats.fps_min or stats.fps
        if self._fps_baseline <= 0:
            self._fps_baseline = low
            return
        # опорный уровень поднимаем медленно, роняем сразу
        if low > self._fps_baseline:
            self._fps_baseline = self._fps_baseline * 0.9 + low * 0.1
            return

        drop = (self._fps_baseline - low) / self._fps_baseline * 100
        if drop >= trigger.threshold:
            self._fire(trigger,
                       f"Кадры упали с {self._fps_baseline:.0f} до {low:.0f} "
                       f"({drop:.0f}% падения).",
                       self.buffer[-1] if self.buffer else {})
            self._fps_baseline = low

    def _count_driver_crashes(self) -> int:
        """Сколько событий 4101 (вылет драйвера) уже в журнале."""
        data = ps_json(
            "(Get-WinEvent -FilterHashtable @{LogName='System'; Id=4101} "
            "-MaxEvents 50 -ErrorAction SilentlyContinue | Measure-Object).Count",
            timeout=25)
        try:
            return int(data)
        except (TypeError, ValueError):
            return 0

    def _check_driver_crash(self) -> None:
        trigger = self.triggers["driver_crash"]
        if not trigger.enabled:
            return
        # журнал событий опрашиваем редко: запрос дорогой
        now = time.time()
        if now - self._last_driver_check < 30:
            return
        self._last_driver_check = now

        count = self._count_driver_crashes()
        if count > self._driver_events_seen:
            new = count - self._driver_events_seen
            self._driver_events_seen = count
            self._fire(trigger,
                       f"Windows записала {new} новых событий о вылете драйвера "
                       "видеокарты (код 4101).",
                       self.buffer[-1] if self.buffer else {})

    # ------------------------------------------------------------ срабатывание

    def _fire(self, trigger: Trigger, detail: str, reading: dict) -> None:
        incident = Incident(when=time.time(), trigger=trigger.title,
                            detail=detail, reading=dict(reading))
        log(f"ЧЁРНЫЙ ЯЩИК: {trigger.title} — {detail}")

        if self.auto_rollback:
            incident.actions = self._rollback()
        incident.file = self._dump(incident)
        self.incidents.insert(0, incident)
        del self.incidents[40:]

        # после срабатывания даём системе устояться, чтобы не сыпать записями
        trigger.enabled = False
        threading.Timer(60.0, lambda: setattr(trigger, "enabled", True)).start()

    def _rollback(self) -> list[str]:
        """Снимает разгон и возвращает безопасные пределы."""
        actions: list[str] = []

        # 1. предел мощности видеокарты — к штатному
        try:
            from .overclock import apply_power_limit, read_gpu_caps
            caps = read_gpu_caps()
            if caps.available and caps.power_default:
                ok, _msg = apply_power_limit(int(caps.power_default))
                if ok:
                    actions.append(
                        f"Предел мощности видеокарты возвращён к штатным "
                        f"{caps.power_default:.0f} Вт.")
        except Exception as exc:
            log(f"Автооткат: предел мощности не вернулся — {exc}")

        # 2. умный авторазгон — стоп
        autooc = getattr(self.ctx, "autooc", None)
        if autooc is not None and getattr(autooc.state, "running", False):
            try:
                autooc.stop(restore=True)
                actions.append("Умный авторазгон остановлен.")
            except Exception:
                pass

        # 3. экспертные настройки высокого риска — выключить
        try:
            risky = [t for t in getattr(self.ctx, "expert_tweaks", [])
                     if t.risk == "высокий" and t.state()]
            for tweak in risky:
                if tweak.apply(False):
                    actions.append(f"Выключено: {tweak.title}.")
        except Exception as exc:
            log(f"Автооткат: экспертные настройки не снялись — {exc}")

        if not actions:
            actions.append("Откатывать было нечего: разгон и опасные настройки "
                           "не были включены.")
        return actions

    def _dump(self, incident: Incident) -> str:
        """Сбрасывает буфер на диск: запись последних минут перед сбоем."""
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(incident.when))
        path = INCIDENT_DIR / f"{stamp}.json"
        payload = {
            "when": incident.when,
            "trigger": incident.trigger,
            "detail": incident.detail,
            "actions": incident.actions,
            "buffer_seconds": self.buffered_seconds,
            "readings": list(self.buffer),
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False,
                                       separators=(",", ":")), encoding="utf-8")
            log(f"Чёрный ящик: запись сброшена в {path.name} "
                f"({self.buffered_seconds:.0f} с показаний)")
            return str(path)
        except OSError as exc:
            log(f"Чёрный ящик: не удалось записать происшествие — {exc}")
            return ""


# ---------------------------------------------------------------- разбор записей


def load_incidents(limit: int = 30) -> list[dict]:
    """Читает сохранённые происшествия, от новых к старым."""
    rows = []
    for path in sorted(INCIDENT_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["file"] = str(path)
            rows.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def analyze(incident: dict) -> list[str]:
    """Человеческий разбор записи: что видно в показаниях перед сбоем."""
    readings = incident.get("readings") or []
    if not readings:
        return ["Показания не записались — буфер был пуст."]

    notes: list[str] = []
    window = readings[-30:]

    temps = [r.get("gpu_temp") for r in window if r.get("gpu_temp")]
    if temps:
        notes.append(
            f"Температура видеокарты за полминуты до сбоя: от {min(temps)} "
            f"до {max(temps)} °C.")
        if max(temps) - min(temps) >= 12:
            notes.append("Температура скакала больше чем на 12 градусов — "
                         "похоже на резкий рост нагрузки или проблему с обдувом.")

    powers = [r.get("wall_w") for r in window if r.get("wall_w")]
    if powers:
        notes.append(f"Потребление из розетки: от {min(powers):.0f} "
                     f"до {max(powers):.0f} Вт.")
        if max(powers) - min(powers) >= 150:
            notes.append("Резкий скачок потребления — если блок питания на "
                         "пределе, просадка напряжения объясняет сбой.")

    loads = [r.get("gpu_load") for r in window if r.get("gpu_load") is not None]
    if loads and max(loads) >= 97:
        notes.append("Видеокарта была загружена под сто процентов — сбой "
                     "случился на полной нагрузке, а не на ровном месте.")

    vram = [(r.get("gpu_mem_used"), r.get("gpu_mem_total")) for r in window
            if r.get("gpu_mem_total")]
    if vram:
        used, total = vram[-1]
        if used and total and used / total >= 0.95:
            notes.append(
                f"Видеопамять была забита на {used / total * 100:.0f}% "
                f"({used} из {total} МБ) — нехватка видеопамяти сама по себе "
                "вызывает вылеты.")

    actions = incident.get("actions") or []
    if actions:
        notes.append("Что было сделано автоматически: " + " ".join(actions))
    return notes
