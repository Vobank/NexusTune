"""Анализ загрузки Windows по её собственному журналу производительности.

Windows умеет измерять каждый свой старт: сколько заняла загрузка целиком,
сколько ушло до появления рабочего стола и — самое ценное — какие именно
приложения, драйверы и службы её замедлили. Эти данные лежат в журнале
Microsoft-Windows-Diagnostics-Performance/Operational.

Загвоздка: на многих сборках Windows 11 этот журнал выключен, и данные просто
не пишутся. Тогда модуль честно говорит об этом и предлагает включить журнал —
после чего замеры начнут появляться со следующей перезагрузки.

События, которые нас интересуют:
  100 — итог загрузки: общее время, основной путь, время после рабочего стола
  101 — приложение замедлило загрузку (с именем!)
  102 — драйвер замедлил загрузку
  103 — служба замедлила загрузку
  109 — устройство задержало запуск
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import is_admin, log, ps_json, run, run_ps

LOG_NAME = "Microsoft-Windows-Diagnostics-Performance/Operational"

# Что означает каждый код события-замедлителя
_CULPRIT_KINDS = {
    101: ("приложение", "Программа из автозагрузки стартовала дольше обычного."),
    102: ("драйвер", "Драйвер устройства инициализировался дольше обычного."),
    103: ("служба", "Служба Windows запускалась дольше обычного."),
    106: ("фоновая задача", "Задача планировщика замедлила запуск."),
    109: ("устройство", "Устройство отвечало дольше обычного при инициализации."),
}


@dataclass
class BootRecord:
    """Один замер загрузки системы."""
    when: str = ""
    total_ms: int = 0            # полное время до готовности
    main_ms: int = 0             # основной путь: ядро, драйверы, вход
    post_ms: int = 0             # после появления рабочего стола

    @property
    def total_s(self) -> float:
        return round(self.total_ms / 1000, 1)

    @property
    def verdict(self) -> tuple[str, str]:
        """Оценка и цветовой ключ."""
        seconds = self.total_s
        if seconds <= 25:
            return "быстро", "ok"
        if seconds <= 45:
            return "нормально", "ok"
        if seconds <= 80:
            return "медленно", "warn"
        return "очень медленно", "danger"


@dataclass
class Culprit:
    """Кто именно замедлил загрузку."""
    kind: str
    name: str
    ms: int
    explanation: str
    count: int = 1

    @property
    def seconds(self) -> float:
        return round(self.ms / 1000, 1)


@dataclass
class BootReport:
    available: bool = False
    enabled: bool = False
    note: str = ""
    records: list = field(default_factory=list)
    culprits: list = field(default_factory=list)

    @property
    def average_s(self) -> float:
        if not self.records:
            return 0.0
        return round(sum(r.total_ms for r in self.records) / len(self.records) / 1000, 1)

    @property
    def best_s(self) -> float:
        return min((r.total_s for r in self.records), default=0.0)

    @property
    def worst_s(self) -> float:
        return max((r.total_s for r in self.records), default=0.0)

    @property
    def trend(self) -> str:
        """Загрузка ускоряется или замедляется со временем."""
        if len(self.records) < 4:
            return ""
        # записи приходят от новых к старым
        recent = self.records[:len(self.records) // 2]
        older = self.records[len(self.records) // 2:]
        new_avg = sum(r.total_ms for r in recent) / len(recent)
        old_avg = sum(r.total_ms for r in older) / len(older)
        delta = (new_avg - old_avg) / 1000
        if abs(delta) < 2:
            return "Время загрузки держится ровно."
        if delta > 0:
            return (f"Загрузка замедлилась примерно на {delta:.0f} с по сравнению "
                    "с более ранними запусками — обычно это новые программы "
                    "в автозагрузке.")
        return f"Загрузка ускорилась примерно на {abs(delta):.0f} с. "


def log_enabled() -> bool | None:
    """Включён ли журнал производительности. None — не удалось узнать."""
    data = ps_json(
        f"Get-WinEvent -ListLog '{LOG_NAME}' -ErrorAction SilentlyContinue | "
        "Select-Object IsEnabled,RecordCount", timeout=20)
    if data is None:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    return bool(data.get("IsEnabled"))


def enable_log() -> tuple[bool, str]:
    """Включает журнал производительности Windows (нужны права администратора)."""
    if not is_admin():
        return False, "Нужны права администратора."
    code, _out, err = run(["wevtutil", "sl", LOG_NAME, "/e:true"], timeout=25)
    if code == 0:
        log("Журнал производительности загрузки включён")
        return True, ("Журнал включён. Данные о загрузке появятся после следующей "
                      "перезагрузки — Windows измеряет старт, а не текущую сессию.")
    return False, err or "Windows отклонила включение журнала."


def _read_events(event_id: int, limit: int) -> list[dict]:
    """Читает события и разбирает их поля в словари."""
    script = (
        f"$ev = Get-WinEvent -FilterHashtable @{{LogName='{LOG_NAME}'; "
        f"Id={event_id}}} -MaxEvents {limit} -ErrorAction SilentlyContinue; "
        "if (-not $ev) { '[]' } else { "
        "$ev | ForEach-Object { "
        "$x = [xml]$_.ToXml(); $row = @{ When = $_.TimeCreated.ToString('dd.MM.yyyy HH:mm') }; "
        "foreach ($d in $x.Event.EventData.Data) { $row[$d.Name] = $d.'#text' }; "
        "[pscustomobject]$row } }")
    data = ps_json(script, timeout=45)
    if not data:
        return []
    return data if isinstance(data, list) else [data]


def _int(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def read_boot_report(limit: int = 12) -> BootReport:
    """Собирает отчёт о последних загрузках системы."""
    report = BootReport()
    enabled = log_enabled()

    if enabled is None:
        report.note = ("Журнал производительности недоступен. Возможно, он отключён "
                       "политикой или не поддерживается этой сборкой Windows.")
        return report
    report.enabled = enabled
    if not enabled:
        report.note = (
            "Журнал производительности Windows выключен, поэтому время загрузки "
            "не записывается. Это частая ситуация на Windows 11. Включи его — "
            "и со следующей перезагрузки здесь появятся замеры: сколько заняла "
            "загрузка и какие именно программы, драйверы и службы её тормозили.")
        return report

    for row in _read_events(100, limit):
        record = BootRecord(
            when=str(row.get("When") or ""),
            total_ms=_int(row, "BootTime"),
            main_ms=_int(row, "MainPathBootTime"),
            post_ms=_int(row, "BootPostBootTime"))
        if record.total_ms:
            report.records.append(record)

    merged: dict[tuple, Culprit] = {}
    for event_id, (kind, explanation) in _CULPRIT_KINDS.items():
        for row in _read_events(event_id, 40):
            name = (row.get("Name") or row.get("FileName")
                    or row.get("FriendlyName") or "").strip()
            ms = (_int(row, "TotalTime") or _int(row, "StartTime")
                  or _int(row, "Degradation") or _int(row, "InitTime"))
            if not name or ms <= 0:
                continue
            key = (kind, name.lower())
            if key in merged:
                existing = merged[key]
                existing.count += 1
                existing.ms = max(existing.ms, ms)
            else:
                merged[key] = Culprit(kind=kind, name=name, ms=ms,
                                      explanation=explanation)

    report.culprits = sorted(merged.values(), key=lambda c: -c.ms)[:12]
    report.available = bool(report.records)
    if not report.available:
        report.note = ("Журнал включён, но записей о загрузке ещё нет. "
                       "Они появятся после следующей перезагрузки.")
    return report


def uptime_summary() -> dict:
    """Время последней загрузки и сколько компьютер уже работает."""
    data = ps_json(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object LastBootUpTime,@{n='Now';e={Get-Date}}", timeout=20)
    result = {"last_boot": "", "uptime_hours": 0.0}
    if not data:
        return result
    if isinstance(data, list):
        data = data[0] if data else {}

    import datetime
    import re as _re

    def parse(value) -> datetime.datetime | None:
        if not value:
            return None
        text = str(value)
        # PowerShell отдаёт дату как /Date(1699999999999)/ либо как строку ISO
        match = _re.search(r"/Date\((\d+)", text)
        if match:
            return datetime.datetime.fromtimestamp(int(match.group(1)) / 1000)
        try:
            return datetime.datetime.fromisoformat(text.replace("Z", ""))
        except ValueError:
            return None

    boot = parse(data.get("LastBootUpTime"))
    now = parse(data.get("Now")) or datetime.datetime.now()
    if boot:
        result["last_boot"] = boot.strftime("%d.%m.%Y %H:%M")
        result["uptime_hours"] = round((now - boot).total_seconds() / 3600, 1)
    return result


def fast_startup_enabled() -> bool:
    """Включён ли «быстрый запуск» — он подменяет честную загрузку гибернацией."""
    from .optimizer import HKLM, reg_read
    value = reg_read(
        HKLM, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
        "HiberbootEnabled")
    return value == 1
