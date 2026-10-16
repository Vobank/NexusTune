"""Быстрое чтение счётчиков производительности Windows напрямую через PDH.

Зачем понадобилось. Два места работали плохо:

1. Частота процессора. psutil на Windows отдаёт паспортную частоту, а не
   текущую: на i5-13400F он всегда показывает 2500 МГц, хотя под нагрузкой
   ядра идут за 4 ГГц. Правильный способ — счётчик
   «% Processor Performance», который показывает долю от базовой частоты.

2. Загрузка видеокарты у AMD и Intel. Раньше она читалась запуском PowerShell
   на каждый опрос — это сотни миллисекунд и целый процесс каждую секунду.

PDH — родной интерфейс Windows к тем же счётчикам. После открытия запроса
чтение занимает микросекунды и не создаёт процессов.

Английские имена счётчиков берутся через PdhAddEnglishCounter, поэтому всё
работает одинаково на русской и любой другой локализации Windows.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from .util import log

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0

_pdh = None


def _status(value: int) -> int:
    """Код возврата PDH как беззнаковое 32-битное число.

    ctypes отдаёт результат знаковым int, поэтому PDH_MORE_DATA (0x800007D2)
    приходит как отрицательное число и не совпадает с константой напрямую.
    """
    return value & 0xFFFFFFFF


def _library():
    global _pdh
    if _pdh is None:
        try:
            _pdh = ctypes.WinDLL("pdh.dll")
        except OSError as exc:
            log(f"PDH недоступен: {exc}")
            _pdh = False
    return _pdh or None


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    """Значение счётчика. Объединение в C выровнено по 8 байт, поэтому padding."""
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    """Одна строка массива значений для счётчика с подстановкой (*)."""
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


class Counter:
    """Один счётчик производительности.

    Счётчики-скорости (загрузка, частота) требуют двух замеров: PDH считает
    разницу между ними. Поэтому первый вызов read() честно возвращает None.
    """

    def __init__(self, path: str, wildcard: bool = False):
        self.path = path
        self.wildcard = wildcard
        self.available = False
        self._query = wintypes.HANDLE()
        self._counter = wintypes.HANDLE()
        self._primed = False
        self._open()

    def _open(self) -> None:
        pdh = _library()
        if pdh is None:
            return
        if _status(pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))) != ERROR_SUCCESS:
            return
        # English-вариант: имена счётчиков не зависят от языка Windows
        status = _status(pdh.PdhAddEnglishCounterW(
            self._query, self.path, 0, ctypes.byref(self._counter)))
        if status != ERROR_SUCCESS:
            pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()
            return
        self.available = True
        # первый сбор — опорная точка для счётчиков-скоростей
        pdh.PdhCollectQueryData(self._query)

    def read(self) -> float | None:
        """Текущее значение. None, если счётчик недоступен или это первый замер."""
        if not self.available:
            return None
        pdh = _library()
        if pdh is None:
            return None
        if _status(pdh.PdhCollectQueryData(self._query)) != ERROR_SUCCESS:
            return None
        if not self._primed:
            self._primed = True
            return None

        value = PDH_FMT_COUNTERVALUE()
        status = _status(pdh.PdhGetFormattedCounterValue(
            self._counter, PDH_FMT_DOUBLE, None, ctypes.byref(value)))
        if status != ERROR_SUCCESS:
            return None
        return float(value.doubleValue)

    def read_sum(self) -> float | None:
        """Сумма по всем экземплярам — для счётчиков с подстановкой (*)."""
        if not self.available:
            return None
        pdh = _library()
        if pdh is None:
            return None
        if _status(pdh.PdhCollectQueryData(self._query)) != ERROR_SUCCESS:
            return None
        if not self._primed:
            self._primed = True
            return None

        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        status = _status(pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), None))
        if status != PDH_MORE_DATA or not size.value:
            return None

        buffer = ctypes.create_string_buffer(size.value)
        status = _status(pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), buffer))
        if status != ERROR_SUCCESS:
            return None

        items = ctypes.cast(
            buffer, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        total = 0.0
        for index in range(count.value):
            item = items[index]
            if item.FmtValue.CStatus == ERROR_SUCCESS:
                total += item.FmtValue.doubleValue
        return total

    def close(self) -> None:
        pdh = _library()
        if pdh is not None and self._query:
            pdh.PdhCloseQuery(self._query)
        self._query = wintypes.HANDLE()
        self.available = False


# ---------------------------------------------------------------- готовые счётчики


class CpuClock:
    """Настоящая частота процессора.

    Счётчик отдаёт долю от базовой частоты в процентах: 142% на базовых
    2500 МГц означает 3550 МГц. Это и есть реальная частота с учётом турбо.
    """

    PATH = r"\Processor Information(_Total)\% Processor Performance"

    def __init__(self):
        self.counter = Counter(self.PATH)
        self.base_mhz = self._base_mhz()

    @staticmethod
    def _base_mhz() -> int:
        import psutil
        try:
            freq = psutil.cpu_freq()
            # psutil на Windows отдаёт как раз базовую частоту — здесь это кстати
            return int(freq.max or freq.current or 0)
        except Exception:
            return 0

    @property
    def available(self) -> bool:
        return self.counter.available and self.base_mhz > 0

    def read(self) -> int | None:
        percent = self.counter.read()
        if percent is None or not self.base_mhz:
            return None
        return int(self.base_mhz * percent / 100.0)


class GpuUtilization:
    """Загрузка видеокарты без сторонних программ — у любого производителя.

    Windows считает загрузку по каждому движку видеокарты отдельно. Нас
    интересует трёхмерный: именно он грузится играми.
    """

    PATH = r"\GPU Engine(*engtype_3D)\Utilization Percentage"

    def __init__(self):
        self.counter = Counter(self.PATH, wildcard=True)

    @property
    def available(self) -> bool:
        return self.counter.available

    def read(self) -> int | None:
        total = self.counter.read_sum()
        if total is None:
            return None
        return max(0, min(100, int(round(total))))
