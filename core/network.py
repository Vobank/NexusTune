"""Сеть: качество связи, пинг до игровых серверов и настройки адаптера.

Пинг меряется системной утилитой, потому что она использует настоящий ICMP,
а не эмуляцию через TCP. Джиттер считается как средний разброс между
соседними ответами — именно он ощущается как рывки в онлайне.
"""
from __future__ import annotations

import re
import statistics
import subprocess
import threading
from dataclasses import dataclass, field

from .util import CREATE_NO_WINDOW, log, ps_json, run, run_ps


@dataclass
class PingResult:
    """Результат проверки одного адреса."""
    name: str
    host: str
    sent: int = 0
    received: int = 0
    min_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 100.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.received > 0

    @property
    def quality(self) -> tuple[str, str]:
        """Оценка качества связи: (слово, пояснение)."""
        if not self.ok:
            return "не отвечает", ("Сервер не ответил на пинг. Часть сервисов "
                                   "запрещает ICMP из соображений безопасности — "
                                   "это не значит, что игра не работает.")
        if self.loss_percent > 5:
            return "потери пакетов", (
                f"Теряется {self.loss_percent:.0f}% пакетов — в онлайне это телепорты "
                "и отмена выстрелов. Проверь кабель и роутер.")
        if self.avg_ms < 30 and self.jitter_ms < 5:
            return "отлично", "Низкая задержка и ровный канал."
        if self.avg_ms < 60 and self.jitter_ms < 12:
            return "хорошо", "Для большинства игр этого достаточно."
        if self.jitter_ms >= 20:
            return "нестабильно", (
                f"Разброс {self.jitter_ms:.0f} мс. Скачущий пинг мешает сильнее, "
                "чем просто высокий. Обычно виноват Wi-Fi или загруженный канал.")
        if self.avg_ms < 120:
            return "терпимо", "Заметно в шутерах, приемлемо в остальном."
        return "высокая задержка", "Сервер далеко или канал перегружен."


# Точки, по которым удобно оценивать канал: разные регионы и разные сети
# Epic Games, EA и PlayStation намеренно не отвечают на ICMP, поэтому их здесь нет:
# показывать «нет связи» для сервиса, который просто запрещает пинг, — вводить в заблуждение.
DEFAULT_TARGETS = [
    ("Google DNS", "8.8.8.8"),
    ("Cloudflare", "1.1.1.1"),
    ("Steam", "steamcommunity.com"),
    ("Riot / Valorant", "riotgames.com"),
    ("Blizzard", "battle.net"),
    ("Discord", "discord.com"),
    ("Ubisoft", "ubisoft.com"),
    ("Wargaming", "wargaming.net"),
]

# Строки ответа зависят от языка Windows, поэтому берём числа регулярными выражениями
_TIME_PATTERN = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)", re.IGNORECASE)
_LOSS_PATTERN = re.compile(r"(\d+)%")


def ping(host: str, count: int = 10, timeout_ms: int = 1500) -> PingResult:
    """Пингует адрес и считает задержку, джиттер и потери."""
    result = PingResult(name=host, host=host, sent=count)
    try:
        proc = subprocess.run(
            ["ping", "-n", str(count), "-w", str(timeout_ms), host],
            capture_output=True, text=True, encoding="cp866", errors="replace",
            timeout=count * (timeout_ms / 1000) + 12,
            creationflags=CREATE_NO_WINDOW)
        output = proc.stdout or ""
    except subprocess.TimeoutExpired:
        result.error = "Проверка не уложилась в отведённое время."
        return result
    except (OSError, ValueError) as exc:
        result.error = str(exc)
        return result

    # Считаем только строки настоящих ответов: в итоговой сводке ping повторяет
    # минимум, максимум и среднее, и они бы посчитались как лишние ответы.
    times: list[float] = []
    for line in output.splitlines():
        if "TTL=" not in line.upper():
            continue
        match = _TIME_PATTERN.search(line)
        if match:
            times.append(float(match.group(1).replace(",", ".")))
    result.received = len(times)
    if times:
        result.min_ms = round(min(times), 1)
        result.max_ms = round(max(times), 1)
        result.avg_ms = round(statistics.fmean(times), 1)
        if len(times) > 1:
            # джиттер — средняя разница между соседними ответами
            deltas = [abs(times[i] - times[i - 1]) for i in range(1, len(times))]
            result.jitter_ms = round(statistics.fmean(deltas), 1)
    result.loss_percent = round((count - result.received) / count * 100, 1)
    return result


def ping_all(targets=None, count: int = 10, progress=None) -> list[PingResult]:
    """Проверяет несколько адресов параллельно."""
    targets = targets or DEFAULT_TARGETS
    results: dict[str, PingResult] = {}
    threads: list[threading.Thread] = []

    def worker(name: str, host: str):
        item = ping(host, count)
        item.name = name
        results[name] = item
        if progress:
            progress(len(results), len(targets))

    for name, host in targets:
        thread = threading.Thread(target=worker, args=(name, host), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=40)

    return [results.get(name, PingResult(name=name, host=host))
            for name, host in targets]


# ---------------------------------------------------------------- состояние адаптера


@dataclass
class AdapterInfo:
    name: str = ""
    kind: str = ""
    speed: str = ""
    wifi: bool = False
    driver_date: str = ""
    ipv4: str = ""


def active_adapter() -> AdapterInfo | None:
    """Активный сетевой адаптер."""
    data = ps_json(
        "Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
        "Where-Object Status -eq 'Up' | Select-Object -First 1 "
        "Name,InterfaceDescription,LinkSpeed,MediaType,DriverDate", timeout=30)
    if not isinstance(data, dict):
        return None
    info = AdapterInfo(
        name=str(data.get("Name") or ""),
        kind=str(data.get("InterfaceDescription") or ""),
        speed=str(data.get("LinkSpeed") or ""),
        driver_date=str(data.get("DriverDate") or ""),
    )
    media = str(data.get("MediaType") or "")
    info.wifi = "802.11" in media or "wireless" in info.kind.lower()

    address = ps_json(
        "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } | "
        "Select-Object -First 1).IPAddress", timeout=25)
    if isinstance(address, str):
        info.ipv4 = address
    return info


def network_advice(adapter: AdapterInfo | None, results: list[PingResult]) -> list[str]:
    """Понятные советы по результатам проверки."""
    tips: list[str] = []
    if adapter and adapter.wifi:
        tips.append(
            "Ты играешь по Wi-Fi. Даже отличный Wi-Fi даёт скачки задержки, которых "
            "не бывает на кабеле. Провод — самое дешёвое улучшение онлайн-игры.")
    good = [r for r in results if r.ok]
    if not good:
        tips.append("Ни один адрес не ответил. Похоже, проблема с интернетом целиком.")
        return tips

    worst_jitter = max(good, key=lambda r: r.jitter_ms)
    if worst_jitter.jitter_ms >= 15:
        tips.append(
            f"Разброс задержки до {worst_jitter.jitter_ms:.0f} мс ({worst_jitter.name}). "
            "Скачущий пинг ощущается хуже ровного высокого. Проверь, не качает ли "
            "что-то в фоне, и не сидит ли кто-то ещё на канале.")

    lossy = [r for r in good if r.loss_percent > 2]
    if lossy:
        names = ", ".join(r.name for r in lossy[:3])
        tips.append(f"Есть потери пакетов: {names}. Начни с замены кабеля и "
                    "перезагрузки роутера — это чинит большинство случаев.")

    average = statistics.fmean([r.avg_ms for r in good])
    if average > 100:
        tips.append(f"Средняя задержка {average:.0f} мс — многовато. "
                    "Выбирай игровые серверы поближе к своему региону.")
    elif average < 35:
        tips.append(f"Средняя задержка {average:.0f} мс — канал хороший, "
                    "тормоза в играх стоит искать не в сети.")

    tips.append(
        "Твики «Отключить алгоритм Нейгла» и «Снять резерв канала под QoS» "
        "на странице оптимизации убирают лишние миллисекунды на стороне Windows.")
    return tips


def flush_dns() -> tuple[bool, str]:
    """Сбрасывает кэш DNS — помогает, когда сайты и серверы резолвятся неправильно."""
    code, _out, err = run(["ipconfig", "/flushdns"], timeout=25)
    return (code == 0), ("Кэш DNS очищен." if code == 0 else err or "Не удалось очистить кэш.")


def reset_network_stack() -> tuple[bool, str]:
    """Сбрасывает сетевой стек Windows. Требует перезагрузки."""
    steps = [
        ["netsh", "winsock", "reset"],
        ["netsh", "int", "ip", "reset"],
    ]
    ok = True
    for command in steps:
        code, _out, _err = run(command, timeout=40)
        ok &= code == 0
    if ok:
        log("Сетевой стек Windows сброшен")
        return True, ("Сетевой стек сброшен. Изменения вступят в силу после "
                      "перезагрузки компьютера.")
    return False, "Не удалось сбросить сетевой стек — нужны права администратора."
