"""Счётчик кадров в играх через RivaTuner Statistics Server.

RTSS — бесплатная программа, которая идёт в комплекте с MSI Afterburner.
Она измеряет время кадра любой игры и выкладывает результат в общую память.
Мы её просто читаем: никаких вмешательств в игру, античиты на это не реагируют.

Если RTSS не установлен, модуль честно сообщает об этом и ничего не выдумывает.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .util import log

SHARED_MEMORY_NAME = "RTSSSharedMemoryV2"
SIGNATURE = 0x52545353          # 'RTSS' в обратном порядке байт

# Заголовок RTSS_SHARED_MEMORY: сигнатура, версия, размер записи, смещение и размер массива
_HEADER = struct.Struct("<IIIII")

# Поля записи приложения (RTSS_SHARED_MEMORY_APP_ENTRY)
_OFF_PROCESS_ID = 0
_OFF_NAME = 4
_NAME_SIZE = 260                # MAX_PATH
_OFF_FLAGS = 264
_OFF_TIME0 = 268
_OFF_TIME1 = 272
_OFF_FRAMES = 276
_OFF_FRAME_TIME = 280           # время кадра в микросекундах

# Статистика, которую RTSS считает сам (доступна начиная с версии 2.x)
_OFF_STAT_FRAMERATE_MIN = 316
_OFF_STAT_FRAMERATE_AVG = 320
_OFF_STAT_FRAMERATE_MAX = 324
_OFF_STAT_FRAMETIME_MIN = 328
_OFF_STAT_FRAMETIME_AVG = 332
_OFF_STAT_FRAMETIME_MAX = 336


@dataclass
class FrameStats:
    """Показания счётчика кадров для одной игры."""
    process_id: int = 0
    name: str = ""
    fps: float = 0.0
    frame_time_ms: float = 0.0
    fps_min: float = 0.0
    fps_avg: float = 0.0
    fps_max: float = 0.0

    @property
    def short_name(self) -> str:
        return Path(self.name).name if self.name else ""


def rtss_running() -> bool:
    """Запущен ли процесс RTSS прямо сейчас."""
    import psutil
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith("rtss") or "riva" in name:
            return True
    return False


def launch_rtss() -> tuple[bool, str]:
    """Запускает RivaTuner, если он установлен, но не работает.

    RTSS должен быть запущен постоянно: без него нет ни счётчика кадров в
    оверлее, ни просадок для авторазгона, ни наложения FPS в самой игре.
    Здесь он запускается свёрнутым в трей.
    """
    if rtss_running():
        return True, "RivaTuner уже работает."
    path = rtss_installed()
    if not path:
        return False, ("RivaTuner не установлен. Он идёт в комплекте с "
                       "MSI Afterburner — поставь его с сайта guru3D.")
    from .util import log, run
    # /S — тихий запуск сразу в трей
    run(["cmd", "/c", "start", "", path], timeout=10)
    log(f"RivaTuner запущен: {path}")
    return True, ("RivaTuner запущен. Дай ему пару секунд, потом зайди в игру — "
                  "FPS появится. Чтобы не запускать вручную каждый раз, включи "
                  "в его настройках «Start with Windows».")


def rtss_installed() -> str | None:
    """Путь к RTSS, если он установлен."""
    candidates = [
        r"C:\Program Files (x86)\RivaTuner Statistics Server\RTSS.exe",
        r"C:\Program Files\RivaTuner Statistics Server\RTSS.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


class FpsReader:
    """Читает общую память RTSS."""

    def __init__(self):
        self.available = False
        self.version = 0
        self.note = ""
        self._checked = False

    def _open(self) -> mmap.mmap | None:
        try:
            return mmap.mmap(-1, 4096 * 64, SHARED_MEMORY_NAME, access=mmap.ACCESS_READ)
        except OSError:
            return None

    def check(self) -> tuple[bool, str]:
        """Доступен ли счётчик кадров прямо сейчас."""
        self._checked = True
        shared = self._open()
        if shared is None:
            self.available = False
            if rtss_installed():
                self.note = ("RivaTuner установлен, но не запущен. "
                             "Запусти его — счётчик кадров появится сам.")
            else:
                self.note = (
                    "Для счётчика кадров нужен RivaTuner Statistics Server. "
                    "Это бесплатная программа, она идёт в комплекте с MSI Afterburner "
                    "и просто измеряет время кадра. Установи её — эта страница оживёт.")
            return False, self.note

        try:
            signature, version, entry_size, arr_offset, arr_size = _HEADER.unpack_from(
                shared, 0)
        except (struct.error, ValueError):
            shared.close()
            self.available = False
            self.note = "RivaTuner отвечает, но формат данных не распознан."
            return False, self.note
        shared.close()

        if signature != SIGNATURE or not entry_size:
            self.available = False
            self.note = "Общая память RivaTuner пуста."
            return False, self.note

        self.available = True
        self.version = version
        self.note = f"RivaTuner на связи, версия протокола {version >> 16}.{version & 0xFFFF}."
        return True, self.note

    def read_all(self) -> list[FrameStats]:
        """Все приложения, за которыми следит RTSS."""
        shared = self._open()
        if shared is None:
            self.available = False
            return []
        try:
            signature, _version, entry_size, arr_offset, arr_size = _HEADER.unpack_from(
                shared, 0)
            if signature != SIGNATURE or not entry_size:
                return []

            results: list[FrameStats] = []
            for index in range(arr_size):
                base = arr_offset + index * entry_size
                if base + _OFF_FRAME_TIME + 4 > shared.size():
                    break
                process_id = struct.unpack_from("<I", shared, base + _OFF_PROCESS_ID)[0]
                if not process_id:
                    continue

                raw_name = shared[base + _OFF_NAME: base + _OFF_NAME + _NAME_SIZE]
                name = raw_name.split(b"\x00")[0].decode("utf-8", errors="replace")

                time0, time1, frames, frame_time = struct.unpack_from(
                    "<IIII", shared, base + _OFF_TIME0)

                stats = FrameStats(process_id=process_id, name=name)
                # мгновенный кадр считаем из времени кадра, оно точнее среднего
                if frame_time:
                    stats.frame_time_ms = round(frame_time / 1000.0, 2)
                    stats.fps = round(1_000_000.0 / frame_time, 1)
                elif time1 > time0 and frames:
                    stats.fps = round(frames * 1000.0 / (time1 - time0), 1)

                if base + _OFF_STAT_FRAMERATE_MAX + 4 <= shared.size():
                    try:
                        fmin, favg, fmax = struct.unpack_from(
                            "<III", shared, base + _OFF_STAT_FRAMERATE_MIN)
                        # RTSS хранит статистику в тысячных долях кадра
                        stats.fps_min = round(fmin / 1000.0, 1) if fmin else 0.0
                        stats.fps_avg = round(favg / 1000.0, 1) if favg else 0.0
                        stats.fps_max = round(fmax / 1000.0, 1) if fmax else 0.0
                    except struct.error:
                        pass

                if stats.fps > 0:
                    results.append(stats)
            return results
        except (struct.error, ValueError, OSError) as exc:
            log(f"Не удалось прочитать данные RivaTuner: {exc}")
            return []
        finally:
            shared.close()

    def read_active(self) -> FrameStats | None:
        """Игра с самым свежим кадром — обычно та, в которую играешь."""
        entries = self.read_all()
        if not entries:
            return None
        return max(entries, key=lambda e: e.fps)


class FpsTracker:
    """Копит статистику кадров за игровую сессию."""

    def __init__(self):
        self.reader = FpsReader()
        self.samples: list[float] = []
        self.current: FrameStats | None = None

    def poll(self) -> FrameStats | None:
        stats = self.reader.read_active()
        self.current = stats
        if stats and stats.fps > 0:
            self.samples.append(stats.fps)
            if len(self.samples) > 20000:
                self.samples = self.samples[-12000:]
        return stats

    def reset(self) -> None:
        self.samples.clear()

    def summary(self) -> dict:
        """Средний кадр и просадки за сессию."""
        if not self.samples:
            return {}
        ordered = sorted(self.samples)
        count = len(ordered)

        def percentile(part: float) -> float:
            index = max(0, min(count - 1, int(count * part)))
            return round(ordered[index], 1)

        return {
            "samples": count,
            "avg": round(sum(ordered) / count, 1),
            "min": round(ordered[0], 1),
            "max": round(ordered[-1], 1),
            # нижний процент кадров показывает рывки лучше, чем средний FPS
            "low_1": percentile(0.01),
            "low_5": percentile(0.05),
        }
