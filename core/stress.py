"""Проверка стабильности: нагрузка с контролем правильности вычислений.

Обычный «тест» просто греет железо и показывает температуру. Это полдела:
разогнанный процессор или память с завышенной частотой чаще всего не падают,
а начинают тихо ошибаться — один бит из миллиарда. Именно так и выглядят
вылеты игр «на ровном месте» через два часа.

Поэтому здесь считается не абы что, а задача с заранее известным ответом:
  * процессор — многократное хеширование одного и того же крупного блока.
    Контрольная сумма обязана совпасть с эталоном до последнего бита;
  * память — крупный буфер заполняется несжимаемым узором и вычитывается
    обратно кусками с побайтовой сверкой.

Если хоть раз результат не совпал — это аппаратная ошибка, и приложение скажет
об этом прямо. Такой тест ловит нестабильность, которую «погонять FurMark»
не ловит никогда.

Чего тест не делает честно: не нагружает видеокарту трёхмерной сценой. Без
собственного рендерера это невозможно, и притворяться приложение не будет —
для видеокарты есть наблюдение за стабильностью во время реальной игры.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field

import psutil

from .util import log

# Размер блока, который хешируется за один проход. Важно, что он крупный:
# на буферах больше пары килобайт hashlib отпускает GIL, поэтому потоки
# считают по-настоящему параллельно, а интерфейс приложения не подвисает.
# На мелких хешах в цикле GIL держится почти всё время и окно перестаёт
# отвечать — это проверено.
_BLOCK_BYTES = 4 * 1024 * 1024

# Сколько проходов по блоку в одном круге. Круг должен занимать доли секунды,
# иначе тест плохо реагирует на кнопку «остановить».
_PASSES = 24

# Размер блока памяти на один поток. Больше — злее тест, но нельзя съесть всё.
_MEM_BLOCK_MB = 64

# Кусок, которым пишем и сверяем память. Мелкие куски держат GIL недолго.
_MEM_PIECE_BYTES = 4 * 1024 * 1024


@dataclass
class StressResult:
    """Итог проверки."""
    seconds: float = 0.0
    mode: str = ""
    threads: int = 0
    cpu_rounds: int = 0
    mem_rounds: int = 0
    errors: int = 0
    error_details: list = field(default_factory=list)
    max_cpu_temp: int = 0
    max_gpu_temp: int = 0
    max_power: float = 0.0
    min_mhz: int = 0
    max_mhz: int = 0
    stopped_early: bool = False

    @property
    def verdict(self) -> tuple[str, str]:
        """Вердикт и цветовой ключ."""
        if self.errors:
            return "Найдены ошибки вычислений", "danger"
        if self.seconds < 60:
            return "Прошло без ошибок, но тест был коротким", "warn"
        if self.max_cpu_temp and self.max_cpu_temp >= 95:
            return "Ошибок нет, но процессор упирается в предел температуры", "warn"
        return "Стабильно", "ok"

    def advice(self) -> list[str]:
        """Человеческие выводы."""
        out: list[str] = []
        if self.errors:
            out.append(
                f"Обнаружено ошибок: {self.errors}. Это значит, что железо считает "
                "неправильно под нагрузкой. Самые частые причины — завышенная "
                "частота памяти (XMP на пределе), разгон процессора или "
                "недостаточное напряжение. Верни память на профиль ниже или "
                "сбрось разгон и повтори тест.")
        else:
            out.append(
                f"За {self.seconds:.0f} с не было ни одной ошибки вычислений. "
                "Проверено кругов: "
                f"{self.cpu_rounds} по процессору, {self.mem_rounds} по памяти.")

        if self.max_cpu_temp:
            if self.max_cpu_temp >= 95:
                out.append(
                    f"Процессор доходил до {self.max_cpu_temp} °C — это потолок, "
                    "дальше он сам сбрасывает частоту. Стоит проверить кулер "
                    "и термопасту или сделать андервольт.")
            elif self.max_cpu_temp >= 85:
                out.append(f"Максимум по процессору {self.max_cpu_temp} °C — "
                           "горячо, но в пределах нормы под полной нагрузкой.")
            else:
                out.append(f"Максимум по процессору {self.max_cpu_temp} °C — "
                           "с охлаждением всё в порядке.")

        if self.max_mhz and self.min_mhz and self.max_mhz - self.min_mhz > 800:
            out.append(
                f"Частота гуляла от {self.min_mhz} до {self.max_mhz} МГц. Под "
                "постоянной нагрузкой такой разброс обычно означает, что "
                "процессор упирается в лимит мощности или температуры.")

        if self.stopped_early:
            out.append("Тест остановлен вручную — вердикт неполный.")
        elif self.seconds < 300 and not self.errors:
            out.append(
                "Для уверенности в разгоне гоняют 20-30 минут: редкие ошибки "
                "проявляются не сразу. Пятиминутный прогон отсекает только "
                "грубую нестабильность.")
        return out


class StressTest:
    """Управляет проверкой стабильности в фоновых потоках."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.running = False
        self.result = StressResult()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._monitor: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = 0.0

    # ------------------------------------------------------------ управление

    def start(self, mode: str = "mixed", threads: int | None = None,
              minutes: float = 5.0) -> None:
        """Запускает проверку. mode: cpu / memory / mixed."""
        if self.running:
            return
        count = threads or max(1, (os.cpu_count() or 4))
        self.result = StressResult(mode=mode, threads=count)
        self._stop.clear()
        self.running = True
        self._started = time.time()
        self._deadline = self._started + minutes * 60

        self._workers = []
        for index in range(count):
            # В смешанном режиме каждый четвёртый поток занимается памятью:
            # так нагружаются и ядра, и контроллер памяти одновременно.
            if mode == "memory" or (mode == "mixed" and index % 4 == 3):
                target = self._memory_worker
            else:
                target = self._cpu_worker
            thread = threading.Thread(target=target, args=(index,), daemon=True)
            thread.start()
            self._workers.append(thread)

        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()
        log(f"Проверка стабильности запущена: режим {mode}, потоков {count}, "
            f"{minutes:.0f} мин")

    def stop(self, manual: bool = True) -> StressResult:
        """Останавливает проверку и возвращает итог."""
        if not self.running:
            return self.result
        self._stop.set()
        for thread in self._workers:
            thread.join(timeout=5)
        if self._monitor:
            self._monitor.join(timeout=5)
        self.running = False
        self.result.seconds = round(time.time() - self._started, 1)
        self.result.stopped_early = manual and time.time() < self._deadline
        log(f"Проверка стабильности завершена: {self.result.errors} ошибок за "
            f"{self.result.seconds:.0f} с")
        return self.result

    @property
    def elapsed(self) -> float:
        return time.time() - self._started if self.running else self.result.seconds

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.time()) if self.running else 0.0

    def _expired(self) -> bool:
        return time.time() >= self._deadline

    # ------------------------------------------------------------ нагрузка

    def _cpu_worker(self, index: int) -> None:
        """Хеширование крупного блока с проверкой результата по эталону."""
        seed = f"kremen-cpu-{index}".encode()
        block = self._make_block(seed)
        reference = self._grind(block)

        while not self._stop.is_set() and not self._expired():
            digest = self._grind(block)
            if digest != reference:
                self._record_error(
                    f"Поток {index}: процессор вернул неверную контрольную сумму "
                    "одного и того же блока данных.")
            with self._lock:
                self.result.cpu_rounds += 1

    @staticmethod
    def _make_block(seed: bytes) -> bytes:
        """Несжимаемый блок данных, одинаковый от запуска к запуску."""
        piece = hashlib.sha512(seed).digest()
        chunk = bytearray()
        while len(chunk) < _BLOCK_BYTES:
            piece = hashlib.sha512(piece).digest()
            chunk += piece
        return bytes(chunk[:_BLOCK_BYTES])

    @staticmethod
    def _grind(block: bytes) -> bytes:
        """Несколько проходов по блоку. Результат обязан быть одним и тем же."""
        digest = b"kremen"
        for _ in range(_PASSES):
            hasher = hashlib.sha256()
            hasher.update(digest)
            hasher.update(block)          # крупный буфер — GIL отпускается
            digest = hasher.digest()
        return digest

    def _memory_worker(self, index: int) -> None:
        """Запись узора в крупный блок и побайтовая сверка при чтении.

        Сверяем кусками, а не целиком: сравнение стомегабайтного буфера одной
        операцией надолго удерживает GIL и подвешивает интерфейс. Куски по
        4 МБ дают тот же охват, но оставляют окно живым.
        """
        block_mb = self._safe_block_size()
        if block_mb <= 0:
            log("Проверка памяти пропущена: слишком мало свободной памяти")
            return

        # Узор строим из хеша, чтобы он не сжимался и не кэшировался целиком
        pattern = hashlib.sha256(f"kremen-mem-{index}".encode()).digest() * 4
        piece = pattern * (_MEM_PIECE_BYTES // len(pattern))
        pieces = max(1, block_mb * 1024 * 1024 // len(piece))

        # Разносим выделение памяти по времени: если все потоки просят по
        # блоку одновременно, система на секунду замирает на старте теста.
        self._stop.wait(index * 0.2)
        try:
            buffer = bytearray(len(piece) * pieces)
        except MemoryError:
            log("Проверка памяти: не хватило памяти под блок")
            return

        while not self._stop.is_set() and not self._expired():
            for number in range(pieces):
                start = number * len(piece)
                buffer[start:start + len(piece)] = piece
            for number in range(pieces):
                if self._stop.is_set():
                    break
                start = number * len(piece)
                if buffer[start:start + len(piece)] != piece:
                    self._record_error(
                        f"Поток {index}: память вернула не то, что в неё записали "
                        f"(кусок {number + 1} из {pieces}).")
            with self._lock:
                self.result.mem_rounds += 1
        del buffer

    def _safe_block_size(self) -> int:
        """Сколько мегабайт можно занять, не подвесив систему."""
        try:
            available_mb = psutil.virtual_memory().available / (1024 ** 2)
        except Exception:
            return 0
        # оставляем системе минимум 2 ГБ и не берём больше четверти свободного
        usable = (available_mb - 2048) / 4
        return int(max(0, min(_MEM_BLOCK_MB, usable)))

    def _record_error(self, text: str) -> None:
        with self._lock:
            self.result.errors += 1
            if len(self.result.error_details) < 20:
                self.result.error_details.append(text)
        log(f"ПРОВЕРКА СТАБИЛЬНОСТИ: {text}")

    # ------------------------------------------------------------ наблюдение

    def _monitor_loop(self) -> None:
        """Пишет температуры и частоты, пока идёт нагрузка."""
        while not self._stop.is_set() and not self._expired():
            try:
                self._sample()
            except Exception:
                pass
            self._stop.wait(2.0)
        # тест мог закончиться сам по времени — отметим это
        if self._expired():
            self._stop.set()

    def _sample(self) -> None:
        freq = psutil.cpu_freq()
        if freq and freq.current:
            mhz = int(freq.current)
            with self._lock:
                self.result.max_mhz = max(self.result.max_mhz, mhz)
                self.result.min_mhz = (mhz if not self.result.min_mhz
                                       else min(self.result.min_mhz, mhz))

        if self.ctx is None or not getattr(self.ctx, "sensors", None):
            return
        reading = self.ctx.last_reading or {}
        with self._lock:
            if reading.get("cpu_temp"):
                self.result.max_cpu_temp = max(self.result.max_cpu_temp,
                                               int(reading["cpu_temp"]))
            if reading.get("gpu_temp"):
                self.result.max_gpu_temp = max(self.result.max_gpu_temp,
                                               int(reading["gpu_temp"]))
            if reading.get("wall_w"):
                self.result.max_power = max(self.result.max_power,
                                            float(reading["wall_w"]))


MODES = {
    "cpu": ("Только процессор",
            "Все ядра считают цепочки хешей с проверкой результата. "
            "Ловит нестабильность разгона процессора."),
    "memory": ("Только память",
               "Крупные блоки заполняются узором и вычитываются обратно "
               "с побайтовой сверкой. Ловит ошибки XMP и завышенной частоты."),
    "mixed": ("Процессор и память вместе",
              "Самый злой режим: ядра считают, а часть потоков одновременно "
              "гоняет память. Так система ведёт себя в тяжёлой игре."),
}
