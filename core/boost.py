"""Режим «Турбо»: одна кнопка, которая готовит компьютер к игре, и откат к прежнему состоянию."""
from __future__ import annotations

import psutil

from . import memory, optimizer, overclock
from .context import AppContext
from .util import log

SNAPSHOT_BOOST = "before_boost"

# Программы, которые чаще всего мешают играм: они жрут память и рисуют оверлеи.
_BACKGROUND_CANDIDATES = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Firefox",
    "opera.exe": "Opera",
    "browser.exe": "Яндекс Браузер",
    "Telegram.exe": "Telegram",
    "Discord.exe": "Discord",
    "Spotify.exe": "Spotify",
    "OneDrive.exe": "OneDrive",
    "Skype.exe": "Skype",
    "Teams.exe": "Microsoft Teams",
    "SearchApp.exe": "Поиск Windows",
    "WhatsApp.exe": "WhatsApp",
    "EpicWebHelper.exe": "Epic Web Helper",
    "GoogleDriveFS.exe": "Google Drive",
    "YandexDisk.exe": "Яндекс.Диск",
    "steamwebhelper.exe": "Steam Web Helper",
}


def heavy_background() -> list[dict]:
    """Фоновые программы-кандидаты на закрытие, с их аппетитом к памяти."""
    found: dict[str, dict] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = proc.info["name"] or ""
            if name not in _BACKGROUND_CANDIDATES:
                continue
            mem = proc.info["memory_info"]
            mb = int(mem.rss / (1024 ** 2)) if mem else 0
            entry = found.setdefault(name, {
                "process": name,
                "title": _BACKGROUND_CANDIDATES[name],
                "mb": 0,
                "pids": [],
            })
            entry["mb"] += mb
            entry["pids"].append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows = sorted(found.values(), key=lambda r: -r["mb"])
    return [r for r in rows if r["mb"] >= 40]


def close_processes(process_names: list[str]) -> tuple[int, int]:
    """Аккуратно закрывает выбранные программы. Возвращает (закрыто, освобождено МБ)."""
    before = psutil.virtual_memory().available
    closed = 0
    victims = []
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info["name"] or "") in process_names:
            victims.append(proc)
    for proc in victims:
        try:
            proc.terminate()
            closed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    gone, alive = psutil.wait_procs(victims, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    freed = max(0, psutil.virtual_memory().available - before)
    return closed, int(freed / (1024 ** 2))


def run_boost(ctx: AppContext, close_apps: list[str] | None = None,
              rgb_scene: bool = True, progress=None) -> list[str]:
    """Готовит систему к игре. Возвращает список выполненных шагов."""
    steps: list[str] = []

    def report(text: str):
        steps.append(text)
        if progress:
            progress(text)

    # 1. Снимок, чтобы было куда возвращаться
    optimizer.save_snapshot(ctx.tweaks, SNAPSHOT_BOOST)
    report("Текущее состояние системы сохранено — откат доступен в любой момент.")

    # 2. Твики, помеченные как рекомендованные
    applied = 0
    for tweak in ctx.tweaks:
        if not tweak.recommended:
            continue
        if tweak.state():
            continue
        if tweak.apply(True):
            applied += 1
    report(f"Применено настроек Windows: {applied}.")

    # 3. Электропитание и политика процессора
    if optimizer.set_power_plan(optimizer.ensure_ultimate_plan()):
        report("План электропитания переключён на максимальную производительность.")
    ok, msg = overclock.apply_cpu_profile("performance")
    if ok:
        report("Процессор: турбо-режим без парковки ядер.")
    else:
        report(f"Профиль процессора не применён — {msg}")

    # 4. Фоновые программы
    if close_apps:
        closed, freed = close_processes(close_apps)
        report(f"Закрыто фоновых программ: {closed}, освобождено {freed} МБ.")

    # 5. Очистка памяти
    result = memory.clean_memory()
    report(f"Память очищена: освобождено {result['freed_mb']} МБ "
           f"({result['before_percent']:.0f}% → {result['after_percent']:.0f}% занято).")

    # 6. Подсветка
    if rgb_scene and ctx.rgb_client and ctx.rgb_client.connected:
        ctx.effects.effect = "static"
        ctx.effects.color = (255, 30, 0)
        ctx.effects.apply_once()
        report("Подсветка переключена на игровую сцену.")

    log("Режим «Турбо» выполнен")
    return steps


def restore_boost(ctx: AppContext) -> list[str]:
    """Возвращает систему в состояние до включения «Турбо»."""
    steps: list[str] = []
    ok, total = optimizer.restore_snapshot(ctx.tweaks, SNAPSHOT_BOOST)
    if total:
        steps.append(f"Настройки Windows возвращены: {ok} из {total}.")
    else:
        steps.append("Снимка не нашлось — возвращать нечего.")
    success, msg = overclock.apply_cpu_profile("balanced")
    steps.append("Процессор: сбалансированный режим." if success else msg)
    if ctx.rgb_client and ctx.rgb_client.connected:
        ctx.effects.effect = "rainbow"
        ctx.effects.start()
        steps.append("Подсветка вернулась к обычной сцене.")
    log("Режим «Турбо» отключён")
    return steps
