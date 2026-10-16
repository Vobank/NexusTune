"""РЕАКТОР — планировщик, который реагирует на твои руки.

Обычный «игровой режим» ставит игре высокий приоритет один раз при запуске
и на этом успокаивается. Реактор работает иначе: он следит за твоим вводом
с частотой опроса мыши и меняет поведение системы в тот момент, когда ты
целишься или стреляешь, а не «в среднем за сессию».

Что он делает на самом деле:

  * держит окно, в которое ты играешь, на производительных ядрах;
  * в момент всплеска ввода (резкий разворот мыши, очередь кликов) поднимает
    класс приоритета игры до высокого, а через секунду покоя опускает обратно —
    так планировщик Windows не успевает отдать твой квант времени фону;
  * тяжёлый фон в этот же момент прижимается к энергоэффективным ядрам;
  * ведёт счётчик: сколько раз вмешался и сколько времени игра провела
    в поднятом состоянии.

Честно о том, чего он НЕ делает, хотя это часто обещают:

  * Перераспределить кэш L3 между ядрами из обычной программы нельзя. Эта
    возможность (Intel CAT / AMD QoS) живёт в регистрах MSR, доступ к ним
    требует драйвера уровня ядра, и на потребительских процессорах она чаще
    всего просто отключена в прошивке. Любая программа, которая обещает
    «перераспределение L3» на домашнем ПК, либо ничего не делает, либо врёт.
    Поэтому здесь этого нет.
  * Режим реального времени (REALTIME_PRIORITY_CLASS) не используется:
    он способен лишить процессорного времени драйвер мыши и клавиатуры, и
    компьютер перестанет отвечать. Потолок — высокий приоритет.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from .cpuinfo import read_topology
from .util import log

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Классы приоритета Windows
IDLE = 0x00000040
BELOW_NORMAL = 0x00004000
NORMAL = 0x00000020
ABOVE_NORMAL = 0x00008000
HIGH = 0x00000080
# REALTIME здесь намеренно отсутствует — см. заголовок модуля

PRIORITY_NAMES = {
    IDLE: "простой", BELOW_NORMAL: "ниже среднего", NORMAL: "обычный",
    ABOVE_NORMAL: "выше среднего", HIGH: "высокий",
}

# Клавиши мыши, по которым определяем «целится / стреляет»
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class Burst:
    """Одно вмешательство реактора."""
    when: float
    reason: str
    intensity: float


@dataclass
class ReactorState:
    """Что реактор видит и делает прямо сейчас."""
    running: bool = False
    target_pid: int = 0
    target_name: str = ""
    priority: int = NORMAL
    boosted: bool = False
    intensity: float = 0.0          # 0..1, насколько активен ввод
    peak_intensity: float = 0.0
    bursts: int = 0                 # сколько раз поднимали приоритет
    boosted_seconds: float = 0.0
    background_parked: int = 0      # сколько фоновых процессов прижато
    poll_hz: float = 0.0            # реальная частота опроса ввода
    note: str = ""
    history: list = field(default_factory=list)

    @property
    def priority_name(self) -> str:
        return PRIORITY_NAMES.get(self.priority, "неизвестно")


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# Программы, которые бывают развёрнуты на весь экран, но играми не являются
_FULLSCREEN_NOT_GAMES = {
    "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
    "code.exe", "devenv.exe", "photoshop.exe", "obs64.exe", "obs32.exe",
    "vlc.exe", "mpc-hc64.exe", "camostudio.exe", "telegram.exe",
    "discord.exe", "spotify.exe", "steam.exe", "steamwebhelper.exe",
    "kremen.exe", "python.exe", "pythonw.exe", "windowsterminal.exe",
    "powershell.exe", "cmd.exe", "notepad.exe", "winword.exe", "excel.exe",
}


def _window_is_fullscreen() -> bool:
    """Занимает ли окно переднего плана весь экран."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    if screen_w <= 0 or screen_h <= 0:
        return False
    # допускаем небольшой выход за края: так ведут себя безрамочные окна
    return width >= screen_w * 0.98 and height >= screen_h * 0.95


def looks_like_game(pid: int) -> bool:
    """Похоже ли окно переднего плана на игру.

    Двух признаков хватает и оба дешёвые:
      1. окно занимает весь экран — полноэкранный или безрамочный режим;
      2. имя процесса не из списка «бывает на весь экран, но не игра».

    Плюс безусловное «да» для процессов, которые автопилот уже опознал как
    игры: они могут идти и в окне.
    """
    if not pid:
        return False
    try:
        name = psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    from .autopilot import KNOWN_GAME_EXES, NOT_GAMES
    if name in KNOWN_GAME_EXES:
        return True
    if name in NOT_GAMES or name in _FULLSCREEN_NOT_GAMES:
        return False
    return _window_is_fullscreen()


def _foreground_pid() -> int:
    """PID процесса, чьё окно сейчас на переднем плане."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _set_priority(pid: int, priority: int) -> bool:
    """Меняет класс приоритета процесса напрямую, без psutil.

    psutil на каждый вызов открывает и закрывает описатель процесса. Реактор
    трогает приоритет десятки раз за сессию в момент, когда важна каждая
    миллисекунда, поэтому здесь прямой вызов Windows.
    """
    handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.SetPriorityClass(handle, priority))
    finally:
        kernel32.CloseHandle(handle)


class InputPulse:
    """Измеритель активности рук: движение мыши и нажатия кнопок.

    Опрашивается часто (по умолчанию 120 раз в секунду). Считает «интенсивность»
    от 0 до 1: ноль — рука лежит, единица — резкий разворот с зажатой кнопкой.
    """

    def __init__(self, decay: float = 0.90):
        self.decay = decay
        self.value = 0.0
        self._last_point = self._cursor()
        self._last_buttons = 0

    @staticmethod
    def _cursor() -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def sample(self) -> float:
        """Один замер. Возвращает текущую интенсивность 0..1."""
        x, y = self._cursor()
        dx = abs(x - self._last_point[0])
        dy = abs(y - self._last_point[1])
        self._last_point = (x, y)

        # 60 пикселей за один опрос — это уже резкий разворот
        movement = min(1.0, (dx + dy) / 60.0)

        buttons = 0
        for key in (VK_LBUTTON, VK_RBUTTON):
            if user32.GetAsyncKeyState(key) & 0x8000:
                buttons += 1
        click = 0.6 if buttons else 0.0
        # нажатие только что появилось — это выстрел, а не удержание
        if buttons > self._last_buttons:
            click = 1.0
        self._last_buttons = buttons

        instant = max(movement, click)
        # затухание: всплеск гаснет плавно, чтобы приоритет не дёргался
        self.value = max(instant, self.value * self.decay)
        return self.value


class Reactor:
    """Реагирует на ввод, пока идёт игра."""

    # Порог интенсивности, выше которого поднимаем приоритет
    BOOST_AT = 0.35
    # Ниже этого — отпускаем
    RELEASE_AT = 0.12
    # Сколько держать поднятым после последнего всплеска
    HOLD_SECONDS = 1.5

    def __init__(self, ctx=None, poll_hz: int = 120):
        self.ctx = ctx
        self.poll_hz = max(30, min(500, poll_hz))
        # реагировать только на полноэкранные окна — то есть на игры
        self.games_only = True
        self.state = ReactorState()
        self.pulse = InputPulse()
        self.park_background = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._topology = None
        self._original: dict[int, int] = {}      # pid -> прежний приоритет
        self._parked: dict[int, int] = {}        # pid -> прежняя маска ядер
        self._boost_started = 0.0
        self._last_boost = 0.0
        self._accumulated = 0.0      # сколько всего пробыли в поднятом состоянии

    # ------------------------------------------------------------ управление

    def start(self, park_background: bool = True,
              games_only: bool = True) -> tuple[bool, str]:
        if self.state.running:
            return True, "Реактор уже работает."
        self._topology = read_topology()
        if not self._topology.cores:
            return False, "Не удалось прочитать топологию процессора."

        self.park_background = park_background
        self.games_only = games_only
        self._stop.clear()
        self.state = ReactorState(running=True)
        self.state.note = (
            "Реактор следит за вводом "
            f"{self.poll_hz} раз в секунду. Приоритет поднимается в момент "
            "резкого движения или выстрела и отпускается через полторы "
            "секунды покоя."
            + (" Реагирует только на полноэкранные окна — то есть на игры."
               if games_only else
               " Реагирует на любое окно переднего плана."))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log(f"Реактор запущен: опрос {self.poll_hz} Гц, "
            f"парковка фона {'включена' if park_background else 'выключена'}")
        return True, self.state.note

    def stop(self) -> ReactorState:
        if not self.state.running:
            return self.state
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self._restore_all()
        self.state.running = False
        log(f"Реактор остановлен: вмешательств {self.state.bursts}, "
            f"в поднятом состоянии {self.state.boosted_seconds:.0f} с")
        return self.state

    # ------------------------------------------------------------ цикл

    def _loop(self) -> None:
        interval = 1.0 / self.poll_hz
        next_tick = time.perf_counter()
        samples = 0
        window_start = time.perf_counter()

        while not self._stop.is_set():
            now = time.perf_counter()
            if now < next_tick:
                # спим ровно до следующего замера, не перегружая ядро
                time.sleep(max(0.0, min(next_tick - now, interval)))
                continue
            next_tick = now + interval

            try:
                self._tick()
            except Exception as exc:
                log(f"Реактор: сбой шага — {exc}")

            samples += 1
            if now - window_start >= 1.0:
                self.state.poll_hz = round(samples / (now - window_start), 1)
                samples = 0
                window_start = now

    def _tick(self) -> None:
        intensity = self.pulse.sample()
        self.state.intensity = round(intensity, 3)
        self.state.peak_intensity = max(self.state.peak_intensity, intensity)

        pid = _foreground_pid()
        # в игровом режиме берём только полноэкранные окна
        if self.games_only and pid and not looks_like_game(pid):
            pid = 0
        if pid != self.state.target_pid:
            self._retarget(pid)
        if not self.state.target_pid:
            return

        now = time.time()
        if intensity >= self.BOOST_AT:
            self._last_boost = now
            if not self.state.boosted:
                self._boost(intensity)
        elif self.state.boosted:
            quiet = now - self._last_boost
            if quiet >= self.HOLD_SECONDS and intensity <= self.RELEASE_AT:
                self._release()

        if self.state.boosted:
            self.state.boosted_seconds = round(now - self._boost_started
                                               + self._accumulated, 1)

    # ------------------------------------------------------------ цели

    def _retarget(self, pid: int) -> None:
        """Переключается на новое окно переднего плана."""
        if self.state.boosted:
            self._release()
        self._restore_all()

        if not pid:
            self.state.target_pid = 0
            self.state.target_name = ""
            return
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self.state.target_pid = 0
            self.state.target_name = ""
            return

        # своё окно не трогаем: приоритет самому себе поднимать незачем
        if name.lower() in ("kremen.exe", "python.exe", "pythonw.exe"):
            self.state.target_pid = 0
            self.state.target_name = ""
            return

        self.state.target_pid = pid
        self.state.target_name = name
        try:
            self._original[pid] = proc.nice()
        except (psutil.Error, OSError):
            self._original[pid] = NORMAL

        # держим игру на производительных ядрах
        if self._topology.hybrid:
            self._pin(pid, self._topology.performance_mask)
        self.state.priority = self._original.get(pid, NORMAL)

    def _pin(self, pid: int, mask: int) -> None:
        try:
            proc = psutil.Process(pid)
            cpus = [i for i in range(self._topology.logical_count)
                    if mask & (1 << i)]
            if cpus:
                proc.cpu_affinity(cpus)
        except (psutil.Error, OSError):
            pass

    # ------------------------------------------------------------ вмешательство

    def _boost(self, intensity: float) -> None:
        pid = self.state.target_pid
        if not pid:
            return
        if _set_priority(pid, HIGH):
            self.state.boosted = True
            self.state.priority = HIGH
            self.state.bursts += 1
            self._boost_started = time.time()
            self.state.history.insert(0, Burst(
                when=time.time(), reason="всплеск ввода",
                intensity=round(intensity, 2)))
            del self.state.history[60:]
            if self.park_background:
                self._park_background()

    def _release(self) -> None:
        pid = self.state.target_pid
        if pid:
            back = self._original.get(pid, ABOVE_NORMAL)
            # между всплесками держим «выше среднего»: так игра не проваливается
            # обратно в общую очередь, но и не давит систему
            _set_priority(pid, ABOVE_NORMAL if back == NORMAL else back)
            self.state.priority = ABOVE_NORMAL if back == NORMAL else back
        if self.state.boosted:
            self._accumulated += time.time() - self._boost_started
        self.state.boosted = False
        self._unpark_background()

    # ------------------------------------------------------------ фон

    def _park_background(self) -> None:
        """Прижимает прожорливый фон к экономичным ядрам на время всплеска."""
        if not self._topology.hybrid or not self._topology.efficient_mask:
            return
        target = self.state.target_pid
        parked = 0
        cpus = [i for i in range(self._topology.logical_count)
                if self._topology.efficient_mask & (1 << i)]
        if not cpus:
            return

        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            pid = proc.info.get("pid") or 0
            if pid in (0, 4, target) or pid in self._parked:
                continue
            if (proc.info.get("cpu_percent") or 0) < 8:
                continue
            name = (proc.info.get("name") or "").lower()
            if name in _NEVER_TOUCH:
                continue
            try:
                current = proc.cpu_affinity()
                mask = 0
                for index in current:
                    mask |= 1 << index
                self._parked[pid] = mask
                proc.cpu_affinity(cpus)
                parked += 1
            except (psutil.Error, OSError):
                self._parked.pop(pid, None)
                continue
        self.state.background_parked = len(self._parked)

    def _unpark_background(self) -> None:
        for pid, mask in list(self._parked.items()):
            try:
                proc = psutil.Process(pid)
                cpus = [i for i in range(self._topology.logical_count)
                        if mask & (1 << i)]
                if cpus:
                    proc.cpu_affinity(cpus)
            except (psutil.Error, OSError):
                pass
            self._parked.pop(pid, None)
        self.state.background_parked = 0

    def _restore_all(self) -> None:
        """Возвращает всё, что трогали, в исходное состояние."""
        for pid, priority in list(self._original.items()):
            _set_priority(pid, priority)
            self._original.pop(pid, None)
        self._unpark_background()


# Процессы, которые реактор не трогает никогда: ввод, звук, защита и античиты
_NEVER_TOUCH = {
    "system", "system idle process", "registry", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "smss.exe", "winlogon.exe", "dwm.exe",
    "audiodg.exe", "svchost.exe", "msmpeng.exe", "nissrv.exe",
    "easyanticheat.exe", "beservice.exe", "vgc.exe", "vgtray.exe",
    "faceitservice.exe", "kremen.exe",
}
