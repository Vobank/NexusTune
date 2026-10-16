"""Топология процессора: производительные и энергоэффективные ядра.

На процессорах Intel 12-го поколения и новее ядра разные: P-ядра быстрые,
E-ядра экономичные. Windows раскидывает потоки сама и иногда ошибается —
отправляет игру на медленные ядра. Здесь считается точная карта ядер,
чтобы можно было прибить игру к быстрым, а фон согнать на медленные.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from .util import log

RELATION_PROCESSOR_CORE = 0

# Смещения внутри SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX для RelationProcessorCore:
#   0  Relationship (DWORD)
#   4  Size (DWORD)
#   8  Flags (BYTE)          — бит 1 означает многопоточность ядра
#   9  EfficiencyClass (BYTE) — 0 у самых медленных ядер, выше у быстрых
#  10  Reserved (20 байт)
#  30  GroupCount (WORD)
#  32  GroupMask[0]: маска потоков (8 байт) + номер группы (WORD)
_OFF_FLAGS = 8
_OFF_EFFICIENCY = 9
_OFF_GROUP_COUNT = 30
_OFF_GROUP_MASK = 32


@dataclass
class CoreInfo:
    """Одно физическое ядро."""
    index: int
    efficiency: int                 # класс эффективности, больше — быстрее
    mask: int                       # маска логических процессоров этого ядра
    threads: list = field(default_factory=list)
    smt: bool = False               # есть ли у ядра второй поток


@dataclass
class CpuTopology:
    """Карта ядер процессора."""
    cores: list = field(default_factory=list)
    logical_count: int = 0
    hybrid: bool = False            # есть ли ядра разных классов
    performance_mask: int = 0       # маска потоков быстрых ядер
    efficient_mask: int = 0         # маска потоков медленных ядер
    all_mask: int = 0

    @property
    def performance_cores(self) -> int:
        best = self._best_class()
        return sum(1 for c in self.cores if c.efficiency == best)

    @property
    def efficient_cores(self) -> int:
        return len(self.cores) - self.performance_cores

    @property
    def performance_threads(self) -> int:
        return bin(self.performance_mask).count("1")

    @property
    def efficient_threads(self) -> int:
        return bin(self.efficient_mask).count("1")

    def _best_class(self) -> int:
        return max((c.efficiency for c in self.cores), default=0)

    @staticmethod
    def _plural(count: int, one: str, few: str, many: str) -> str:
        """Правильное окончание: 1 поток, 2 потока, 5 потоков."""
        if count % 10 == 1 and count % 100 != 11:
            return one
        if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
            return few
        return many

    def describe(self) -> str:
        if not self.cores:
            return "Топологию определить не удалось."
        if not self.hybrid:
            return (f"{len(self.cores)} одинаковых ядер, {self.logical_count} потоков. "
                    "Все ядра равны — распределять нагрузку по типам не нужно.")
        p_word = self._plural(self.performance_threads, "поток", "потока", "потоков")
        e_word = self._plural(self.efficient_threads, "поток", "потока", "потоков")
        return (f"{self.performance_cores} производительных ядер "
                f"({self.performance_threads} {p_word}) и "
                f"{self.efficient_cores} энергоэффективных "
                f"({self.efficient_threads} {e_word}). "
                "Игру имеет смысл держать на производительных, фон — на экономичных.")


def read_topology() -> CpuTopology:
    """Читает карту ядер у Windows."""
    topology = CpuTopology(logical_count=psutil.cpu_count(logical=True) or 0)
    topology.all_mask = (1 << topology.logical_count) - 1 if topology.logical_count else 0

    kernel32 = ctypes.windll.kernel32
    size = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(
        RELATION_PROCESSOR_CORE, None, ctypes.byref(size))
    if not size.value:
        log("Не удалось получить размер буфера топологии процессора")
        return topology

    buffer = ctypes.create_string_buffer(size.value)
    if not kernel32.GetLogicalProcessorInformationEx(
            RELATION_PROCESSOR_CORE, buffer, ctypes.byref(size)):
        log("Не удалось прочитать топологию процессора")
        return topology

    offset = 0
    index = 0
    while offset < size.value:
        relation, entry_size = struct.unpack_from("<II", buffer, offset)
        if entry_size <= 0:
            break
        if relation == RELATION_PROCESSOR_CORE:
            flags = struct.unpack_from("<B", buffer, offset + _OFF_FLAGS)[0]
            efficiency = struct.unpack_from("<B", buffer, offset + _OFF_EFFICIENCY)[0]
            group_count = struct.unpack_from("<H", buffer, offset + _OFF_GROUP_COUNT)[0]

            mask = 0
            for group in range(max(group_count, 1)):
                group_offset = offset + _OFF_GROUP_MASK + group * 16
                if group_offset + 8 <= size.value:
                    mask |= struct.unpack_from("<Q", buffer, group_offset)[0]

            threads = [bit for bit in range(64) if mask & (1 << bit)]
            topology.cores.append(CoreInfo(
                index=index, efficiency=efficiency, mask=mask,
                threads=threads, smt=bool(flags & 1)))
            index += 1
        offset += entry_size

    classes = {c.efficiency for c in topology.cores}
    topology.hybrid = len(classes) > 1
    best = max(classes) if classes else 0
    for core in topology.cores:
        if core.efficiency == best:
            topology.performance_mask |= core.mask
        else:
            topology.efficient_mask |= core.mask
    if not topology.hybrid:
        topology.performance_mask = topology.all_mask
        topology.efficient_mask = topology.all_mask

    log(f"Топология процессора: {len(topology.cores)} ядер, "
        f"{'гибридная' if topology.hybrid else 'однородная'}")
    return topology


# ---------------------------------------------------------------- готовые наборы ядер


def affinity_presets(topology: CpuTopology) -> list[tuple[str, str, int]]:
    """Готовые варианты привязки: (название, пояснение, маска)."""
    presets: list[tuple[str, str, int]] = [
        ("Все ядра", "Стандартное поведение Windows — процесс может занимать любое ядро.",
         topology.all_mask),
    ]
    if not topology.hybrid:
        half = 0
        for core in topology.cores[: max(len(topology.cores) // 2, 1)]:
            half |= core.mask
        presets.append(("Половина ядер",
                        "Ограничивает тяжёлую фоновую задачу, чтобы она не мешала игре.",
                        half))
        return presets

    presets.append((
        "Только производительные",
        "Игра не будет случайно попадать на медленные ядра — убирает рывки "
        "в играх, которые плохо дружат с гибридными процессорами.",
        topology.performance_mask))
    presets.append((
        "Только энергоэффективные",
        "Для фоновых программ: браузер, торренты, антивирус. "
        "Они перестают отбирать быстрые ядра у игры.",
        topology.efficient_mask))

    # первые физические P-ядра без второго потока — для игр, чувствительных к SMT
    physical_only = 0
    best = max((c.efficiency for c in topology.cores), default=0)
    for core in topology.cores:
        if core.efficiency == best and core.threads:
            physical_only |= 1 << core.threads[0]
    if physical_only and physical_only != topology.performance_mask:
        presets.append((
            "Производительные без гиперпоточности",
            "По одному потоку с каждого быстрого ядра. Иногда помогает старым "
            "играм, которые плохо работают с виртуальными потоками.",
            physical_only))
    return presets


def mask_to_text(mask: int, logical_count: int) -> str:
    """Человекочитаемое описание маски ядер."""
    threads = [str(i) for i in range(logical_count) if mask & (1 << i)]
    if not threads:
        return "нет ядер"
    if len(threads) == logical_count:
        return "все ядра"
    return f"потоки {', '.join(threads)}"
