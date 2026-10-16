"""Кто и куда лезет из твоего компьютера.

Диспетчер задач показывает общую скорость сети и всё. На вопрос «какая
программа сейчас занимает канал и с каким сервером она разговаривает» он
не отвечает. А вопрос важный: и когда в игре скачет пинг, и когда хочется
понять, что за программа стучится в интернет без спроса.

Здесь список активных соединений сшивается с процессами, которые их открыли,
и группируется по программам. Плюс живая скорость по каждому сетевому
адаптеру из тех же счётчиков Windows, что и в остальном приложении.

Чего честно нет: скорости по каждой программе отдельно. Windows не отдаёт
такую разбивку обычным программам — для этого нужен драйвер-фильтр уровня
ядра. Притворяться приложение не будет: показывается число соединений и
адреса, а скорость — суммарная по адаптеру.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

import psutil

from .pdh import Counter
from .util import log

# Порты, по которым можно узнать характер соединения
_WELL_KNOWN = {
    80: "HTTP", 443: "HTTPS", 53: "DNS", 22: "SSH", 21: "FTP",
    25: "почта", 587: "почта", 993: "почта", 3389: "удалённый рабочий стол",
    27015: "игровой сервер Valve", 3478: "голосовой чат", 3479: "голосовой чат",
    5222: "чат", 6667: "IRC", 1935: "видеопоток",
}

# Программы, чьи соединения объяснимы и не должны пугать
_KNOWN_APPS = {
    "steam.exe": "Steam: библиотека, друзья, загрузки",
    "steamwebhelper.exe": "встроенный браузер Steam",
    "discord.exe": "Discord: чат и голос",
    "chrome.exe": "браузер Chrome",
    "msedge.exe": "браузер Edge",
    "firefox.exe": "браузер Firefox",
    "telegram.exe": "Telegram",
    "spotify.exe": "Spotify",
    "svchost.exe": "служебные процессы Windows",
    "nvcontainer.exe": "служба NVIDIA",
    "nvidia web helper.exe": "служба NVIDIA",
    "epicgameslauncher.exe": "лаунчер Epic Games",
    "eadesktop.exe": "лаунчер EA",
    "battle.net.exe": "лаунчер Battle.net",
    "code.exe": "Visual Studio Code",
    "explorer.exe": "проводник Windows",
    "searchapp.exe": "поиск Windows",
    "msteams.exe": "Microsoft Teams",
    "obs64.exe": "OBS: стриминг",
}


@dataclass
class Connection:
    """Одно активное соединение."""
    pid: int
    process: str
    local_port: int
    remote_ip: str
    remote_port: int
    status: str

    @property
    def service(self) -> str:
        return _WELL_KNOWN.get(self.remote_port, f"порт {self.remote_port}")


@dataclass
class AppConnections:
    """Все соединения одной программы."""
    process: str
    pid_count: int = 0
    connections: list = field(default_factory=list)
    known: str = ""

    @property
    def count(self) -> int:
        return len(self.connections)

    @property
    def remotes(self) -> list[str]:
        """Уникальные адреса, с которыми программа сейчас разговаривает."""
        seen = []
        for conn in self.connections:
            if conn.remote_ip and conn.remote_ip not in seen:
                seen.append(conn.remote_ip)
        return seen

    @property
    def services(self) -> list[str]:
        seen = []
        for conn in self.connections:
            name = conn.service
            if name not in seen:
                seen.append(name)
        return seen[:4]


def _is_local(address: str) -> bool:
    """Соединение внутри компьютера или домашней сети."""
    if not address:
        return True
    if address in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return True
    return (address.startswith("192.168.") or address.startswith("10.")
            or address.startswith("169.254.") or address.startswith("fe80:"))


def read_connections(include_local: bool = False) -> list[AppConnections]:
    """Активные соединения, сгруппированные по программам."""
    try:
        rows = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        log("Список соединений недоступен без прав администратора")
        return []
    except Exception as exc:
        log(f"Не удалось прочитать соединения: {exc}")
        return []

    names: dict[int, str] = {}
    groups: dict[str, AppConnections] = {}
    pids_seen: dict[str, set] = {}

    for row in rows:
        if row.status not in ("ESTABLISHED", "SYN_SENT", "CLOSE_WAIT"):
            continue
        remote = row.raddr.ip if row.raddr else ""
        if not include_local and _is_local(remote):
            continue

        pid = row.pid or 0
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name() if pid else "система"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                names[pid] = "неизвестно"
        name = names[pid]

        group = groups.get(name)
        if group is None:
            group = AppConnections(process=name,
                                   known=_KNOWN_APPS.get(name.lower(), ""))
            groups[name] = group
            pids_seen[name] = set()
        pids_seen[name].add(pid)
        group.connections.append(Connection(
            pid=pid, process=name,
            local_port=row.laddr.port if row.laddr else 0,
            remote_ip=remote,
            remote_port=row.raddr.port if row.raddr else 0,
            status=row.status))

    for name, group in groups.items():
        group.pid_count = len(pids_seen[name])

    result = list(groups.values())
    result.sort(key=lambda g: -g.count)
    return result


def resolve_host(address: str, timeout: float = 0.4) -> str:
    """Имя сервера по адресу. Пустая строка, если не определилось.

    Обратный поиск может занять секунды, поэтому вызывать его нужно только
    по требованию и в фоне, а не для всего списка сразу.
    """
    if not address or _is_local(address):
        return ""
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        return socket.gethostbyaddr(address)[0]
    except (OSError, socket.herror, socket.gaierror):
        return ""
    finally:
        socket.setdefaulttimeout(old)


# ---------------------------------------------------------------- скорость канала


class Bandwidth:
    """Живая скорость приёма и отдачи по всем сетевым адаптерам.

    Считается из тех же счётчиков, что показывает диспетчер задач, но
    читается напрямую и без запуска процессов.
    """

    RECV = r"\Network Interface(*)\Bytes Received/sec"
    SENT = r"\Network Interface(*)\Bytes Sent/sec"

    def __init__(self):
        self.recv = Counter(self.RECV, wildcard=True)
        self.sent = Counter(self.SENT, wildcard=True)
        self._fallback_at = 0.0
        self._fallback_prev = None

    @property
    def available(self) -> bool:
        return self.recv.available and self.sent.available

    def read(self) -> tuple[float, float] | None:
        """(приём, отдача) в мегабитах в секунду."""
        if self.available:
            down = self.recv.read_sum()
            up = self.sent.read_sum()
            if down is not None and up is not None:
                return round(down * 8 / 1_000_000, 2), round(up * 8 / 1_000_000, 2)
        return self._read_fallback()

    def _read_fallback(self) -> tuple[float, float] | None:
        """Запасной путь через psutil, если счётчики недоступны."""
        now = time.time()
        try:
            counters = psutil.net_io_counters()
        except Exception:
            return None
        if self._fallback_prev is None:
            self._fallback_prev = (counters.bytes_recv, counters.bytes_sent)
            self._fallback_at = now
            return None
        elapsed = now - self._fallback_at
        if elapsed <= 0:
            return None
        down = (counters.bytes_recv - self._fallback_prev[0]) / elapsed
        up = (counters.bytes_sent - self._fallback_prev[1]) / elapsed
        self._fallback_prev = (counters.bytes_recv, counters.bytes_sent)
        self._fallback_at = now
        return round(max(0, down) * 8 / 1_000_000, 2), round(max(0, up) * 8 / 1_000_000, 2)


def summary(groups: list[AppConnections]) -> str:
    """Короткий человеческий итог."""
    if not groups:
        return ("Внешних соединений сейчас нет. Либо всё закрыто, либо список "
                "недоступен без прав администратора.")
    total = sum(g.count for g in groups)
    unknown = [g for g in groups if not g.known]
    text = (f"Открыто внешних соединений: {total} у {len(groups)} программ. "
            f"Больше всех — {groups[0].process} ({groups[0].count}).")
    if unknown:
        text += (f" Не опознано программ: {len(unknown)} — это не значит, что "
                 "с ними что-то не так, просто их нет в списке известных.")
    return text
