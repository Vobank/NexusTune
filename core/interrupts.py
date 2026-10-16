"""ПРЕРЫВАНИЯ — управление тем, какие ядра дёргает железо.

Сначала неприятная правда, потому что она важнее любой функции.

В интернете гуляет идея «заморозить ISR и DPC до нуля ради разгона». Это
невозможно и означало бы мгновенную смерть системы. Прерывания — это способ,
которым железо вообще разговаривает с процессором: по прерыванию приходит
движение мыши, нажатие клавиши, готовый кадр от видеокарты, тик системного
таймера. Обнулить их — это не «освободить мегагерцы», это выключить мышь,
клавиатуру, звук, сеть и часы одновременно. Любая программа, которая обещает
такое, либо ничего не делает, либо ломает систему.

Что можно и нужно делать по-настоящему — и что делает этот модуль:

  1. РАЗВЕСТИ прерывания и игру по разным ядрам. Прерывания устройства всегда
     обрабатываются на каком-то ядре. Если это то же ядро, где считает игра,
     её поток прерывают тысячи раз в секунду — отсюда микрорывки. Windows
     позволяет задать, каким ядрам разрешено обрабатывать прерывания
     конкретного устройства. Это настоящий, поддерживаемый механизм
     (DevicePolicy и AssignmentSetOverride в ветке устройства).

  2. ПОКАЗАТЬ, кто именно дёргает процессор. Счётчики Windows отдают число
     прерываний и отложенных вызовов (DPC) в секунду и время, проведённое
     в них. Резкий рост — это почти всегда конкретный драйвер.

  3. ВКЛЮЧИТЬ MSI-режим, если устройство его умеет: прерывания сообщением по
     шине вместо старой общей линии IRQ. Это убирает конфликты между
     устройствами и уже есть на странице «Эксперт» для видеокарты.

Всё обратимо: настройка сходства — это значение в реестре, удаление которого
возвращает поведение Windows по умолчанию.
"""
from __future__ import annotations

import re
import winreg
from dataclasses import dataclass, field

from .cpuinfo import read_topology
from .pdh import Counter
from .util import is_admin, log, ps_json

HKLM = winreg.HKEY_LOCAL_MACHINE

# Ветка настроек прерываний внутри записи устройства
_AFFINITY_SUFFIX = (r"\Device Parameters\Interrupt Management"
                    r"\Affinity Policy")
_MSI_SUFFIX = (r"\Device Parameters\Interrupt Management"
               r"\MessageSignaledInterruptProperties")

# Политики назначения прерываний (DevicePolicy)
POLICY_DEFAULT = 0        # решает Windows
POLICY_ALL_CLOSE = 1      # любое ядро рядом
POLICY_ONE_CLOSE = 2      # одно ядро рядом
POLICY_ALL = 3            # любое ядро
POLICY_SPECIFIED = 4      # только указанные ядра — то, что нам нужно
POLICY_SPREAD = 5         # распределять

POLICY_NAMES = {
    POLICY_DEFAULT: "как решит Windows",
    POLICY_ALL_CLOSE: "любое ближайшее ядро",
    POLICY_ONE_CLOSE: "одно ближайшее ядро",
    POLICY_ALL: "любое ядро",
    POLICY_SPECIFIED: "только заданные ядра",
    POLICY_SPREAD: "распределять по ядрам",
}

# Классы устройств, чьи прерывания реально влияют на игру
_INTERESTING = {
    "Display": ("Видеокарта", 1,
                "Сообщает о готовых кадрах. Её прерывания идут постоянно "
                "и в самый неподходящий момент."),
    "Net": ("Сеть", 2,
            "Каждый пакет — прерывание. На быстром канале это десятки тысяч "
            "прерываний в секунду."),
    "USB": ("USB", 3,
            "Мышь с частотой опроса 1000 Гц даёт тысячу прерываний в секунду "
            "только от себя."),
    "MEDIA": ("Звук", 4,
              "Звуковая карта требует, чтобы её обслуживали вовремя, иначе "
              "будут щелчки."),
    "SCSIAdapter": ("Контроллер накопителей", 5,
                    "Прерывания при чтении с диска — заметны при подгрузке "
                    "уровней."),
    "HDC": ("Контроллер дисков", 6, "То же самое для дисковых контроллеров."),
}


@dataclass
class Device:
    """Устройство, чьи прерывания можно развести по ядрам."""
    name: str
    device_id: str
    klass: str
    group: str
    priority: int
    why: str
    reg_path: str = ""
    policy: int = POLICY_DEFAULT
    mask: int = 0
    msi: bool | None = None

    @property
    def policy_name(self) -> str:
        return POLICY_NAMES.get(self.policy, "неизвестно")

    @property
    def pinned(self) -> bool:
        return self.policy == POLICY_SPECIFIED and self.mask > 0


def _reg_path_for(device_id: str) -> str:
    """Путь в реестре к записи устройства."""
    return r"SYSTEM\CurrentControlSet\Enum\\" + device_id


def _read_dword(path: str, name: str):
    try:
        with winreg.OpenKey(HKLM, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _read_qword(path: str, name: str):
    return _read_dword(path, name)


def read_devices() -> list[Device]:
    """Устройства, чьи прерывания имеет смысл разводить."""
    classes = "','".join(_INTERESTING)
    rows = ps_json(
        "Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.PNPClass -in @('{classes}') -and $_.Status -eq 'OK' "
        "-and $_.DeviceID -like 'PCI*' } | "
        "Select-Object Name,PNPClass,DeviceID", timeout=45)
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return []

    devices: list[Device] = []
    for row in rows:
        device_id = str(row.get("DeviceID") or "")
        klass = str(row.get("PNPClass") or "")
        if not device_id or klass not in _INTERESTING:
            continue
        group, priority, why = _INTERESTING[klass]
        path = _reg_path_for(device_id)

        device = Device(
            name=str(row.get("Name") or "устройство").strip(),
            device_id=device_id, klass=klass, group=group,
            priority=priority, why=why, reg_path=path)

        policy = _read_dword(path + _AFFINITY_SUFFIX, "DevicePolicy")
        device.policy = int(policy) if policy is not None else POLICY_DEFAULT
        mask = _read_qword(path + _AFFINITY_SUFFIX, "AssignmentSetOverride")
        if isinstance(mask, bytes):
            mask = int.from_bytes(mask, "little")
        device.mask = int(mask) if mask else 0

        msi = _read_dword(path + _MSI_SUFFIX, "MSISupported")
        device.msi = None if msi is None else bool(msi)
        devices.append(device)

    devices.sort(key=lambda d: (d.priority, d.name.lower()))
    return devices


def set_affinity(device: Device, mask: int) -> tuple[bool, str]:
    """Разрешает обрабатывать прерывания устройства только заданным ядрам.

    mask = 0 снимает ограничение и возвращает поведение Windows по умолчанию.
    """
    if not is_admin():
        return False, "Нужны права администратора."

    path = device.reg_path + _AFFINITY_SUFFIX
    try:
        with winreg.CreateKeyEx(HKLM, path, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            if mask <= 0:
                for name in ("DevicePolicy", "AssignmentSetOverride"):
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                device.policy = POLICY_DEFAULT
                device.mask = 0
                log(f"Прерывания {device.name}: ограничение снято")
                return True, ("Ограничение снято. Windows снова сама решает, "
                              "какое ядро обрабатывает прерывания этого "
                              "устройства. Нужна перезагрузка.")

            # AssignmentSetOverride — двоичное значение, младший байт первым
            length = max(1, (mask.bit_length() + 7) // 8)
            winreg.SetValueEx(key, "AssignmentSetOverride", 0, winreg.REG_BINARY,
                              mask.to_bytes(length, "little"))
            winreg.SetValueEx(key, "DevicePolicy", 0, winreg.REG_DWORD,
                              POLICY_SPECIFIED)
    except PermissionError:
        return False, "Нужны права администратора."
    except OSError as exc:
        return False, f"Не удалось записать настройку: {exc}"

    device.policy = POLICY_SPECIFIED
    device.mask = mask
    cores = [str(i) for i in range(64) if mask & (1 << i)]
    log(f"Прерывания {device.name} -> ядра {', '.join(cores)}")
    return True, (f"Прерывания устройства закреплены за ядрами "
                  f"{', '.join(cores)}. Изменение вступит в силу после "
                  "перезагрузки.")


def suggest_mask(topology=None) -> tuple[int, str]:
    """Какие ядра отдать под прерывания, чтобы не мешать игре.

    Логика: игра идёт на производительных ядрах, значит прерывания надо
    увести на энергоэффективные. Если процессор однородный — отдаём
    последнюю пару ядер, а игре остаётся всё остальное.
    """
    topology = topology or read_topology()
    if not topology.cores:
        return 0, "Топологию процессора прочитать не удалось."

    if topology.hybrid and topology.efficient_mask:
        cores = [str(i) for i in range(topology.logical_count)
                 if topology.efficient_mask & (1 << i)]
        return topology.efficient_mask, (
            f"Прерывания уйдут на энергоэффективные ядра ({', '.join(cores)}), "
            "а производительные останутся игре. Это и есть смысл разделения: "
            "поток игры перестанут прерывать тысячи раз в секунду.")

    # однородный процессор: отдаём два последних потока
    total = topology.logical_count
    if total < 4:
        return 0, ("Ядер слишком мало: разводить прерывания на таком "
                   "процессоре вредно — они начнут мешать друг другу.")
    mask = (1 << (total - 1)) | (1 << (total - 2))
    return mask, (f"Прерывания уйдут на потоки {total - 2} и {total - 1}, "
                  f"игре останутся потоки 0-{total - 3}.")


# ---------------------------------------------------------------- живые счётчики


class InterruptLoad:
    """Живая нагрузка от прерываний и отложенных вызовов.

    Это те самые счётчики, по которым видно, что драйвер ведёт себя плохо:
    время в прерываниях и в DPC растёт, а полезная работа стоит.
    """

    def __init__(self):
        self.interrupts = Counter(r"\Processor(_Total)\Interrupts/sec")
        self.dpc_rate = Counter(r"\Processor(_Total)\DPCs Queued/sec")
        self.dpc_time = Counter(r"\Processor(_Total)\% DPC Time")
        self.interrupt_time = Counter(r"\Processor(_Total)\% Interrupt Time")

    @property
    def available(self) -> bool:
        return self.interrupts.available and self.dpc_time.available

    def read(self) -> dict:
        """Текущие значения. None внутри означает «ещё нет второго замера»."""
        return {
            "interrupts_sec": self.interrupts.read(),
            "dpc_sec": self.dpc_rate.read(),
            "dpc_percent": self.dpc_time.read(),
            "interrupt_percent": self.interrupt_time.read(),
        }

    @staticmethod
    def verdict(values: dict) -> tuple[str, str]:
        """Оценка и цветовой ключ по текущим значениям."""
        dpc = values.get("dpc_percent")
        isr = values.get("interrupt_percent")
        if dpc is None or isr is None:
            return "жду второй замер", "dim"

        total = dpc + isr
        if total >= 12:
            return ("Прерывания съедают больше десятой части процессора — "
                    "это уже причина микрорывков"), "danger"
        if total >= 6:
            return ("Заметная нагрузка от прерываний: стоит развести их "
                    "с игровыми ядрами"), "warn"
        return "Нагрузка от прерываний в норме", "ok"


def summary(devices: list[Device]) -> str:
    """Короткий итог по устройствам."""
    if not devices:
        return ("Устройства с настраиваемыми прерываниями не найдены. "
                "Такое бывает на ноутбуках и в виртуальных машинах.")
    pinned = sum(1 for d in devices if d.pinned)
    msi_on = sum(1 for d in devices if d.msi)
    text = f"Устройств на шине PCI: {len(devices)}"
    if pinned:
        text += f" · закреплено за ядрами {pinned}"
    if msi_on:
        text += f" · в режиме MSI {msi_on}"
    return text + "."
