"""Управление процессами: приоритеты, привязка к ядрам и правила.

Это то, за что берут деньги программы вроде Process Lasso. Смысл простой:
Windows раскидывает потоки сама и иногда отдаёт игре медленные ядра, а
браузеру — быстрые. Здесь можно закрепить нужное поведение навсегда.

Умный баланс работает сам: пока идёт игра, прожорливые фоновые программы
уходят на энергоэффективные ядра с пониженным приоритетом, а после выхода
из игры всё возвращается.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import psutil

from .cpuinfo import CpuTopology, read_topology
from .util import log

# Приоритеты без режима реального времени: он способен подвесить систему целиком,
# потому что процесс начинает вытеснять драйверы ввода и мыши.
PRIORITIES = [
    ("Низкий", psutil.IDLE_PRIORITY_CLASS,
     "Процесс получает время только когда все остальные простаивают."),
    ("Ниже среднего", psutil.BELOW_NORMAL_PRIORITY_CLASS,
     "Уступает дорогу активной программе. Хорошо для загрузок и архивации."),
    ("Обычный", psutil.NORMAL_PRIORITY_CLASS,
     "Стандартный приоритет Windows."),
    ("Выше среднего", psutil.ABOVE_NORMAL_PRIORITY_CLASS,
     "Небольшое преимущество перед фоном. Безопасный выбор для игры."),
    ("Высокий", psutil.HIGH_PRIORITY_CLASS,
     "Заметное преимущество. Больше не нужно — режим реального времени "
     "способен подвесить мышь и клавиатуру."),
]

PRIORITY_NAMES = {value: name for name, value, _why in PRIORITIES}

# Эти процессы трогать нельзя: система, драйверы, антивирус, ввод
PROTECTED = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "fontdrvhost.exe", "audiodg.exe", "conhost.exe",
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "ctfmon.exe",
    "sihost.exe", "runtimebroker.exe", "dllhost.exe", "taskhostw.exe",
    "wudfhost.exe", "spoolsv.exe", "nvcontainer.exe", "nvdisplay.container.exe",
    "lghub.exe", "lghub_agent.exe", "icue.exe", "armourycrate.exe",
    "easyanticheat.exe", "beservice.exe", "vgc.exe", "vgtray.exe",
    "python.exe", "pythonw.exe", "kremen.exe",
}

# Кандидаты на ссылку в фон во время игры
BACKGROUND_HOGS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "browser.exe",
    "steamwebhelper.exe", "discord.exe", "discordptb.exe", "spotify.exe",
    "telegram.exe", "onedrive.exe", "dropbox.exe", "googledrivefs.exe",
    "yandexdisk.exe", "utorrent.exe", "qbittorrent.exe", "obs64.exe",
    "code.exe", "devenv.exe", "photoshop.exe", "wallpaper64.exe",
    "wallpaperservice32_c.exe", "tidal.exe", "epicwebhelper.exe",
    "searchapp.exe", "searchindexer.exe", "widgets.exe", "msteams.exe",
}


@dataclass
class ProcessRow:
    """Строка в списке процессов."""
    pid: int
    name: str
    cpu: float = 0.0
    memory_mb: int = 0
    priority: int = psutil.NORMAL_PRIORITY_CLASS
    affinity: int = 0
    threads: int = 0
    protected: bool = False
    has_rule: bool = False

    @property
    def priority_name(self) -> str:
        return PRIORITY_NAMES.get(self.priority, "неизвестно")


@dataclass
class ProcessRule:
    """Правило, которое применяется к программе автоматически."""
    process: str                     # имя исполняемого файла
    priority: int | None = None
    affinity: int | None = None
    enabled: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return {"process": self.process, "priority": self.priority,
                "affinity": self.affinity, "enabled": self.enabled, "note": self.note}

    @classmethod
    def from_dict(cls, data: dict) -> "ProcessRule":
        return cls(
            process=str(data.get("process", "")),
            priority=data.get("priority"),
            affinity=data.get("affinity"),
            enabled=bool(data.get("enabled", True)),
            note=str(data.get("note", "")),
        )


def _mask_to_list(mask: int, logical: int) -> list[int]:
    return [i for i in range(logical) if mask & (1 << i)]


def _list_to_mask(cpus: list[int]) -> int:
    mask = 0
    for cpu in cpus:
        mask |= 1 << cpu
    return mask


class ProcessManager:
    """Читает процессы, применяет приоритеты и держит правила."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.topology: CpuTopology = read_topology()
        self.logical = self.topology.logical_count or (psutil.cpu_count() or 4)
        self._cpu_primed = False

    # ------------------------------------------------------------ чтение

    def list_processes(self, limit: int = 60) -> list[ProcessRow]:
        """Список процессов, отсортированный по нагрузке на процессор."""
        if not self._cpu_primed:
            # первый вызов cpu_percent всегда возвращает ноль — прогреваем счётчики
            for proc in psutil.process_iter():
                try:
                    proc.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self._cpu_primed = True

        rules = {r.process.lower() for r in self.load_rules()}
        rows: list[ProcessRow] = []
        for proc in psutil.process_iter(["pid", "name", "memory_info", "num_threads"]):
            try:
                name = proc.info["name"] or ""
                # «Бездействие системы» — не программа, а счётчик свободного времени:
                # его 150% нагрузки означают простой, а не работу
                if not name or name.lower() in ("system idle process", "idle"):
                    continue
                info = proc.info["memory_info"]
                row = ProcessRow(
                    pid=proc.info["pid"],
                    name=name,
                    cpu=round(proc.cpu_percent(None) / max(self.logical, 1), 1),
                    memory_mb=int(info.rss / (1024 ** 2)) if info else 0,
                    threads=proc.info["num_threads"] or 0,
                    protected=name.lower() in PROTECTED,
                    has_rule=name.lower() in rules,
                )
                try:
                    row.priority = proc.nice()
                    row.affinity = _list_to_mask(proc.cpu_affinity())
                except (psutil.AccessDenied, OSError):
                    pass
                rows.append(row)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        rows.sort(key=lambda r: (-r.cpu, -r.memory_mb))
        return rows[:limit]

    # ------------------------------------------------------------ изменение

    def set_priority(self, pid: int, priority: int) -> tuple[bool, str]:
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() in PROTECTED:
                return False, "Это системный процесс — его лучше не трогать."
            proc.nice(priority)
            return True, f"{proc.name()}: приоритет «{PRIORITY_NAMES.get(priority, priority)}»."
        except psutil.NoSuchProcess:
            return False, "Процесс уже закрылся."
        except psutil.AccessDenied:
            return False, "Нет доступа. Запусти приложение от имени администратора."
        except (OSError, ValueError) as exc:
            return False, str(exc)

    def set_affinity(self, pid: int, mask: int) -> tuple[bool, str]:
        cpus = _mask_to_list(mask, self.logical)
        if not cpus:
            return False, "Нужно оставить хотя бы одно ядро."
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() in PROTECTED:
                return False, "Это системный процесс — его лучше не трогать."
            proc.cpu_affinity(cpus)
            return True, f"{proc.name()}: работает на {len(cpus)} потоках."
        except psutil.NoSuchProcess:
            return False, "Процесс уже закрылся."
        except psutil.AccessDenied:
            return False, "Нет доступа. Запусти приложение от имени администратора."
        except (OSError, ValueError) as exc:
            return False, str(exc)

    def apply_to_name(self, process_name: str, priority: int | None,
                      affinity: int | None) -> int:
        """Применяет настройки ко всем копиям программы. Возвращает число процессов."""
        touched = 0
        target = process_name.lower()
        for proc in psutil.process_iter(["pid", "name"]):
            if (proc.info["name"] or "").lower() != target:
                continue
            pid = proc.info["pid"]
            ok = False
            if priority is not None:
                ok |= self.set_priority(pid, priority)[0]
            if affinity is not None:
                ok |= self.set_affinity(pid, affinity)[0]
            if ok:
                touched += 1
        return touched

    # ------------------------------------------------------------ правила

    def load_rules(self) -> list[ProcessRule]:
        if not self.ctx:
            return []
        raw = self.ctx.config.get("process_rules", [])
        rules = []
        for item in raw:
            if isinstance(item, dict):
                rules.append(ProcessRule.from_dict(item))
        return rules

    def save_rules(self, rules: list[ProcessRule]) -> None:
        if not self.ctx:
            return
        self.ctx.config["process_rules"] = [r.to_dict() for r in rules]
        self.ctx.save()

    def add_rule(self, rule: ProcessRule) -> None:
        rules = [r for r in self.load_rules() if r.process.lower() != rule.process.lower()]
        rules.append(rule)
        self.save_rules(rules)
        log(f"Правило для {rule.process} сохранено")

    def remove_rule(self, process_name: str) -> None:
        rules = [r for r in self.load_rules()
                 if r.process.lower() != process_name.lower()]
        self.save_rules(rules)

    def apply_all_rules(self) -> int:
        """Применяет все правила к запущенным процессам."""
        applied = 0
        for rule in self.load_rules():
            if rule.enabled:
                applied += self.apply_to_name(rule.process, rule.priority, rule.affinity)
        return applied

    # ------------------------------------------------------------ быстрые действия

    def background_to_efficient_cores(self) -> tuple[int, str]:
        """Сгоняет известные фоновые программы на энергоэффективные ядра."""
        if not self.topology.hybrid:
            return 0, ("У этого процессора все ядра одинаковые — "
                       "разделять их на быстрые и медленные нечего.")
        mask = self.topology.efficient_mask
        moved = 0
        for proc in psutil.process_iter(["pid", "name"]):
            name = (proc.info["name"] or "").lower()
            if name in BACKGROUND_HOGS and name not in PROTECTED:
                if self.set_affinity(proc.info["pid"], mask)[0]:
                    moved += 1
        return moved, (f"Фоновых процессов переведено на энергоэффективные ядра: {moved}. "
                       "Быстрые ядра теперь свободны для игры.")

    def release_all_cores(self) -> tuple[int, str]:
        """Возвращает всем процессам доступ ко всем ядрам."""
        mask = self.topology.all_mask
        restored = 0
        for proc in psutil.process_iter(["pid", "name"]):
            name = (proc.info["name"] or "").lower()
            if name in PROTECTED:
                continue
            try:
                current = _list_to_mask(proc.cpu_affinity())
                if current != mask:
                    if self.set_affinity(proc.info["pid"], mask)[0]:
                        restored += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return restored, f"Привязка к ядрам снята у {restored} процессов."


# ---------------------------------------------------------------- умный баланс


class SmartBalancer:
    """Пока идёт игра, придерживает прожорливый фон.

    Работает мягко: не закрывает программы, а понижает им приоритет и уводит
    на энергоэффективные ядра. После выхода из игры всё возвращается.
    """

    def __init__(self, manager: ProcessManager, ctx=None):
        self.manager = manager
        self.ctx = ctx
        self.cpu_threshold = 8.0            # процент нагрузки, с которого фон мешает
        self.enabled = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._touched: dict[int, tuple[int, int]] = {}   # pid -> (приоритет, маска)
        self.last_action = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.enabled = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("Умный баланс включён")

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.restore()
        log("Умный баланс выключен")

    def _game_running(self) -> bool:
        watcher = getattr(self.ctx, "watcher", None) if self.ctx else None
        return bool(watcher and watcher.current)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._game_running():
                    self._throttle()
                elif self._touched:
                    self.restore()
            except Exception as exc:
                log(f"Умный баланс споткнулся: {exc}")
            self._stop.wait(5.0)

    def _throttle(self) -> None:
        """Придерживает фоновые процессы, которые едят процессор."""
        logical = max(self.manager.logical, 1)
        efficient = self.manager.topology.efficient_mask
        game_process = ""
        watcher = getattr(self.ctx, "watcher", None) if self.ctx else None
        if watcher and watcher.current:
            game_process = watcher.current.process.lower()

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info["name"] or "").lower()
                pid = proc.info["pid"]
                if not name or name in PROTECTED or name == game_process:
                    continue
                if pid in self._touched:
                    continue
                cpu = proc.cpu_percent(None) / logical
                if cpu < self.cpu_threshold and name not in BACKGROUND_HOGS:
                    continue
                if cpu < 2.0:
                    continue

                original_priority = proc.nice()
                original_mask = _list_to_mask(proc.cpu_affinity())
                proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                if self.manager.topology.hybrid and efficient:
                    proc.cpu_affinity(_mask_to_list(efficient, logical))
                self._touched[pid] = (original_priority, original_mask)
                self.last_action = (f"{proc.info['name']} придержан: "
                                    f"занимал {cpu:.0f}% процессора во время игры.")
                log(self.last_action)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

    def restore(self) -> int:
        """Возвращает всё, что придерживал."""
        restored = 0
        for pid, (priority, mask) in list(self._touched.items()):
            try:
                proc = psutil.Process(pid)
                proc.nice(priority)
                proc.cpu_affinity(_mask_to_list(mask, self.manager.logical))
                restored += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
            self._touched.pop(pid, None)
        if restored:
            self.last_action = f"Возвращено к обычному режиму: {restored} процессов."
            log(self.last_action)
        return restored

    @property
    def held_count(self) -> int:
        return len(self._touched)
