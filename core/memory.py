"""Виртуальная память: файл подкачки, очистка ОЗУ и RAM-диск."""
from __future__ import annotations

import ctypes
import shutil
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

from .util import is_admin, log, ps_json, run, run_ps

# ---------------------------------------------------------------- файл подкачки


@dataclass
class PagefileState:
    automatic: bool = True
    entries: list = None            # [{drive, initial_mb, maximum_mb}]
    current_mb: int = 0             # реально выделенный размер

    @property
    def summary(self) -> str:
        if self.automatic:
            return "Управляется системой"
        if not self.entries:
            return "Отключён"
        return ", ".join(
            f"{e['drive']} {e['initial_mb']}-{e['maximum_mb']} МБ" for e in self.entries)


_PAGEFILE_SCRIPT = """
$out = @{}
$out.automatic = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).AutomaticManagedPagefile
$out.settings = @(Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
    Select-Object Name,InitialSize,MaximumSize)
$out.allocated = (Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty AllocatedBaseSize)
[pscustomobject]$out
"""


def read_pagefile() -> PagefileState:
    """Текущая конфигурация файла подкачки.

    Раньше здесь было три отдельных запроса, и каждый поднимал свой процесс
    PowerShell: полторы секунды уходило только на запуск, ещё столько же на
    работу — вместе пять с половиной секунд. Запрос тот же, процесс один.
    """
    st = PagefileState(entries=[])
    data = ps_json(_PAGEFILE_SCRIPT, timeout=45)
    if not isinstance(data, dict):
        st.automatic = True
        return st

    auto = data.get("automatic")
    st.automatic = bool(auto) if auto is not None else True

    settings = data.get("settings")
    if isinstance(settings, dict):
        settings = [settings]
    for item in settings or []:
        st.entries.append({
            "drive": str(item.get("Name", "")).split("\\")[0].upper() or "C:",
            "initial_mb": int(item.get("InitialSize") or 0),
            "maximum_mb": int(item.get("MaximumSize") or 0),
        })

    allocated = data.get("allocated")
    if isinstance(allocated, (int, float)):
        st.current_mb = int(allocated)
    return st


def recommend_pagefile(ram_gb: float, free_disk_gb: float,
                       heavy_games: bool = True) -> tuple[int, int, str]:
    """Рекомендуемые размеры (начальный, максимальный, пояснение) в мегабайтах."""
    ram_mb = int(ram_gb * 1024)
    if ram_gb >= 30:
        initial = 8192
        maximum = 16384
        why = ("32 ГБ и больше: огромный файл подкачки не нужен. "
               "8 ГБ хватает под аварийные всплески, 16 ГБ — потолок на случай "
               "тяжёлых игр вроде Tarkov или Cities: Skylines II.")
    elif ram_gb >= 15:
        initial = 8192
        maximum = 20480
        why = ("16 ГБ: подкачка ещё реально используется в требовательных играх. "
               "Фиксированный минимум 8 ГБ убирает паузы на расширение файла.")
    elif ram_gb >= 7:
        initial = 10240
        maximum = 24576
        why = ("8 ГБ ОЗУ: подкачка критична. Крупный фиксированный файл "
               "спасает от вылетов «недостаточно памяти» в современных играх.")
    else:
        initial = ram_mb
        maximum = ram_mb * 3
        why = ("Меньше 8 ГБ ОЗУ: подкачка работает как основной резерв. "
               "Лучшее вложение здесь — докупить планку памяти.")

    # не занимаем больше четверти свободного места на диске
    disk_cap = int(free_disk_gb * 1024 * 0.25)
    if disk_cap > 1024 and maximum > disk_cap:
        maximum = max(initial, disk_cap)
        why += f" Максимум урезан до {maximum} МБ — на диске мало свободного места."

    if not heavy_games:
        initial = min(initial, 4096)
    return initial, maximum, why


def set_pagefile(drive: str, initial_mb: int, maximum_mb: int) -> tuple[bool, str]:
    """Задаёт фиксированный файл подкачки. Требуются права администратора."""
    if not is_admin():
        return False, "Нужны права администратора."
    drive = drive.rstrip("\\").upper()
    if not drive.endswith(":"):
        drive += ":"
    path = f"{drive}\\\\pagefile.sys"

    script = (
        "$cs = Get-CimInstance Win32_ComputerSystem; "
        "if ($cs.AutomaticManagedPagefile) { "
        "Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile=$false} }; "
        f"$pf = Get-CimInstance Win32_PageFileSetting | Where-Object {{ $_.Name -like '{drive}*' }}; "
        "if ($pf) { Set-CimInstance -InputObject $pf -Property "
        f"@{{InitialSize={initial_mb}; MaximumSize={maximum_mb}}} }} "
        f"else {{ New-CimInstance -ClassName Win32_PageFileSetting -Property "
        f"@{{Name='{path}'; InitialSize={initial_mb}; MaximumSize={maximum_mb}}} }}; "
        "'OK'"
    )
    code, out, err = run_ps(script, timeout=60)
    if code == 0 and "OK" in out:
        log(f"Файл подкачки {drive} задан: {initial_mb}-{maximum_mb} МБ")
        return True, "Готово. Изменения вступят в силу после перезагрузки."
    return False, err or out or "Не удалось изменить файл подкачки."


def set_pagefile_automatic() -> tuple[bool, str]:
    """Возвращает управление файлом подкачки системе."""
    if not is_admin():
        return False, "Нужны права администратора."
    code, _o, err = run_ps(
        "$cs = Get-CimInstance Win32_ComputerSystem; "
        "Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile=$true}", timeout=60)
    return (code == 0), ("Управление возвращено системе." if code == 0 else err)


# ---------------------------------------------------------------- очистка памяти

_MEMORY_PURGE_STANDBY_LIST = 4
_MEMORY_EMPTY_WORKING_SETS = 2
_SYSTEM_MEMORY_LIST_INFORMATION = 0x50


def _enable_privilege(name: str) -> bool:
    """Включает привилегию текущему процессу."""
    TOKEN_ADJUST_PRIVILEGES = 0x20
    TOKEN_QUERY = 0x8
    SE_PRIVILEGE_ENABLED = 0x2

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    advapi = ctypes.windll.advapi32
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(ctypes.windll.kernel32.GetCurrentProcess(),
                                   TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token)):
        return False
    luid = LUID()
    if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
        return False
    tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)((luid, SE_PRIVILEGE_ENABLED)))
    ok = advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    return bool(ok) and ctypes.windll.kernel32.GetLastError() == 0


def trim_working_sets() -> tuple[int, int]:
    """Сбрасывает рабочие наборы процессов. Возвращает (обработано, освобождено МБ)."""
    before = psutil.virtual_memory().available
    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    PROCESS_SET_QUOTA = 0x0100
    touched = 0
    for proc in psutil.process_iter(["pid", "name"]):
        pid = proc.info["pid"]
        if pid <= 4:
            continue
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA, False, pid)
        if not handle:
            continue
        try:
            if psapi.EmptyWorkingSet(handle):
                touched += 1
        finally:
            kernel32.CloseHandle(handle)
    freed = max(0, psutil.virtual_memory().available - before)
    return touched, int(freed / (1024 ** 2))


def purge_standby_list() -> tuple[bool, int]:
    """Очищает список ожидания (standby list) — кэш файлов в ОЗУ.

    Именно он обычно занимает «непонятные» гигабайты. Требуются права администратора.
    """
    if not is_admin():
        return False, 0
    if not _enable_privilege("SeProfileSingleProcessPrivilege"):
        return False, 0
    before = psutil.virtual_memory().available
    command = ctypes.c_int(_MEMORY_PURGE_STANDBY_LIST)
    status = ctypes.windll.ntdll.NtSetSystemInformation(
        _SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(command), ctypes.sizeof(command))
    if status != 0:
        log(f"Очистка standby list не удалась, код {status:#x}")
        return False, 0
    freed = max(0, psutil.virtual_memory().available - before)
    return True, int(freed / (1024 ** 2))


def clean_memory() -> dict:
    """Полная очистка: рабочие наборы + список ожидания."""
    before = psutil.virtual_memory()
    touched, _ = trim_working_sets()
    purged, _ = purge_standby_list()
    after = psutil.virtual_memory()
    freed_mb = int(max(0, after.available - before.available) / (1024 ** 2))
    return {
        "processes": touched,
        "standby_purged": purged,
        "freed_mb": freed_mb,
        "before_percent": before.percent,
        "after_percent": after.percent,
    }


def top_memory_hogs(limit: int = 8) -> list[dict]:
    """Процессы, которые едят больше всего памяти."""
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            mem = proc.info["memory_info"]
            if mem:
                rows.append({"pid": proc.info["pid"],
                             "name": proc.info["name"] or "?",
                             "mb": int(mem.rss / (1024 ** 2))})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: -r["mb"])
    return rows[:limit]


# ---------------------------------------------------------------- RAM-диск

IMDISK_EXE = "imdisk.exe"


def imdisk_available() -> bool:
    """Установлен ли драйвер ImDisk (нужен для настоящего RAM-диска)."""
    if shutil.which(IMDISK_EXE):
        return True
    return Path(r"C:\Windows\System32\imdisk.exe").exists()


def free_drive_letters() -> list[str]:
    used = {Path(p.mountpoint).drive.rstrip(":").upper()
            for p in psutil.disk_partitions(all=False) if p.mountpoint}
    return [f"{c}:" for c in "RSTUVWXYZ" if c not in used]


def create_ramdisk(letter: str, size_mb: int) -> tuple[bool, str]:
    """Создаёт RAM-диск через ImDisk."""
    if not imdisk_available():
        return False, ("Нужен драйвер ImDisk — без него Windows не умеет делать RAM-диск. "
                       "Скачай ImDisk Toolkit, установи и вернись сюда.")
    if not is_admin():
        return False, "Нужны права администратора."
    letter = letter.rstrip(":").upper()
    code, out, err = run([IMDISK_EXE, "-a", "-s", f"{size_mb}M", "-m", f"{letter}:",
                          "-p", "/fs:ntfs /q /y"], timeout=120)
    if code == 0:
        log(f"RAM-диск {letter}: создан, {size_mb} МБ")
        return True, f"RAM-диск {letter}: на {size_mb} МБ готов."
    return False, err or out or "ImDisk вернул ошибку."


def remove_ramdisk(letter: str) -> tuple[bool, str]:
    letter = letter.rstrip(":").upper()
    code, out, err = run([IMDISK_EXE, "-D", "-m", f"{letter}:"], timeout=60)
    return (code == 0), (f"RAM-диск {letter}: отключён." if code == 0 else (err or out))
