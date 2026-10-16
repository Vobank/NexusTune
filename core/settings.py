"""Настройки самого приложения: автозапуск, поведение, обслуживание данных.

Всё, что относится к тому, как ведёт себя КРЕМЕНЬ, а не к тому, как он меняет
Windows. Значения живут в общем конфиге (`%LOCALAPPDATA%\\Kremen\\config.json`),
здесь только операции, которым нужен доступ к системе.
"""
from __future__ import annotations

import os
import shutil
import sys
import winreg
from pathlib import Path

from .util import APP_DIR, CONFIG_FILE, LOG_FILE, SNAPSHOT_DIR, human_bytes, log

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Kremen"


# ---------------------------------------------------------------- автозапуск


def _launch_command() -> str:
    """Команда, которой Windows будет запускать приложение."""
    if getattr(sys, "frozen", False):
        # собранный .exe — запускаем его самого
        return f'"{Path(sys.executable).resolve()}"'

    # Запуск из исходников. sys.argv[0] может быть не путём к скрипту (например,
    # при запуске через -c), поэтому берём main.py рядом с пакетом core.
    script = Path(sys.argv[0]).resolve()
    if script.suffix.lower() != ".py" or not script.exists():
        script = Path(__file__).resolve().parent.parent / "main.py"

    # pythonw не открывает чёрное окно консоли
    runner = Path(sys.executable).with_name("pythonw.exe")
    if not runner.exists():
        runner = Path(sys.executable)
    return f'"{runner}" "{script}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value = winreg.QueryValueEx(key, RUN_NAME)[0]
            return bool(value)
    except OSError:
        return False


def set_autostart(enable: bool) -> tuple[bool, str]:
    """Добавляет или убирает приложение из автозапуска текущего пользователя."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enable:
                command = _launch_command()
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
                log(f"Автозапуск включён: {command}")
                return True, ("Приложение будет запускаться вместе с Windows. "
                              "Права администратора при таком старте не запрашиваются — "
                              "Windows не умеет это делать из автозапуска, поэтому "
                              "системные твики до нажатия «Перезапустить как админ» "
                              "будут недоступны.")
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
            log("Автозапуск выключен")
            return True, "Приложение убрано из автозапуска."
    except OSError as exc:
        return False, f"Не удалось изменить автозапуск: {exc}"


# ---------------------------------------------------------------- обслуживание данных


def data_usage() -> dict:
    """Сколько места занимают данные приложения."""
    def size_of(path: Path) -> int:
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        for root, _dirs, files in os.walk(path, onerror=lambda e: None):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total

    log_size = size_of(LOG_FILE)
    snap_size = size_of(SNAPSHOT_DIR)
    total = size_of(APP_DIR)
    snapshots = len(list(SNAPSHOT_DIR.glob("*.json"))) if SNAPSHOT_DIR.exists() else 0
    return {
        "folder": str(APP_DIR),
        "total": total,
        "total_text": human_bytes(total),
        "log_text": human_bytes(log_size),
        "snapshots_text": human_bytes(snap_size),
        "snapshots": snapshots,
    }


def clear_log() -> tuple[bool, str]:
    try:
        LOG_FILE.write_text("", encoding="utf-8")
        return True, "Журнал очищен."
    except OSError as exc:
        return False, f"Не удалось очистить журнал: {exc}"


def open_data_folder() -> None:
    try:
        os.startfile(str(APP_DIR))
    except OSError as exc:
        log(f"Не удалось открыть папку данных: {exc}")


def export_config(target: Path) -> tuple[bool, str]:
    """Сохраняет конфиг и снимки рядом, чтобы перенести на другой компьютер."""
    try:
        target = Path(target)
        if target.suffix.lower() == ".zip":
            target = target.with_suffix("")
        archive = shutil.make_archive(str(target), "zip", root_dir=APP_DIR)
        return True, f"Настройки сохранены: {archive}"
    except (OSError, shutil.Error) as exc:
        return False, f"Не удалось выгрузить настройки: {exc}"


def import_config(source: Path) -> tuple[bool, str]:
    """Разворачивает ранее выгруженный архив настроек."""
    try:
        shutil.unpack_archive(str(source), str(APP_DIR))
        return True, ("Настройки загружены. Перезапусти приложение, чтобы они "
                      "вступили в силу.")
    except (OSError, shutil.ReadError, ValueError) as exc:
        return False, f"Не удалось загрузить настройки: {exc}"


def reset_all(keep_snapshots: bool = True) -> tuple[bool, str]:
    """Сбрасывает настройки приложения к заводским.

    Снимки системы по умолчанию сохраняются: без них нечем откатывать
    изменения, внесённые в Windows.
    """
    try:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        if not keep_snapshots and SNAPSHOT_DIR.exists():
            shutil.rmtree(SNAPSHOT_DIR, ignore_errors=True)
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        log("Настройки приложения сброшены")
        return True, ("Настройки приложения сброшены. Снимки системы "
                      + ("сохранены." if keep_snapshots else "удалены.")
                      + " Перезапусти приложение.")
    except OSError as exc:
        return False, f"Не удалось сбросить настройки: {exc}"
