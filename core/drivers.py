"""Драйверы устройств: что стоит, когда выпущено, что пора обновить.

Windows знает дату и версию каждого драйвера, но показывает их в диспетчере
устройств по одному, в пяти кликах от списка. Здесь всё сразу и с оценкой
возраста: для игрового компьютера важнее всего свежесть драйвера видеокарты,
сетевой карты и чипсета.

Приложение ничего не скачивает и не устанавливает само: подсовывать драйверы
из интернета — верный способ убить систему. Оно показывает возраст и даёт
прямую ссылку на официальную страницу производителя.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from .util import log, ps_json

# Классы устройств, которые реально влияют на игры и стабильность
_IMPORTANT = {
    "display": ("Видеокарта", 1,
                "Самый важный драйвер для игр: свежая версия часто даёт "
                "прирост кадров в новых играх и чинит вылеты."),
    "net": ("Сеть", 2,
            "Влияет на задержку и стабильность соединения. Обновляется редко, "
            "но старый драйвер Wi-Fi — частая причина скачков пинга."),
    "system": ("Чипсет и система", 3,
               "Драйверы чипсета отвечают за питание и распределение нагрузки "
               "по ядрам. На процессорах AMD это заметно влияет на кадры."),
    "media": ("Звук", 4,
              "Кривой звуковой драйвер — самая частая причина микрофризов "
              "и щелчков в играх."),
    "hdc": ("Контроллеры дисков", 5,
            "Отвечают за скорость накопителей."),
    "usb": ("USB", 6, "Влияет на опрос мыши и клавиатуры."),
    "mouse": ("Мышь", 7, "Драйвер устройства ввода."),
    "keyboard": ("Клавиатура", 8, "Драйвер устройства ввода."),
}

# Официальные страницы загрузки — только производители, ничего постороннего
VENDOR_LINKS = {
    "nvidia": ("NVIDIA", "https://www.nvidia.com/Download/index.aspx"),
    "amd": ("AMD", "https://www.amd.com/en/support"),
    "intel": ("Intel", "https://www.intel.com/content/www/us/en/download-center/home.html"),
    "realtek": ("Realtek", "https://www.realtek.com/downloads"),
}


@dataclass
class DriverInfo:
    """Один драйвер устройства."""
    device: str
    provider: str
    version: str
    date: str                    # дд.мм.гггг
    days_old: int
    group: str                   # человеческое название класса
    priority: int = 9
    why: str = ""

    @property
    def years_old(self) -> float:
        return round(self.days_old / 365, 1)

    @property
    def verdict(self) -> tuple[str, str]:
        """Оценка свежести и цветовой ключ."""
        # Для видеокарты планка жёстче: NVIDIA и AMD выпускают версии ежемесячно
        if self.group == "Видеокарта":
            if self.days_old <= 90:
                return "свежий", "ok"
            if self.days_old <= 210:
                return "стареет", "warn"
            return "устарел", "danger"
        if self.days_old <= 730:
            return "нормально", "ok"
        if self.days_old <= 1460:
            return "старый", "warn"
        return "очень старый", "danger"

    @property
    def vendor_key(self) -> str:
        low = f"{self.provider} {self.device}".lower()
        for key in VENDOR_LINKS:
            if key in low:
                return key
        return ""


# Все встроенные в Windows драйверы помечены одной и той же датой-заглушкой.
# Это не «драйвер 2006 года», а признак «поставляется вместе с системой».
INBOX_DATE = datetime.date(2006, 6, 21)

# Виртуальные адаптеры, которые вообще не являются железом
_VIRTUAL = re.compile(
    r"(wan miniport|virtual|kernel debug|teredo|loopback|"
    r"personal area network|microsoft ip|tap-|vpn|hyper-v|wintun|wireguard|"
    r"software device|remote desktop)",
    re.IGNORECASE)


def _parse_date(value) -> datetime.date | None:
    """Дата драйвера приходит в разных форматах в зависимости от версии PowerShell."""
    if not value:
        return None
    text = str(value)
    match = re.search(r"/Date\((\d+)", text)
    if match:
        try:
            return datetime.datetime.fromtimestamp(int(match.group(1)) / 1000).date()
        except (ValueError, OSError):
            return None
    # WMI-формат: 20240115000000.000000-000
    match = re.match(r"(\d{4})(\d{2})(\d{2})", text)
    if match:
        try:
            return datetime.date(int(match.group(1)), int(match.group(2)),
                                 int(match.group(3)))
        except ValueError:
            return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "")).date()
    except ValueError:
        return None


def read_drivers() -> list[DriverInfo]:
    """Читает драйверы важных классов устройств."""
    classes = "','".join(_IMPORTANT)
    script = (
        "Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.DeviceClass -and ($_.DeviceClass.ToLower() -in @('{classes}')) "
        "-and $_.DriverVersion } | "
        "Select-Object DeviceName,DriverProviderName,DriverVersion,DriverDate,DeviceClass")
    rows = ps_json(script, timeout=60)
    if not rows:
        log("Список драйверов получить не удалось")
        return []
    if isinstance(rows, dict):
        rows = [rows]

    today = datetime.date.today()
    result: list[DriverInfo] = []
    seen: set[tuple] = set()
    for row in rows:
        name = str(row.get("DeviceName") or "").strip()
        provider = str(row.get("DriverProviderName") or "").strip()
        version = str(row.get("DriverVersion") or "").strip()
        klass = str(row.get("DeviceClass") or "").lower()
        if not name or not version:
            continue
        # Виртуальные адаптеры железом не являются — обновлять там нечего
        if _VIRTUAL.search(name):
            continue
        key = (name.lower(), version)
        if key in seen:
            continue
        seen.add(key)

        date = _parse_date(row.get("DriverDate"))
        if not date:
            continue
        # Встроенный драйвер Windows: обновляется вместе с системой, а не отдельно
        if date == INBOX_DATE:
            continue
        group, priority, why = _IMPORTANT.get(klass, ("Прочее", 9, ""))
        result.append(DriverInfo(
            device=name, provider=provider, version=version,
            date=date.strftime("%d.%m.%Y"), days_old=(today - date).days,
            group=group, priority=priority, why=why))

    result.sort(key=lambda d: (d.priority, -d.days_old))
    return result


def outdated(drivers: list[DriverInfo]) -> list[DriverInfo]:
    """Только те, что стоит обновить."""
    return [d for d in drivers if d.verdict[1] in ("warn", "danger")]


def summary(drivers: list[DriverInfo]) -> str:
    if not drivers:
        return "Список драйверов получить не удалось."
    stale = outdated(drivers)
    gpu = next((d for d in drivers if d.group == "Видеокарта"), None)
    parts = [f"Найдено драйверов: {len(drivers)}"]
    if gpu:
        state, _tone = gpu.verdict
        parts.append(f"видеокарта — версия {gpu.version} от {gpu.date} ({state})")
    if stale:
        parts.append(f"стоит обновить: {len(stale)}")
    else:
        parts.append("все свежие")
    return " · ".join(parts) + "."
