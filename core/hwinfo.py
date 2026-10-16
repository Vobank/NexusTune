"""Определение конфигурации ПК и живой мониторинг датчиков."""
from __future__ import annotations

import re
import ctypes
import json
import shutil
import time
from dataclasses import dataclass, field, asdict

import psutil

from .util import APP_DIR, ps_json, run, run_ps, log

# ---------------------------------------------------------------- статические данные


@dataclass
class GpuInfo:
    name: str = "Неизвестно"
    vendor: str = "unknown"          # nvidia / amd / intel
    vram_mb: int = 0
    driver: str = ""
    tier: int = 0                    # 1..5, класс производительности
    power: int = 65                  # условный индекс мощности


@dataclass
class Machine:
    cpu_name: str = "Неизвестно"
    cpu_vendor: str = "unknown"
    cores: int = 0
    threads: int = 0
    cpu_base_mhz: int = 0
    cpu_tier: int = 0
    ram_gb: float = 0.0
    ram_speed: int = 0
    ram_type: str = ""
    ram_sticks: int = 0
    gpu: GpuInfo = field(default_factory=GpuInfo)
    board: str = ""
    board_vendor: str = ""
    width: int = 1920
    height: int = 1080
    refresh: int = 60
    os_name: str = ""
    system_disk_ssd: bool = True
    disks: list = field(default_factory=list)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resolution"] = self.resolution
        return d


# Таблица классов видеокарт: (регулярка, класс 1-5, условный индекс мощности)
_GPU_TABLE = [
    (r"rtx\s*50(90|80)", 5, 260), (r"rtx\s*50(70|60)", 5, 190),
    (r"rtx\s*4090", 5, 250), (r"rtx\s*4080", 5, 205),
    (r"rtx\s*4070", 4, 165), (r"rtx\s*4060", 4, 120),
    (r"rtx\s*3090|rtx\s*3080\s*ti", 5, 200), (r"rtx\s*3080", 5, 175),
    (r"rtx\s*3070", 4, 150), (r"rtx\s*3060\s*ti", 4, 130),
    (r"rtx\s*3060", 3, 100), (r"rtx\s*3050", 3, 72),
    (r"rtx\s*2080", 4, 130), (r"rtx\s*2070|rtx\s*2060\s*super", 3, 105),
    (r"rtx\s*2060", 3, 92),
    (r"gtx\s*166\d|gtx\s*165\d", 2, 70), (r"gtx\s*1080", 3, 100),
    (r"gtx\s*1070", 3, 88), (r"gtx\s*1060", 2, 62), (r"gtx\s*1050", 1, 40),
    (r"gtx\s*9\d\d", 1, 45),
    (r"rx\s*7900", 5, 210), (r"rx\s*7800", 4, 165), (r"rx\s*7700", 4, 140),
    (r"rx\s*7600", 3, 100), (r"rx\s*69\d\d|rx\s*695\d", 5, 190),
    (r"rx\s*6800", 4, 165), (r"rx\s*6700", 4, 125), (r"rx\s*6600", 3, 95),
    (r"rx\s*6500|rx\s*6400", 1, 50), (r"rx\s*5700", 3, 95), (r"rx\s*5600", 2, 78),
    (r"arc\s*a7\d\d", 3, 105), (r"arc\s*a5\d\d", 2, 70),
    (r"(uhd|iris|vega)\s*graphics|integrated", 1, 22),
]


def _classify_gpu(name: str) -> tuple[int, int]:
    """Возвращает (класс 1-5, индекс мощности) по названию видеокарты."""
    low = name.lower()
    for pattern, tier, power in _GPU_TABLE:
        if re.search(pattern, low):
            return tier, power
    return 2, 65


def _classify_cpu(name: str, cores: int, threads: int) -> int:
    """Класс процессора 1-5."""
    low = name.lower()
    gen = 0
    m = re.search(r"i[3579]-(\d{4,5})", low)
    if m:
        digits = m.group(1)
        gen = int(digits[:2]) if len(digits) == 5 else int(digits[0])
    ryzen = re.search(r"ryzen\s*[3579]\s*(\d{4})", low)
    score = cores * 1.0 + threads * 0.5
    if gen >= 12 or (ryzen and int(ryzen.group(1)[0]) >= 5):
        score += 6
    elif gen >= 9:
        score += 3
    if score >= 26:
        return 5
    if score >= 20:
        return 4
    if score >= 14:
        return 3
    if score >= 9:
        return 2
    return 1


_INVENTORY_SCRIPT = """
$out = @{}
$out.cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue |
    Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,Manufacturer
$out.dimms = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue |
    Select-Object Capacity,Speed,SMBIOSMemoryType,Manufacturer)
$out.gpus = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
    Select-Object Name,DriverVersion,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
$out.board = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue |
    Select-Object Manufacturer,Product
$out.os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue |
    Select-Object Caption,BuildNumber
$out.disks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue |
    Select-Object FriendlyName,MediaType,Size)
[pscustomobject]$out
"""


def read_display() -> tuple[int, int, int]:
    """Разрешение и частота обновления экрана. Мгновенно, без WMI.

    Эти три числа меняются чаще всего остального железа — при смене монитора
    или частоты обновления. Поэтому они всегда читаются заново, а не берутся
    из кэша, и берутся напрямую у Windows, а не через WMI.
    """
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
    except Exception:
        return 0, 0, 0

    # частота обновления — из настроек текущего режима экрана
    refresh = 0
    try:
        class DEVMODE(ctypes.Structure):
            _fields_ = [("dmDeviceName", ctypes.c_wchar * 32),
                        ("dmSpecVersion", ctypes.c_ushort),
                        ("dmDriverVersion", ctypes.c_ushort),
                        ("dmSize", ctypes.c_ushort),
                        ("dmDriverExtra", ctypes.c_ushort),
                        ("dmFields", ctypes.c_ulong),
                        ("dmPositionX", ctypes.c_long),
                        ("dmPositionY", ctypes.c_long),
                        ("dmDisplayOrientation", ctypes.c_ulong),
                        ("dmDisplayFixedOutput", ctypes.c_ulong),
                        ("dmColor", ctypes.c_short),
                        ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short),
                        ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short),
                        ("dmFormName", ctypes.c_wchar * 32),
                        ("dmLogPixels", ctypes.c_ushort),
                        ("dmBitsPerPel", ctypes.c_ulong),
                        ("dmPelsWidth", ctypes.c_ulong),
                        ("dmPelsHeight", ctypes.c_ulong),
                        ("dmDisplayFlags", ctypes.c_ulong),
                        ("dmDisplayFrequency", ctypes.c_ulong),
                        ("dmICMMethod", ctypes.c_ulong),
                        ("dmICMIntent", ctypes.c_ulong),
                        ("dmMediaType", ctypes.c_ulong),
                        ("dmDitherType", ctypes.c_ulong),
                        ("dmReserved1", ctypes.c_ulong),
                        ("dmReserved2", ctypes.c_ulong),
                        ("dmPanningWidth", ctypes.c_ulong),
                        ("dmPanningHeight", ctypes.c_ulong)]

        mode = DEVMODE()
        mode.dmSize = ctypes.sizeof(DEVMODE)
        # ENUM_CURRENT_SETTINGS = -1
        if ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):
            refresh = int(mode.dmDisplayFrequency)
            if mode.dmPelsWidth:
                width, height = int(mode.dmPelsWidth), int(mode.dmPelsHeight)
    except Exception:
        pass
    return width, height, refresh or 60


CACHE_FILE = APP_DIR / "machine.json"
CACHE_DAYS = 14


def _load_cache() -> dict | None:
    """Сохранённые сведения о железе, если они ещё свежие."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    age_days = (time.time() - float(data.get("saved", 0))) / 86400
    if age_days > CACHE_DAYS:
        return None
    return data.get("inventory")


def _save_cache(inventory: dict) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps({"saved": time.time(), "inventory": inventory},
                       ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить сведения о железе: {exc}")


def _read_inventory() -> dict:
    """Собирает все сведения о железе одним запуском PowerShell.

    Раньше здесь было шесть отдельных вызовов, и каждый поднимал свой процесс
    PowerShell — вместе это давало четырнадцать секунд на заставке при
    запуске. Запрос тот же самый, но процесс теперь один.
    """
    data = ps_json(_INVENTORY_SCRIPT, timeout=60, depth=6)
    if isinstance(data, dict) and data.get("cpu"):
        _save_cache(data)
        return data
    return {}


def detect_machine(use_cache: bool = True) -> Machine:
    """Полное определение конфигурации ПК.

    Опрос WMI стоит около четырёх секунд, а железо меняется раз в год.
    Поэтому состав компьютера берётся из кэша, если он свежий, а разрешение
    экрана всё равно читается заново — оно меняется чаще всего.

    use_cache=False заставляет перечитать всё заново: так делает фоновое
    обновление после запуска окна.
    """
    m = Machine()
    inventory = (_load_cache() if use_cache else None) or _read_inventory()

    # --- процессор
    cpu = inventory.get("cpu")
    if isinstance(cpu, dict):
        m.cpu_name = (cpu.get("Name") or "").strip()
        m.cores = int(cpu.get("NumberOfCores") or 0)
        m.threads = int(cpu.get("NumberOfLogicalProcessors") or 0)
        m.cpu_base_mhz = int(cpu.get("MaxClockSpeed") or 0)
        vendor = (cpu.get("Manufacturer") or "").lower()
        m.cpu_vendor = "intel" if "intel" in vendor else ("amd" if "amd" in vendor else "unknown")
    if not m.threads:
        m.threads = psutil.cpu_count(logical=True) or 4
        m.cores = psutil.cpu_count(logical=False) or 2
    m.cpu_tier = _classify_cpu(m.cpu_name, m.cores, m.threads)

    # --- оперативная память
    m.ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    dimms = inventory.get("dimms")
    if isinstance(dimms, dict):
        dimms = [dimms]
    if isinstance(dimms, list) and dimms:
        m.ram_sticks = len(dimms)
        m.ram_speed = max(int(d.get("Speed") or 0) for d in dimms)
        mem_type = dimms[0].get("SMBIOSMemoryType")
        m.ram_type = {26: "DDR4", 34: "DDR5", 24: "DDR3"}.get(mem_type, "")
        if not m.ram_type:
            m.ram_type = "DDR5" if m.ram_speed >= 4000 else "DDR4"

    # --- видеокарта
    gpus = inventory.get("gpus")
    if isinstance(gpus, dict):
        gpus = [gpus]
    if isinstance(gpus, list) and gpus:
        # выбираем дискретную карту, а не встроенную
        best = max(gpus, key=lambda g: _classify_gpu(g.get("Name") or "")[1])
        m.gpu.name = (best.get("Name") or "Неизвестно").strip()
        m.gpu.driver = (best.get("DriverVersion") or "").strip()
        low = m.gpu.name.lower()
        m.gpu.vendor = ("nvidia" if "nvidia" in low or "geforce" in low else
                        "amd" if "radeon" in low or "amd" in low else
                        "intel" if "intel" in low or "arc" in low else "unknown")
        m.gpu.tier, m.gpu.power = _classify_gpu(m.gpu.name)
        for g in gpus:
            if g.get("CurrentHorizontalResolution"):
                m.width = int(g["CurrentHorizontalResolution"])
                m.height = int(g["CurrentVerticalResolution"])
                m.refresh = int(g.get("CurrentRefreshRate") or 60)
                break

    # экран читаем всегда заново: он меняется чаще всего остального
    width, height, refresh = read_display()
    if width and height:
        m.width, m.height, m.refresh = width, height, refresh

    # объём видеопамяти: WMI занижает его на картах >4 ГБ, поэтому берём из nvidia-smi
    if m.gpu.vendor == "nvidia" and shutil.which("nvidia-smi"):
        code, out, _ = run(["nvidia-smi", "--query-gpu=memory.total,driver_version",
                            "--format=csv,noheader,nounits"], timeout=10)
        if code == 0 and out:
            parts = out.splitlines()[0].split(",")
            try:
                m.gpu.vram_mb = int(parts[0].strip())
                m.gpu.driver = parts[1].strip()
            except (ValueError, IndexError):
                pass
    if not m.gpu.vram_mb:
        m.gpu.vram_mb = {1: 4096, 2: 6144, 3: 8192, 4: 12288, 5: 16384}.get(m.gpu.tier, 8192)

    # --- материнская плата и ОС
    board = inventory.get("board")
    if isinstance(board, dict):
        m.board_vendor = (board.get("Manufacturer") or "").strip()
        m.board = (board.get("Product") or "").strip()
    osi = inventory.get("os")
    if isinstance(osi, dict):
        m.os_name = f"{osi.get('Caption', '')} (сборка {osi.get('BuildNumber', '')})".strip()

    # --- накопители
    disks = inventory.get("disks")
    if isinstance(disks, dict):
        disks = [disks]
    if isinstance(disks, list):
        m.disks = [{"name": d.get("FriendlyName", ""),
                    "type": str(d.get("MediaType", "")),
                    "gb": round((d.get("Size") or 0) / (1024 ** 3))} for d in disks]
        m.system_disk_ssd = any("SSD" in d["type"] for d in m.disks)

    log(f"Определено железо: {m.cpu_name} / {m.gpu.name} / {m.ram_gb} ГБ / {m.resolution}")
    return m


# ---------------------------------------------------------------- живые датчики



# Класс датчиков живёт в отдельном модуле, но остаётся доступен отсюда
from .sensors import Sensors  # noqa: E402,F401
