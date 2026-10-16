"""Список установленных программ и их удаление.

Читаем то же, что показывает «Программы и компоненты»: разделы реестра
Uninstall в трёх местах (машина 64-бит, машина 32-бит, текущий пользователь).
Каждая запись — реальная программа с собственным деинсталлятором; запускаем
именно его, а не удаляем файлы руками, иначе в системе останется хвост.

Магазинные приложения (Store) сюда не попадают — для них есть отдельная
страница со списком безопасных к удалению. Здесь только классические программы.
"""
from __future__ import annotations

import re
import winreg
from dataclasses import dataclass
from datetime import datetime

from .util import human_bytes, is_admin, log, run

HKLM = winreg.HKEY_LOCAL_MACHINE
HKCU = winreg.HKEY_CURRENT_USER

_ROOTS = [
    (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
    (HKLM, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    (HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
]

# Издатели и имена, которые лучше не трогать: драйверы, среды выполнения,
# без которых перестанут запускаться другие программы и игры.
_SYSTEM_HINTS = re.compile(
    r"(microsoft (visual c\+\+|\.net|edge|windows)|"
    r"nvidia (driver|graphics|hd audio|physx)|amd (software|chipset)|intel\(r\)|"
    r"realtek|directx|redistributable|runtime|driver|kb\d{6})",
    re.IGNORECASE)


@dataclass
class Program:
    """Установленная программа."""
    name: str
    version: str
    publisher: str
    uninstall: str            # строка запуска деинсталлятора
    quiet_uninstall: str
    size_bytes: int
    install_date: str
    location: str
    system_like: bool

    @property
    def size_text(self) -> str:
        return human_bytes(self.size_bytes) if self.size_bytes else "—"

    @property
    def date_text(self) -> str:
        if not self.install_date or len(self.install_date) != 8:
            return "—"
        try:
            return datetime.strptime(self.install_date, "%Y%m%d").strftime("%d.%m.%Y")
        except ValueError:
            return "—"

    @property
    def can_silent(self) -> bool:
        return bool(self.quiet_uninstall)


def _read_value(handle, name: str, default=""):
    try:
        return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return default


def list_programs() -> list[Program]:
    """Все классические программы, у которых есть деинсталлятор."""
    seen: dict[str, Program] = {}
    for root, path, access in _ROOTS:
        try:
            base = winreg.OpenKey(root, path, 0,
                                  winreg.KEY_READ | (access or 0))
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, index)
                except OSError:
                    break
                index += 1
                try:
                    handle = winreg.OpenKey(base, sub)
                except OSError:
                    continue
                try:
                    name = str(_read_value(handle, "DisplayName")).strip()
                    uninstall = str(_read_value(handle, "UninstallString")).strip()
                    system_component = _read_value(handle, "SystemComponent", 0)
                    parent = _read_value(handle, "ParentKeyName", "")
                    release = _read_value(handle, "ReleaseType", "")
                    if not name or not uninstall:
                        continue
                    # скрытые компоненты, обновления и части других пакетов
                    if system_component or parent or release in ("Update", "Hotfix",
                                                                 "Security Update"):
                        continue
                    if name.lower() in seen:
                        continue

                    publisher = str(_read_value(handle, "Publisher", "")).strip()
                    try:
                        size = int(_read_value(handle, "EstimatedSize", 0)) * 1024
                    except (TypeError, ValueError):
                        size = 0
                    program = Program(
                        name=name,
                        version=str(_read_value(handle, "DisplayVersion", "")).strip(),
                        publisher=publisher,
                        uninstall=uninstall,
                        quiet_uninstall=str(_read_value(handle, "QuietUninstallString",
                                                        "")).strip(),
                        size_bytes=size,
                        install_date=str(_read_value(handle, "InstallDate", "")).strip(),
                        location=str(_read_value(handle, "InstallLocation", "")).strip(),
                        system_like=bool(_SYSTEM_HINTS.search(f"{name} {publisher}")))
                    seen[name.lower()] = program
                finally:
                    winreg.CloseKey(handle)
        finally:
            winreg.CloseKey(base)

    programs = list(seen.values())
    programs.sort(key=lambda p: p.name.lower())
    return programs


def summary(programs: list[Program]) -> dict:
    """Короткая сводка для шапки страницы."""
    total = len(programs)
    system = sum(1 for p in programs if p.system_like)
    known_size = sum(p.size_bytes for p in programs)
    return {
        "total": total,
        "regular": total - system,
        "system": system,
        "size_text": human_bytes(known_size) if known_size else "—",
    }


def uninstall(program: Program, silent: bool = True) -> tuple[bool, str]:
    """Запускает деинсталлятор программы.

    Тихий режим убирает файлы без вопросов, но не все программы его умеют —
    тогда открывается обычный мастер удаления, и дальше пользователь идёт сам.
    """
    if program.system_like:
        return False, ("Похоже на системный компонент или драйвер. Такие вещи "
                       "приложение удалять не даёт: без них могут перестать "
                       "запускаться игры и другие программы.")

    command = program.quiet_uninstall if (silent and program.quiet_uninstall) \
        else program.uninstall
    if not command:
        return False, "У программы нет строки удаления."

    log(f"Удаление программы: {program.name}")
    args = _split_command(command)
    # деинсталлятор часто сам просит права администратора — запускаем как есть
    code, out, err = run(args, timeout=600)
    text = (out + " " + err).strip()
    if code in (0, 3010, 1641):        # 3010/1641 — успех с просьбой перезагрузиться
        return True, f"«{program.name}» удалена." + (
            " Нужна перезагрузка." if code in (3010, 1641) else "")
    if code == 1602:
        return False, "Удаление отменено."
    return False, text or (
        "Деинсталлятор завершился с ошибкой. Возможно, окно мастера удаления "
        "открылось поверх — заверши удаление в нём.")


def _split_command(command: str) -> list[str]:
    """Разбирает строку запуска деинсталлятора на программу и аргументы."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            exe = command[1:end]
            rest = command[end + 1:].strip()
            return [exe] + rest.split()
    # MsiExec.exe /X{GUID} и подобное — делится по пробелам
    return command.split()
