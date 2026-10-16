"""Опрос датчиков в реальном времени.

Источники перебираются по очереди, чтобы приложение работало на любом
компьютере, а не только на связке NVIDIA + Intel:
  1. nvidia-smi — для карт NVIDIA, самый точный и быстрый источник;
  2. LibreHardwareMonitor — универсальный, если он запущен: даёт температуру
     и мощность процессора, а также данные по картам AMD и Intel;
  3. счётчики производительности Windows — загрузка видеокарты любого
     производителя вообще без сторонних программ.
"""
from __future__ import annotations

import re
import shutil

import psutil

from .hwinfo import Machine
from .pdh import CpuClock, GpuUtilization
from .util import log, ps_json, run, run_ps


class Sensors:
    """Живые показания железа."""

    def __init__(self, machine: Machine):
        self.machine = machine
        self.has_nvidia = machine.gpu.vendor == "nvidia" and bool(shutil.which("nvidia-smi"))
        self._lhm_ns: str | None = None
        self._lhm_checked = False
        self._counter_fails = 0
        # Родные счётчики Windows: частота процессора и загрузка видеокарты.
        # Читаются за доли миллисекунды и не создают процессов.
        self._clock = CpuClock()
        self._gpu_counter = GpuUtilization()
        self._counters_ok = self._gpu_counter.available
        psutil.cpu_percent(interval=None)          # прогрев счётчика загрузки

    @property
    def sources(self) -> list[str]:
        """Какие источники данных сейчас работают."""
        found = []
        if self.has_nvidia:
            found.append("nvidia-smi")
        if self._lhm_ns:
            found.append("LibreHardwareMonitor")
        if self._counters_ok:
            found.append("счётчики Windows")
        if self._clock.available:
            found.append("частота из PDH")
        return found

    # ------------------------------------------------------------ основной снимок

    def read(self) -> dict:
        """Полный снимок доступных датчиков."""
        vm = psutil.virtual_memory()
        # psutil на Windows отдаёт паспортную частоту вместо текущей,
        # поэтому сначала спрашиваем счётчик производительности
        mhz = self._clock.read()
        if mhz is None:
            freq = psutil.cpu_freq()
            mhz = int(freq.current) if freq else 0
        data = {
            "cpu_load": psutil.cpu_percent(interval=None),
            "cpu_mhz": mhz,
            "cpu_temp": None,
            "cpu_power": None,
            "ram_percent": vm.percent,
            "ram_used_gb": round((vm.total - vm.available) / (1024 ** 3), 1),
            "ram_total_gb": round(vm.total / (1024 ** 3), 1),
            "gpu_load": None, "gpu_temp": None, "gpu_mhz": None,
            "gpu_mem_used": None, "gpu_mem_total": None, "gpu_power": None,
            "gpu_fan": None,
            "mb_temp": None,
            "processes": len(psutil.pids()),
        }
        try:
            usage = psutil.disk_usage("C:\\")
            data["disk_percent"] = usage.percent
            data["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
        except OSError:
            data["disk_percent"] = 0
            data["disk_free_gb"] = 0

        if self.has_nvidia:
            data.update(self._read_nvidia())

        for key, value in self._read_lhm().items():
            if value is not None and data.get(key) is None:
                data[key] = value

        # загрузка видеокарты без сторонних программ — для AMD и Intel
        if data["gpu_load"] is None:
            load = self._read_gpu_counter()
            if load is not None:
                data["gpu_load"] = load

        return data

    # ------------------------------------------------------------ NVIDIA

    def _read_nvidia(self) -> dict:
        query = ("utilization.gpu,temperature.gpu,clocks.current.graphics,"
                 "memory.used,memory.total,power.draw,fan.speed")
        code, out, _err = run(["nvidia-smi", f"--query-gpu={query}",
                               "--format=csv,noheader,nounits"], timeout=6)
        if code != 0 or not out:
            return {}
        parts = [p.strip() for p in out.splitlines()[0].split(",")]

        def num(index, cast=int):
            try:
                return cast(float(parts[index]))
            except (ValueError, IndexError):
                return None

        return {
            "gpu_load": num(0), "gpu_temp": num(1), "gpu_mhz": num(2),
            "gpu_mem_used": num(3), "gpu_mem_total": num(4),
            "gpu_power": num(5, float), "gpu_fan": num(6),
        }

    # ------------------------------------------------------------ LibreHardwareMonitor

    def _find_lhm(self) -> str | None:
        for namespace in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
            code, out, _err = run_ps(
                f"(Get-CimInstance -Namespace {namespace} -ClassName Sensor "
                "-ErrorAction SilentlyContinue | Measure-Object).Count", timeout=8)
            if code == 0 and out.strip().isdigit() and int(out.strip()) > 0:
                log(f"Найден источник датчиков: {namespace}")
                return namespace
        return None

    def _read_lhm(self) -> dict:
        """Температура и мощность процессора, показания карт AMD и Intel."""
        if not self._lhm_checked:
            self._lhm_checked = True
            self._lhm_ns = self._find_lhm()
        if not self._lhm_ns:
            return {}

        rows = ps_json(
            f"Get-CimInstance -Namespace {self._lhm_ns} -ClassName Sensor | "
            "Where-Object { $_.SensorType -in @('Temperature','Power','Load','Clock') } | "
            "Select-Object Name,SensorType,Value,Identifier", timeout=10)
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return {}

        result: dict = {}
        for row in rows:
            name = str(row.get("Name") or "")
            kind = str(row.get("SensorType") or "")
            ident = str(row.get("Identifier") or "").lower()
            try:
                value = float(row.get("Value"))
            except (TypeError, ValueError):
                continue

            is_gpu = "/gpu" in ident
            is_cpu = "/cpu" in ident or "/amdcpu" in ident or "/intelcpu" in ident

            if kind == "Temperature":
                if is_cpu and re.search(r"package|core max|cpu total|tctl", name, re.I):
                    result["cpu_temp"] = round(value)
                elif is_gpu and re.search(r"core|hot ?spot|gpu", name, re.I):
                    result.setdefault("gpu_temp", round(value))
                elif re.search(r"motherboard|system|mainboard", name, re.I):
                    result.setdefault("mb_temp", round(value))
            elif kind == "Power":
                if is_cpu and re.search(r"package|cpu total", name, re.I):
                    result["cpu_power"] = round(value, 1)
                elif is_gpu and re.search(r"package|total|ppt", name, re.I):
                    result.setdefault("gpu_power", round(value, 1))
            elif kind == "Load" and is_gpu and re.search(r"core|d3d 3d|gpu total", name, re.I):
                result.setdefault("gpu_load", round(value))
            elif kind == "Clock" and is_gpu and re.search(r"core", name, re.I):
                result.setdefault("gpu_mhz", round(value))
        return result

    # ------------------------------------------------------------ счётчики Windows

    def _read_gpu_counter(self) -> int | None:
        """Загрузка видеокарты через счётчики Windows — работает у любого вендора.

        Раньше здесь запускался PowerShell на каждый опрос: отдельный процесс
        и сотни миллисекунд на ровном месте. Теперь тот же счётчик читается
        напрямую через PDH за доли миллисекунды.
        """
        if not self._counters_ok:
            return None
        value = self._gpu_counter.read()
        if value is None:
            self._counter_fails += 1
            if self._counter_fails >= 5:
                self._counters_ok = False
                log("Счётчики загрузки видеокарты недоступны — источник отключён")
            return None
        self._counter_fails = 0
        return value
