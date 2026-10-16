"""Управление подсветкой через OpenRGB.

Здесь реализован сетевой протокол OpenRGB SDK напрямую, без сторонних библиотек.
Эффекты (радуга, дыхание, волна, реакция на температуру) считаются самим
приложением и отправляются кадрами — так они одинаково работают на любых
устройствах: плате, памяти, видеокарте, клавиатуре, кулере.
"""
from __future__ import annotations

import colorsys
import math
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from .util import log, run_ps

MAGIC = b"ORGB"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6742

# идентификаторы пакетов протокола
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
DEVICE_UPDATE_LEDS = 1050
DEVICE_SET_CUSTOM_MODE = 1100

DEVICE_TYPES = {
    0: "Материнская плата", 1: "Оперативная память", 2: "Видеокарта", 3: "Кулер",
    4: "Лента", 5: "Клавиатура", 6: "Мышь", 7: "Коврик", 8: "Гарнитура",
    9: "Подставка", 10: "Геймпад", 11: "Лампа", 12: "Колонки", 13: "Виртуальное",
    14: "Накопитель", 15: "Корпус", 16: "Микрофон", 17: "Аксессуар", 18: "Кейпад",
    19: "Другое",
}


@dataclass
class RgbDevice:
    index: int
    name: str
    type_id: int
    description: str = ""
    led_count: int = 0
    leds: list = field(default_factory=list)
    enabled: bool = True

    @property
    def type_name(self) -> str:
        return DEVICE_TYPES.get(self.type_id, "Другое")


class _Reader:
    """Последовательное чтение бинарного буфера протокола."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u8(self) -> int:
        return struct.unpack("<B", self.take(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def string(self) -> str:
        length = self.u16()
        raw = self.take(length)
        return raw.split(b"\x00")[0].decode("utf-8", errors="replace")


class OpenRGBClient:
    """Минимальный, но полноценный клиент OpenRGB SDK."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.protocol = 0
        self.devices: list[RgbDevice] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ соединение

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self, timeout: float = 3.0) -> tuple[bool, str]:
        """Подключается к серверу OpenRGB."""
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
            self.sock.settimeout(timeout)
        except OSError as exc:
            self.sock = None
            return False, self._hint(exc)

        try:
            self._negotiate_version()
            self._send(0, SET_CLIENT_NAME, b"Kremen\x00")
            self.refresh_devices()
        except (OSError, struct.error) as exc:
            self.disconnect()
            return False, f"Сервер OpenRGB ответил неожиданно: {exc}"
        return True, f"Подключено. Устройств: {len(self.devices)}"

    @staticmethod
    def _hint(exc: OSError) -> str:
        return ("Сервер OpenRGB не отвечает на 127.0.0.1:6742.\n\n"
                "Что нужно сделать:\n"
                "1. Установить OpenRGB (openrgb.org) — он бесплатный и открытый.\n"
                "2. Запустить его от имени администратора.\n"
                "3. В настройках включить «Enable SDK Server» и перезапустить программу.\n\n"
                f"Техническая причина: {exc}")

    def disconnect(self) -> None:
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None
            self.devices = []

    # ------------------------------------------------------------ протокол

    def _send(self, device_id: int, packet_id: int, data: bytes = b"") -> None:
        if not self.sock:
            raise OSError("нет соединения")
        header = MAGIC + struct.pack("<III", device_id, packet_id, len(data))
        with self._lock:
            self.sock.sendall(header + data)

    def _recv_exact(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise OSError("соединение закрыто сервером")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, int, bytes]:
        header = self._recv_exact(16)
        if header[:4] != MAGIC:
            raise OSError("неверная сигнатура пакета")
        device_id, packet_id, size = struct.unpack("<III", header[4:])
        return device_id, packet_id, self._recv_exact(size) if size else b""

    def _negotiate_version(self) -> None:
        self._send(0, REQUEST_PROTOCOL_VERSION, struct.pack("<I", 4))
        try:
            _d, _p, data = self._recv()
            server = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0
        except OSError:
            server = 0
        self.protocol = min(server, 4)

    # ------------------------------------------------------------ устройства

    def refresh_devices(self) -> list[RgbDevice]:
        self._send(0, REQUEST_CONTROLLER_COUNT)
        _d, _p, data = self._recv()
        count = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0

        devices: list[RgbDevice] = []
        for i in range(count):
            self._send(i, REQUEST_CONTROLLER_DATA, struct.pack("<I", self.protocol))
            _d, _p, blob = self._recv()
            try:
                devices.append(self._parse_controller(i, blob))
            except (struct.error, IndexError, UnicodeDecodeError) as exc:
                log(f"Не удалось разобрать устройство {i}: {exc}")
                devices.append(RgbDevice(index=i, name=f"Устройство {i}", type_id=19))
        self.devices = devices
        return devices

    def _parse_controller(self, index: int, blob: bytes) -> RgbDevice:
        r = _Reader(blob)
        r.u32()                                   # общий размер блока
        type_id = r.u32()
        name = r.string()
        if self.protocol >= 1:
            r.string()                            # производитель
        description = r.string()
        r.string()                                # версия
        r.string()                                # серийный номер
        r.string()                                # расположение

        num_modes = r.u16()
        r.u32()                                   # активный режим
        for _ in range(num_modes):
            r.string()                            # имя режима
            if self.protocol >= 5:
                r.i32()                           # идентификатор режима
            r.i32()                               # значение
            r.u32()                               # флаги
            r.u32(); r.u32()                      # мин/макс скорость
            if self.protocol >= 3:
                r.u32(); r.u32()                  # мин/макс яркость
            r.u32(); r.u32()                      # мин/макс число цветов
            r.u32()                               # скорость
            if self.protocol >= 3:
                r.u32()                           # яркость
            r.u32()                               # направление
            r.u32()                               # цветовой режим
            mode_colors = r.u16()
            r.take(mode_colors * 4)

        num_zones = r.u16()
        for _ in range(num_zones):
            r.string()                            # имя зоны
            r.i32()                               # тип зоны
            r.u32(); r.u32(); r.u32()             # мин/макс/текущее число светодиодов
            matrix_len = r.u16()
            r.take(matrix_len)
            if self.protocol >= 4:
                segments = r.u16()
                for _s in range(segments):
                    r.string()
                    r.i32()
                    r.u32(); r.u32()
            if self.protocol >= 5:
                r.u32()                           # флаги зоны

        num_leds = r.u16()
        led_names = []
        for _ in range(num_leds):
            led_names.append(r.string())
            r.u32()                               # значение светодиода

        return RgbDevice(index=index, name=name, type_id=type_id,
                         description=description, led_count=num_leds, leds=led_names)

    # ------------------------------------------------------------ управление цветом

    def set_custom_mode(self, device_index: int) -> None:
        """Переводит устройство в режим прямого управления цветом."""
        try:
            self._send(device_index, DEVICE_SET_CUSTOM_MODE)
        except OSError:
            pass

    def set_colors(self, device_index: int, colors: list[tuple[int, int, int]]) -> bool:
        """Задаёт цвет каждому светодиоду устройства."""
        if not self.sock:
            return False
        payload = struct.pack("<H", len(colors))
        for (red, green, blue) in colors:
            payload += struct.pack("<BBBB", red & 255, green & 255, blue & 255, 0)
        data = struct.pack("<I", len(payload) + 4) + payload
        try:
            self._send(device_index, DEVICE_UPDATE_LEDS, data)
            return True
        except OSError as exc:
            log(f"Не удалось отправить цвета на устройство {device_index}: {exc}")
            return False

    def fill(self, device: RgbDevice, color: tuple[int, int, int]) -> bool:
        return self.set_colors(device.index, [color] * max(device.led_count, 1))


# ---------------------------------------------------------------- эффекты


def _hsv(hue: float, sat: float = 1.0, val: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, sat, val)
    return int(r * 255), int(g * 255), int(b * 255)


def temp_to_color(temp: float, cold: float = 40.0, hot: float = 85.0) -> tuple[int, int, int]:
    """Синий → зелёный → жёлтый → красный по температуре."""
    ratio = max(0.0, min(1.0, (temp - cold) / (hot - cold)))
    hue = (1.0 - ratio) * 0.66          # 0.66 = синий, 0.0 = красный
    return _hsv(hue, 1.0, 1.0)


EFFECTS = {
    "static": "Статичный цвет",
    "breathing": "Дыхание",
    "rainbow": "Радуга",
    "wave": "Волна",
    "temperature": "Реакция на температуру",
    "load": "Реакция на загрузку",
    "off": "Выключить подсветку",
}


class EffectEngine:
    """Рисует кадры подсветки в отдельном потоке."""

    def __init__(self, client: OpenRGBClient):
        self.client = client
        self.effect = "static"
        self.color = (255, 40, 0)
        self.speed = 1.0
        self.brightness = 1.0
        self.sensor_value = 0.0          # температура или загрузка для реактивных эффектов
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fps = 25

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        for dev in self.client.devices:
            if dev.enabled:
                self.client.set_custom_mode(dev.index)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def apply_once(self) -> None:
        """Разовая отрисовка — для статичного цвета без постоянного потока."""
        self._render(0.0)

    def _loop(self) -> None:
        start = time.monotonic()
        frame_time = 1.0 / self.fps
        while not self._stop.is_set():
            self._render(time.monotonic() - start)
            self._stop.wait(frame_time)

    def _render(self, elapsed: float) -> None:
        if not self.client.connected:
            return
        phase = elapsed * self.speed
        for dev in self.client.devices:
            if not dev.enabled or dev.led_count <= 0:
                continue
            colors = self._frame(dev, phase)
            self.client.set_colors(dev.index, colors)

    def _frame(self, dev: RgbDevice, phase: float) -> list[tuple[int, int, int]]:
        n = max(dev.led_count, 1)
        bright = self.brightness

        def dim(c):
            return (int(c[0] * bright), int(c[1] * bright), int(c[2] * bright))

        if self.effect == "off":
            return [(0, 0, 0)] * n
        if self.effect == "static":
            return [dim(self.color)] * n
        if self.effect == "breathing":
            wave = (math.sin(phase * 2.0) + 1) / 2
            level = 0.15 + 0.85 * wave
            return [(int(self.color[0] * level * bright),
                     int(self.color[1] * level * bright),
                     int(self.color[2] * level * bright))] * n
        if self.effect == "rainbow":
            return [dim(_hsv(phase * 0.15 + i / max(n, 12))) for i in range(n)]
        if self.effect == "wave":
            base = colorsys.rgb_to_hsv(*[c / 255 for c in self.color])[0]
            out = []
            for i in range(n):
                level = (math.sin(phase * 3 + i * 0.6) + 1) / 2
                out.append(dim(_hsv(base, 1.0, 0.2 + 0.8 * level)))
            return out
        if self.effect == "temperature":
            return [dim(temp_to_color(self.sensor_value))] * n
        if self.effect == "load":
            ratio = max(0.0, min(1.0, self.sensor_value / 100))
            lit = int(n * ratio)
            hot = _hsv((1.0 - ratio) * 0.33)
            return [dim(hot) if i < lit else (0, 0, 0) for i in range(n)]
        return [dim(self.color)] * n


# ---------------------------------------------------------------- вспомогательное


def openrgb_installed() -> str | None:
    """Ищет установленный OpenRGB."""
    from pathlib import Path
    for p in (r"C:\Program Files\OpenRGB\OpenRGB.exe",
              r"C:\Program Files (x86)\OpenRGB\OpenRGB.exe",
              r"C:\OpenRGB\OpenRGB.exe"):
        if Path(p).exists():
            return p
    return None


def openrgb_running() -> bool:
    """Запущен ли OpenRGB.

    Раньше здесь поднимался PowerShell — полторы секунды на вопрос, ответ на
    который лежит в списке процессов. Из-за этого страница подсветки
    открывалась почти пять секунд.
    """
    import psutil
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower().startswith("openrgb"):
            return True
    return False


def launch_openrgb() -> bool:
    """Запускает OpenRGB со включённым сервером SDK."""
    path = openrgb_installed()
    if not path:
        return False
    from .util import run as _run
    _run(["cmd", "/c", "start", "", path, "--server", "--startminimized"], timeout=10)
    return True


GAME_SCENES = {
    "esports": ("Киберспорт", (255, 20, 0), "static"),
    "balanced": ("Баланс", (0, 150, 255), "breathing"),
    "quality": ("Качество", (140, 0, 255), "wave"),
    "ultra": ("Ультра", (255, 255, 255), "rainbow"),
}
