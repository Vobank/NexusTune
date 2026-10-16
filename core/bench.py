"""Встроенный тест производительности: процессор, память, диск.

Нужен, чтобы измерять эффект оптимизации, а не верить на слово. Прогони тест
до применения профиля и после — приложение покажет разницу в цифрах.

Многопоточный тест намеренно построен на хешировании: эта операция отпускает
глобальную блокировку интерпретатора, поэтому реально грузит все ядра.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import random
import statistics
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import psutil

from .util import APP_DIR, log

RESULTS_FILE = APP_DIR / "benchmarks.json"

# Опорные значения — примерно уровень игрового компьютера среднего класса.
# Результат в 1000 очков означает «как эталон», больше — быстрее эталона.
_REF_SINGLE = 4_400_000        # итераций в секунду на одном ядре
_REF_MULTI = 5_800             # МБ/с хеширования на всех ядрах
_REF_MEMORY = 5_800            # МБ/с копирования в памяти
_REF_DISK_WRITE = 950          # МБ/с записи со сбросом на диск — честная скорость носителя
_REF_DISK_RAND = 55           # МБ/с случайного чтения 4 КБ мимо кэша (очередь 1)

# Версия калибровки. Меняется при правке опорных значений выше — результаты
# разных версий несопоставимы, поэтому сравниваются только одинаковые.
CALIBRATION = 3


@dataclass
class BenchResult:
    """Результат одного прогона."""
    timestamp: float = field(default_factory=time.time)
    label: str = ""
    single_ops: float = 0.0
    multi_mbs: float = 0.0
    memory_mbs: float = 0.0
    disk_write_mbs: float = 0.0
    disk_read_mbs: float = 0.0
    disk_random_mbs: float = 0.0

    single_score: int = 0
    multi_score: int = 0
    memory_score: int = 0
    disk_score: int = 0
    total_score: int = 0

    cpu_name: str = ""
    threads: int = 0
    calibration: int = CALIBRATION

    @property
    def when(self) -> str:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(self.timestamp))

    def compute_scores(self) -> None:
        self.single_score = int(self.single_ops / _REF_SINGLE * 1000)
        self.multi_score = int(self.multi_mbs / _REF_MULTI * 1000)
        self.memory_score = int(self.memory_mbs / _REF_MEMORY * 1000)
        # запись со сбросом на диск обмануть кэшем нельзя, поэтому вес у неё выше
        disk_mix = (self.disk_write_mbs / _REF_DISK_WRITE * 0.6
                    + self.disk_random_mbs / _REF_DISK_RAND * 0.4)
        self.disk_score = int(disk_mix * 1000)
        self.total_score = int(
            self.single_score * 0.3 + self.multi_score * 0.35
            + self.memory_score * 0.15 + self.disk_score * 0.2)


# ---------------------------------------------------------------- отдельные тесты


def bench_single_core(seconds: float = 3.0) -> float:
    """Скорость одного ядра: сколько операций успевает за отведённое время."""
    end = time.perf_counter() + seconds
    count = 0
    value = 1
    while time.perf_counter() < end:
        # смесь целочисленной и вещественной арифметики, как в игровой логике
        for _ in range(2000):
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
            count += 1
    return count / seconds


def bench_multi_core(seconds: float = 3.0, threads: int | None = None) -> float:
    """Пропускная способность всех ядер, МБ/с."""
    workers = threads or (os.cpu_count() or 4)
    block = os.urandom(4 * 1024 * 1024)               # 4 МБ данных на поток
    counters = [0] * workers
    stop = threading.Event()

    def worker(index: int):
        local = 0
        while not stop.is_set():
            hashlib.sha256(block).digest()            # эта операция отпускает GIL
            local += 1
        counters[index] = local

    pool = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    start = time.perf_counter()
    for thread in pool:
        thread.start()
    time.sleep(seconds)
    stop.set()
    for thread in pool:
        thread.join(timeout=5)
    elapsed = time.perf_counter() - start
    total_mb = sum(counters) * 4
    return total_mb / elapsed if elapsed else 0.0


def bench_memory(seconds: float = 2.0) -> float:
    """Скорость работы с оперативной памятью, МБ/с."""
    size = 64 * 1024 * 1024                           # 64 МБ
    source = bytearray(os.urandom(1024 * 1024)) * 64
    target = bytearray(size)
    view_src = memoryview(source)
    view_dst = memoryview(target)

    end = time.perf_counter() + seconds
    copied = 0
    start = time.perf_counter()
    while time.perf_counter() < end:
        view_dst[:] = view_src
        copied += size
    elapsed = time.perf_counter() - start
    return copied / (1024 ** 2) / elapsed if elapsed else 0.0


# Флаги CreateFileW для чтения мимо кэша Windows
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_NO_BUFFERING = 0x20000000
_INVALID_HANDLE = -1
_SECTOR = 4096


def _random_read_uncached(test_file: Path, file_size: int,
                          reads: int = 2500) -> float | None:
    """Случайное чтение блоками по 4 КБ мимо кэша Windows.

    Обычное чтение только что записанного файла попадает в кэш и показывает
    сотни мегабайт в секунду на блоках по 4 КБ — это скорость оперативной
    памяти, а не диска. FILE_FLAG_NO_BUFFERING заставляет Windows идти прямо
    к носителю. Требование флага: смещения и размеры кратны размеру сектора,
    а буфер выровнен по его границе — поэтому буфер выделяется с запасом
    и выравнивается вручную.

    None означает, что открыть файл так не удалось — тогда зовущий код
    честно откатывается на обычное чтение.
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(
        str(test_file), _GENERIC_READ, _FILE_SHARE_READ, None,
        _OPEN_EXISTING, _FILE_FLAG_NO_BUFFERING, None)
    if handle == _INVALID_HANDLE or not handle:
        return None

    # выравниваем буфер по границе сектора: без этого ReadFile откажет
    backing = ctypes.create_string_buffer(_SECTOR * 2)
    address = ctypes.addressof(backing)
    aligned = (address + _SECTOR - 1) & ~(_SECTOR - 1)
    buffer = (ctypes.c_char * _SECTOR).from_address(aligned)

    read_bytes = ctypes.c_ulong(0)
    position = ctypes.c_longlong(0)
    total = 0
    limit = max(file_size - _SECTOR, _SECTOR)

    start = time.perf_counter()
    try:
        for _ in range(reads):
            offset = random.randrange(0, limit // _SECTOR) * _SECTOR
            position.value = offset
            if not kernel32.SetFilePointerEx(handle, position, None, 0):
                return None
            if not kernel32.ReadFile(handle, buffer, _SECTOR,
                                     ctypes.byref(read_bytes), None):
                return None
            total += read_bytes.value
    finally:
        kernel32.CloseHandle(handle)
        del buffer                      # снимаем ссылку на выровненную область
    elapsed = time.perf_counter() - start
    return total / (1024 ** 2) / elapsed if elapsed else 0.0


def _disk_pass(test_file: Path, chunk: bytes, blocks: int) -> tuple[float, float, float]:
    """Один проход теста диска: (запись МБ/с, чтение МБ/с, случайное чтение МБ/с)."""
    # --- последовательная запись со сбросом на носитель
    start = time.perf_counter()
    try:
        with open(test_file, "wb", buffering=0) as fh:
            for _ in range(blocks):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        log(f"Тест диска не удался: {exc}")
        return 0.0, 0.0, 0.0
    write_time = time.perf_counter() - start
    written_mb = blocks * 4

    # --- последовательное чтение
    start = time.perf_counter()
    try:
        with open(test_file, "rb", buffering=0) as fh:
            while fh.read(4 * 1024 * 1024):
                pass
    except OSError:
        pass
    read_time = time.perf_counter() - start

    # --- случайное чтение блоками по 4 КБ, мимо кэша Windows
    file_size = written_mb * 1024 * 1024
    random_mbs = _random_read_uncached(test_file, file_size)
    if random_mbs is None:
        # запасной путь: обычное чтение. Оно попадает в кэш и завышает
        # результат, но это лучше, чем не измерить ничего.
        random_bytes = 0
        start = time.perf_counter()
        try:
            with open(test_file, "rb", buffering=0) as fh:
                for _ in range(2500):
                    offset = random.randrange(0, max(file_size - 4096, 1))
                    fh.seek(offset)
                    random_bytes += len(fh.read(4096))
        except OSError:
            pass
        random_time = time.perf_counter() - start
        random_mbs = (random_bytes / (1024 ** 2) / random_time
                      if random_time else 0.0)

    return (
        written_mb / write_time if write_time else 0.0,
        written_mb / read_time if read_time else 0.0,
        random_mbs,
    )


def bench_disk(path: Path | None = None, size_mb: int = 96,
               passes: int = 3) -> tuple[float, float, float]:
    """Скорость диска: (запись МБ/с, чтение МБ/с, случайное чтение МБ/с).

    Диск — самая шумная часть теста. Состояние SLC-кэша SSD, фоновая запись
    Windows и антивирус легко меняют результат вдвое: замеренные подряд прогоны
    давали 2011 и 1049 очков на одной и той же машине. Поэтому проход делается
    трижды на блоке поменьше, и берётся медиана — она устойчива к единичному
    выбросу, а суммарное время остаётся прежним.

    Запись идёт со сбросом на носитель, поэтому это настоящая скорость диска.
    Чтение частично попадает в кэш Windows и на машинах с большим объёмом ОЗУ
    показывает завышенные цифры — в общий счёт оно идёт с малым весом.
    """
    folder = path or APP_DIR
    test_file = folder / "kremen_bench.tmp"
    chunk = os.urandom(4 * 1024 * 1024)
    blocks = max(size_mb // 4, 8)

    writes: list[float] = []
    reads: list[float] = []
    randoms: list[float] = []
    for _ in range(max(1, passes)):
        write, read, rnd = _disk_pass(test_file, chunk, blocks)
        if write <= 0:
            break
        writes.append(write)
        reads.append(read)
        randoms.append(rnd)

    try:
        test_file.unlink()
    except OSError:
        pass

    if not writes:
        return 0.0, 0.0, 0.0
    return (statistics.median(writes), statistics.median(reads),
            statistics.median(randoms))


# ---------------------------------------------------------------- полный прогон


def run_full(machine=None, label: str = "", progress=None) -> BenchResult:
    """Полный тест. progress получает (описание шага, процент готовности)."""

    def report(text: str, percent: int):
        if progress:
            progress(text, percent)

    result = BenchResult(label=label)
    if machine:
        result.cpu_name = machine.cpu_name
        result.threads = machine.threads
    else:
        result.threads = os.cpu_count() or 4

    report("Разогреваю процессор…", 3)
    bench_single_core(0.6)

    report("Тест одного ядра — так игры считают логику…", 10)
    result.single_ops = bench_single_core(3.0)

    report("Тест всех ядер — так работает распаковка и кодирование…", 35)
    result.multi_mbs = bench_multi_core(3.5)

    report("Тест оперативной памяти…", 60)
    result.memory_mbs = bench_memory(2.0)

    report("Тест диска: запись, чтение, случайный доступ…", 75)
    write_mbs, read_mbs, random_mbs = bench_disk()
    result.disk_write_mbs = write_mbs
    result.disk_read_mbs = read_mbs
    result.disk_random_mbs = random_mbs

    report("Считаю очки…", 96)
    result.compute_scores()
    save_result(result)
    report("Готово.", 100)
    log(f"Тест завершён: {result.total_score} очков ({label})")
    return result


# ---------------------------------------------------------------- история результатов


def load_results() -> list[BenchResult]:
    if not RESULTS_FILE.exists():
        return []
    try:
        raw = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    results = []
    for item in raw:
        try:
            result = BenchResult(**item)
        except TypeError:
            continue
        if result.calibration == CALIBRATION:
            results.append(result)
    return sorted(results, key=lambda r: r.timestamp, reverse=True)


def save_result(result: BenchResult) -> None:
    results = load_results()
    results.insert(0, result)
    results = results[:40]
    try:
        RESULTS_FILE.write_text(
            json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить результат теста: {exc}")


def compare(new: BenchResult, old: BenchResult) -> list[tuple[str, float, float, float]]:
    """Сравнение двух прогонов: (название, было, стало, разница в процентах)."""
    rows = [
        ("Одно ядро", old.single_score, new.single_score),
        ("Все ядра", old.multi_score, new.multi_score),
        ("Память", old.memory_score, new.memory_score),
        ("Диск", old.disk_score, new.disk_score),
        ("Общий счёт", old.total_score, new.total_score),
    ]
    out = []
    for name, before, after in rows:
        delta = ((after - before) / before * 100) if before else 0.0
        out.append((name, before, after, round(delta, 1)))
    return out


def readiness_warnings() -> list[str]:
    """Проверяет, не помешает ли что-то честному замеру."""
    warnings = []
    load = psutil.cpu_percent(interval=0.4)
    if load > 25:
        warnings.append(f"Процессор уже занят на {load:.0f}% — результат будет занижен. "
                        "Закрой лишние программы.")
    memory = psutil.virtual_memory()
    if memory.percent > 85:
        warnings.append(f"Память занята на {memory.percent:.0f}% — тест может врать.")
    try:
        battery = psutil.sensors_battery()
        if battery and not battery.power_plugged:
            warnings.append("Ноутбук работает от батареи — процессор будет придушен.")
    except Exception:
        pass
    free_gb = psutil.disk_usage(str(APP_DIR)).free / (1024 ** 3)
    if free_gb < 2:
        warnings.append("На системном диске меньше 2 ГБ свободного места — "
                        "тест диска не запустится.")
    return warnings
