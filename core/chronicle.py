"""Хроника: что изменилось на компьютере с прошлого раза.

Компьютер меняется сам. Обновление Windows возвращает отключённую телеметрию.
Установщик игры добавляет себя в автозагрузку. Драйвер обновляется и сбрасывает
настройку. Программа, которую ставил полгода назад, тихо появилась в списке
служб. Обычно это замечают, только когда становится хуже, — и не понимают, что
именно произошло.

Хроника снимает слепок системы (не чаще раза в сутки) и сравнивает с прошлым:
какие программы появились и исчезли, что добавилось в автозагрузку, какие
драйверы обновились, какие настройки сбросились. Слепки хранятся локально,
это обычные JSON-файлы в папке приложения.

В слепок попадают только имена и версии — ни путей к документам, ни содержимого.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import APP_DIR, log

CHRONICLE_DIR = APP_DIR / "chronicle"
CHRONICLE_DIR.mkdir(parents=True, exist_ok=True)

MAX_SNAPSHOTS = 40
MIN_HOURS_BETWEEN = 20          # чаще раза в сутки снимать смысла нет


@dataclass
class Change:
    """Одно изменение между двумя слепками."""
    kind: str                   # программа / автозагрузка / драйвер / настройка
    direction: str              # появилось / исчезло / изменилось
    title: str
    detail: str = ""
    tone: str = "accent"        # accent / ok / warn


@dataclass
class Snapshot:
    """Слепок состояния системы в один момент."""
    stamp: float = 0.0
    programs: dict = field(default_factory=dict)      # имя -> версия
    startup: dict = field(default_factory=dict)       # имя -> включено
    drivers: dict = field(default_factory=dict)       # устройство -> версия
    tweaks: dict = field(default_factory=dict)        # id -> включено
    expert: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)      # имя службы -> тип запуска
    machine: dict = field(default_factory=dict)

    @property
    def when(self) -> str:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(self.stamp))

    @property
    def age_text(self) -> str:
        hours = (time.time() - self.stamp) / 3600
        if hours < 1:
            return "меньше часа назад"
        if hours < 24:
            return f"{int(hours)} ч назад"
        days = int(hours / 24)
        if days == 1:
            return "вчера"
        if days < 5:
            return f"{days} дня назад"
        return f"{days} дней назад"

    def to_dict(self) -> dict:
        return {
            "stamp": self.stamp, "programs": self.programs, "startup": self.startup,
            "drivers": self.drivers, "tweaks": self.tweaks, "expert": self.expert,
            "services": self.services, "machine": self.machine,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(
            stamp=float(data.get("stamp") or 0),
            programs=data.get("programs") or {},
            startup=data.get("startup") or {},
            drivers=data.get("drivers") or {},
            tweaks=data.get("tweaks") or {},
            expert=data.get("expert") or {},
            services=data.get("services") or {},
            machine=data.get("machine") or {})


def _safe(func, default):
    try:
        return func()
    except Exception as exc:
        log(f"Хроника: часть слепка не собралась — {exc}")
        return default


def capture(ctx) -> Snapshot:
    """Снимает текущее состояние системы."""
    snap = Snapshot(stamp=time.time())

    def programs() -> dict:
        from .uninstall import list_programs
        return {p.name: p.version for p in list_programs()}

    def startup() -> dict:
        from .cleanup import startup_items
        return {item.name: bool(item.enabled) for item in startup_items()}

    def drivers() -> dict:
        from .drivers import read_drivers
        return {d.device: d.version for d in read_drivers()}

    snap.programs = _safe(programs, {})
    snap.startup = _safe(startup, {})
    snap.drivers = _safe(drivers, {})
    snap.tweaks = _safe(
        lambda: {t.id: bool(t.state()) for t in ctx.tweaks}, {})
    snap.expert = _safe(
        lambda: {t.id: bool(t.state()) for t in getattr(ctx, "expert_tweaks", [])}, {})

    machine = ctx.machine
    snap.machine = {
        "cpu": machine.cpu_name, "gpu": machine.gpu.name,
        "ram_gb": round(machine.ram_gb, 1), "ram_sticks": machine.ram_sticks,
        "resolution": machine.resolution, "os": machine.os_name,
    }
    return snap


def save(snapshot: Snapshot) -> Path:
    path = CHRONICLE_DIR / f"{int(snapshot.stamp)}.json"
    path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False),
                    encoding="utf-8")
    _trim()
    log(f"Хроника: слепок сохранён ({len(snapshot.programs)} программ)")
    return path


def _trim() -> None:
    files = sorted(CHRONICLE_DIR.glob("*.json"))
    for old in files[:-MAX_SNAPSHOTS]:
        try:
            old.unlink()
        except OSError:
            pass


def history() -> list[Snapshot]:
    """Все слепки, от новых к старым."""
    snaps: list[Snapshot] = []
    for path in sorted(CHRONICLE_DIR.glob("*.json"), reverse=True):
        try:
            snaps.append(Snapshot.from_dict(
                json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return snaps


def should_capture() -> bool:
    """Пора ли снимать новый слепок."""
    snaps = history()
    if not snaps:
        return True
    return (time.time() - snaps[0].stamp) / 3600 >= MIN_HOURS_BETWEEN


def capture_if_due(ctx) -> Snapshot | None:
    """Снимает слепок, если прошло достаточно времени. Иначе ничего не делает."""
    if not should_capture():
        return None
    snapshot = capture(ctx)
    save(snapshot)
    return snapshot


# ---------------------------------------------------------------- сравнение


def _diff_map(old: dict, new: dict, kind: str, appeared: str, gone: str,
              changed: str = "", tone_new: str = "accent") -> list[Change]:
    """Сравнивает два словаря «имя -> значение»."""
    changes: list[Change] = []
    for name, value in new.items():
        if name not in old:
            changes.append(Change(kind, "появилось", name,
                                  f"{appeared}{f': {value}' if value and value is not True else ''}",
                                  tone_new))
        elif changed and old[name] != value:
            changes.append(Change(kind, "изменилось", name,
                                  f"{changed}: было {old[name]}, стало {value}", "accent"))
    for name in old:
        if name not in new:
            changes.append(Change(kind, "исчезло", name, gone, "ok"))
    return changes


def compare(old: Snapshot, new: Snapshot) -> list[Change]:
    """Что изменилось между двумя слепками."""
    changes: list[Change] = []

    changes += _diff_map(old.programs, new.programs, "Программа",
                         "установлена", "удалена", "обновилась")
    changes += _diff_map(old.drivers, new.drivers, "Драйвер",
                         "появился", "устройство пропало", "обновился")

    # автозагрузка: важно не только появление, но и включение/выключение
    for name, enabled in new.startup.items():
        if name not in old.startup:
            changes.append(Change(
                "Автозагрузка", "появилось", name,
                "программа добавила себя в автозагрузку"
                + (" и включена" if enabled else ", но выключена"),
                "warn" if enabled else "accent"))
        elif old.startup[name] != enabled:
            changes.append(Change(
                "Автозагрузка", "изменилось", name,
                "включена" if enabled else "выключена",
                "warn" if enabled else "ok"))
    for name in old.startup:
        if name not in new.startup:
            changes.append(Change("Автозагрузка", "исчезло", name,
                                  "убралась из автозагрузки", "ok"))

    # настройки: самое ценное — те, что сбросились сами
    for source, label in ((("tweaks",), "Настройка"), (("expert",), "Экспертная настройка")):
        key = source[0]
        old_map = getattr(old, key)
        new_map = getattr(new, key)
        for tweak_id, enabled in new_map.items():
            if tweak_id in old_map and old_map[tweak_id] != enabled:
                changes.append(Change(
                    label, "изменилось", tweak_id,
                    "включилась сама" if enabled else
                    "сбросилась — обычно это делает обновление Windows",
                    "accent" if enabled else "warn"))

    # железо меняется редко, но если поменялось — это самое важное событие
    for key, title in (("cpu", "Процессор"), ("gpu", "Видеокарта"),
                       ("ram_gb", "Объём памяти"), ("ram_sticks", "Планок памяти"),
                       ("resolution", "Разрешение"), ("os", "Система")):
        before = old.machine.get(key)
        after = new.machine.get(key)
        if before and after and before != after:
            changes.append(Change("Железо", "изменилось", title,
                                  f"было {before}, стало {after}", "accent"))

    order = {"Железо": 0, "Экспертная настройка": 1, "Настройка": 2,
             "Автозагрузка": 3, "Программа": 4, "Драйвер": 5}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.title.lower()))
    return changes


def describe(changes: list[Change]) -> str:
    """Короткий человеческий итог сравнения."""
    if not changes:
        return "С прошлого слепка ничего не изменилось."
    by_kind: dict[str, int] = {}
    for change in changes:
        by_kind[change.kind] = by_kind.get(change.kind, 0) + 1
    parts = [f"{kind.lower()} — {count}" for kind, count in by_kind.items()]
    reverted = sum(1 for c in changes
                   if "сбросилась" in c.detail)
    text = "Изменений: " + ", ".join(parts) + "."
    if reverted:
        text += (f" Из них {reverted} настроек сбросилось само — "
                 "стоит применить профиль заново.")
    return text


def resolve_titles(changes: list[Change], ctx) -> None:
    """Заменяет технические id твиков на человеческие названия."""
    names = {t.id: t.title for t in ctx.tweaks}
    names.update({t.id: t.title for t in getattr(ctx, "expert_tweaks", [])})
    for change in changes:
        if change.kind in ("Настройка", "Экспертная настройка"):
            change.title = names.get(change.title, change.title)
