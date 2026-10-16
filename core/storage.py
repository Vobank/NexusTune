"""Накопители: здоровье по SMART и анализ занятого места.

Здоровье читается через счётчики надёжности Windows — то же, что показывают
платные утилиты вроде CrystalDiskInfo. Анализатор места отвечает на вопрос
«куда делись 200 гигабайт»: показывает самые толстые папки по уровням.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .util import log, ps_json


# ================================================================ здоровье дисков


@dataclass
class DriveHealth:
    """Состояние одного физического накопителя."""
    name: str = ""
    media_type: str = ""
    size_gb: int = 0
    health: str = ""                 # Healthy / Warning / Unhealthy
    bus: str = ""
    temperature: int = 0
    temperature_max: int = 0
    power_on_hours: int = 0
    wear_percent: int = 0
    read_errors: int = 0
    write_errors: int = 0
    start_stop_cycles: int = 0

    @property
    def healthy(self) -> bool:
        return self.health in ("Healthy", "0", "")

    @property
    def years_on(self) -> float:
        return round(self.power_on_hours / 8760.0, 1) if self.power_on_hours else 0.0

    @property
    def remaining_percent(self) -> int:
        """Сколько ресурса осталось. Для SSD износ считается в процентах."""
        return max(0, 100 - self.wear_percent)

    def verdict(self) -> tuple[str, str]:
        """Словесная оценка и пояснение."""
        if not self.healthy:
            return "bad", ("Windows сообщает о проблеме с диском. "
                           "Скопируй важные файлы на другой носитель прямо сейчас.")
        if self.read_errors or self.write_errors:
            return "warn", (f"Зафиксированы ошибки: чтения {self.read_errors}, "
                            f"записи {self.write_errors}. Понаблюдай за диском.")
        if self.wear_percent >= 80:
            return "bad", (f"Израсходовано {self.wear_percent}% ресурса записи. "
                           "Диск близок к концу жизни, пора планировать замену.")
        if self.wear_percent >= 50:
            return "warn", (f"Израсходовано {self.wear_percent}% ресурса записи. "
                            "Ещё поработает, но держи это в голове.")
        if self.temperature >= 70:
            return "warn", (f"Температура {self.temperature} °C — жарковато для накопителя. "
                            "NVMe под нагрузкой снижает скорость, чтобы не перегреться. "
                            "Помогает радиатор или обдув.")
        if self.temperature >= 60:
            return "warn", (f"{self.temperature} °C — терпимо, но радиатор бы не помешал.")
        return "ok", "Диск здоров."


def read_drives() -> list[DriveHealth]:
    """Здоровье всех физических накопителей."""
    rows = ps_json(
        "Get-PhysicalDisk | ForEach-Object { "
        "$counter = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
        "[PSCustomObject]@{ "
        "Name = $_.FriendlyName; MediaType = $_.MediaType; Size = $_.Size; "
        "Health = $_.HealthStatus; Bus = $_.BusType; "
        "Temperature = $counter.Temperature; TemperatureMax = $counter.TemperatureMax; "
        "PowerOnHours = $counter.PowerOnHours; Wear = $counter.Wear; "
        "ReadErrors = $counter.ReadErrorsTotal; WriteErrors = $counter.WriteErrorsTotal; "
        "StartStop = $counter.StartStopCycleCount } }", timeout=60)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    def num(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    drives = []
    for row in rows:
        media = str(row.get("MediaType") or "")
        media_names = {"3": "HDD", "4": "SSD", "5": "SCM", "0": "неизвестно"}
        bus_names = {7: "USB", 8: "RAID", 11: "SATA", 17: "NVMe", 12: "SAS"}
        drives.append(DriveHealth(
            name=str(row.get("Name") or "Диск").strip(),
            media_type=media_names.get(media, media or "неизвестно"),
            size_gb=round(num(row.get("Size")) / (1024 ** 3)),
            health=str(row.get("Health") or ""),
            bus=bus_names.get(num(row.get("Bus")), str(row.get("Bus") or "")),
            temperature=num(row.get("Temperature")),
            temperature_max=num(row.get("TemperatureMax")),
            power_on_hours=num(row.get("PowerOnHours")),
            wear_percent=num(row.get("Wear")),
            read_errors=num(row.get("ReadErrors")),
            write_errors=num(row.get("WriteErrors")),
            start_stop_cycles=num(row.get("StartStop")),
        ))
    return drives


def volumes() -> list[dict]:
    """Разделы с занятым и свободным местом."""
    result = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        result.append({
            "device": part.device.rstrip("\\"),
            "fstype": part.fstype,
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "percent": usage.percent,
        })
    return result


# ================================================================ анализ места


@dataclass
class FolderSize:
    path: str
    name: str
    bytes: int = 0
    files: int = 0

    @property
    def gb(self) -> float:
        return round(self.bytes / (1024 ** 3), 2)

    @property
    def mb(self) -> float:
        return round(self.bytes / (1024 ** 2), 1)


class DiskAnalyzer:
    """Считает, что занимает место. Умеет останавливаться на полпути."""

    def __init__(self):
        self._stop = threading.Event()
        self.scanned_files = 0
        self.current_path = ""

    def cancel(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        self._stop.clear()
        self.scanned_files = 0

    def _folder_size(self, path: str) -> tuple[int, int]:
        """Размер папки со всем содержимым: (байты, файлов)."""
        total = 0
        count = 0
        stack = [path]
        while stack and not self._stop.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self._stop.is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                                count += 1
                        except OSError:
                            continue
            except (OSError, PermissionError):
                continue
        self.scanned_files += count
        return total, count

    def top_folders(self, root: str, limit: int = 20, progress=None) -> list[FolderSize]:
        """Самые толстые папки первого уровня внутри указанной."""
        self.reset()
        results: list[FolderSize] = []
        try:
            entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        except (OSError, PermissionError) as exc:
            log(f"Не удалось прочитать {root}: {exc}")
            return []

        for index, entry in enumerate(entries, 1):
            if self._stop.is_set():
                break
            self.current_path = entry.path
            if progress:
                progress(index, len(entries), entry.name)
            size, files = self._folder_size(entry.path)
            if size:
                results.append(FolderSize(path=entry.path, name=entry.name,
                                          bytes=size, files=files))

        # отдельной строкой показываем файлы, лежащие прямо в корне
        loose = 0
        loose_count = 0
        try:
            with os.scandir(root) as items:
                for item in items:
                    try:
                        if item.is_file(follow_symlinks=False):
                            loose += item.stat(follow_symlinks=False).st_size
                            loose_count += 1
                    except OSError:
                        continue
        except OSError:
            pass
        if loose:
            results.append(FolderSize(path=root, name="(файлы в корне)",
                                      bytes=loose, files=loose_count))

        results.sort(key=lambda f: -f.bytes)
        return results[:limit]

    def big_files(self, root: str, min_mb: int = 500, limit: int = 30,
                  progress=None) -> list[FolderSize]:
        """Крупные файлы: обычно это забытые образы, архивы и видео."""
        self.reset()
        found: list[FolderSize] = []
        threshold = min_mb * 1024 * 1024
        stack = [root]
        checked = 0

        while stack and not self._stop.is_set():
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if self._stop.is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                size = entry.stat(follow_symlinks=False).st_size
                                checked += 1
                                if progress and checked % 4000 == 0:
                                    progress(checked, 0, entry.name)
                                if size >= threshold:
                                    found.append(FolderSize(
                                        path=entry.path, name=entry.name,
                                        bytes=size, files=1))
                        except OSError:
                            continue
            except (OSError, PermissionError):
                continue

        found.sort(key=lambda f: -f.bytes)
        return found[:limit]


def suggest_targets() -> list[tuple[str, str]]:
    """Где обычно прячется занятое место."""
    user = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    targets = [
        ("Весь диск C:", "C:\\"),
        ("Папка пользователя", str(user)),
        ("Загрузки", str(user / "Downloads")),
        ("Данные приложений", local),
        ("Program Files", r"C:\Program Files"),
    ]
    return [(name, path) for name, path in targets if path and Path(path).exists()]
