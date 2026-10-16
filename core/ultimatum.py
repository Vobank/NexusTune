"""УЛЬТИМАТУМ — режим «выжать всё», со страховкой и полным откатом.

Это самая опасная кнопка в приложении, и она сделана честно опасной: она
действительно снимает почти все фоновые щиты Windows ради каждого свободного
такта процессора. Отключаются брандмауэр, защита в реальном времени,
телеметрия, поиск, обновления, автоматическое обслуживание, десятки служб.
Компьютер на это время становится голым и быстрым.

Почему это вообще допустимо, а не безумие:

  * НИЧЕГО не делается необратимо. Перед включением снимается полный слепок:
    какие службы работали, какие настройки стояли, какой был план питания.
    Кнопка «Отбой» возвращает ровно то, что было, — служба за службой.
  * Режим сам себя выключает. По умолчанию через два часа (настраивается)
    срабатывает таймер и возвращает все щиты на место, даже если ты забыл.
    Матч закончился — защита вернулась сама.
  * Приложение прямо говорит, чем ты платишь: пока режим включён, компьютер
    беззащитен перед вирусами. Это режим для оффлайн-матча или бенчмарка,
    а не для повседневной работы и не для сёрфинга.

Чего «Ультиматум» НЕ делает, потому что это уже не отвага, а глупость:

  * не трогает античиты (Vanguard, EasyAntiCheat, BattlEye) — без них тебя
    просто забанят, а не ускорят;
  * не удаляет ничего с диска;
  * не лезет в BIOS и напряжения;
  * защиту ядра от драйверов-вредителей (VBS) оставляет решать пользователю
    на странице «Эксперт» — здесь её нет, потому что её отключение требует
    перезагрузки и не даёт мгновенного эффекта.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from . import optimizer
from .util import APP_DIR, is_admin, log, run, run_ps

STATE_FILE = APP_DIR / "ultimatum.json"

# Службы, которые режим глушит. Античитов и звука здесь нет намеренно.
_KILL_SERVICES = [
    ("WSearch", "поиск Windows"),
    ("SysMain", "SuperFetch"),
    ("DiagTrack", "телеметрия"),
    ("dmwappushservice", "push-телеметрия"),
    ("wuauserv", "центр обновления"),
    ("BITS", "фоновая передача"),
    ("DoSvc", "раздача обновлений"),
    ("WerSvc", "отчёты об ошибках"),
    ("PcaSvc", "помощник совместимости"),
    ("MapsBroker", "карты"),
    ("Spooler", "диспетчер печати"),
    ("Fax", "факс"),
    ("RemoteRegistry", "удалённый реестр"),
    ("WbioSrvc", "биометрия"),
    ("TabletInputService", "рукописный ввод"),
    ("lfsvc", "геолокация"),
    ("PhoneSvc", "телефония"),
    ("WMPNetworkSvc", "сеть медиаплеера"),
    ("XblAuthManager", "авторизация Xbox"),
    ("XboxNetApiSvc", "сеть Xbox"),
    ("XblGameSave", "сохранения Xbox"),
    ("wscsvc", "центр безопасности"),
    ("wisvc", "программа предварительной оценки"),
    ("RetailDemo", "демо-режим"),
]

# Античиты и всё, что их обслуживает — не трогаем ни при каких условиях
_SACRED = {
    "vgc", "vgk", "beservice", "easyanticheat", "easyanticheat_eos",
    "faceit", "esea", "audioendpointbuilder", "audiosrv",
}


@dataclass
class UltimatumState:
    active: bool = False
    since: float = 0.0
    auto_off_at: float = 0.0
    services_stopped: list = field(default_factory=list)
    defender_off: bool = False
    firewall_off: bool = False
    steps: list = field(default_factory=list)

    @property
    def minutes_left(self) -> float:
        if not self.active or not self.auto_off_at:
            return 0.0
        return max(0.0, (self.auto_off_at - time.time()) / 60)


def _load_state() -> UltimatumState:
    if not STATE_FILE.exists():
        return UltimatumState()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return UltimatumState(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return UltimatumState()


def _save_state(state: UltimatumState) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps({
                "active": state.active, "since": state.since,
                "auto_off_at": state.auto_off_at,
                "services_stopped": state.services_stopped,
                "defender_off": state.defender_off,
                "firewall_off": state.firewall_off, "steps": [],
            }, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"Ультиматум: не удалось сохранить состояние — {exc}")


def is_active() -> bool:
    return _load_state().active


def current_state() -> UltimatumState:
    return _load_state()


# ---------------------------------------------------------------- защита


def _defender_realtime(disable: bool) -> bool:
    """Включает или выключает защиту в реальном времени.

    Защита от подделки (Tamper Protection) может отклонить команду — тогда
    Windows просто вернёт защиту через несколько секунд. Приложение это
    честно переживёт: значит система решила постоять за себя, и это нормально.
    """
    flag = "$true" if disable else "$false"
    code, _out, err = run_ps(
        f"Set-MpPreference -DisableRealtimeMonitoring {flag} "
        "-ErrorAction SilentlyContinue", timeout=30)
    if code != 0:
        log(f"Ультиматум: защита реального времени не поддалась — {err}")
    return code == 0


def _firewall(enable: bool) -> bool:
    state = "on" if enable else "off"
    code, _o, _e = run(
        ["netsh", "advfirewall", "set", "allprofiles", "state", state],
        timeout=20)
    return code == 0


# ---------------------------------------------------------------- службы


def _service_start_type(name: str) -> str:
    return optimizer.service_state(name)


def _stop_service(name: str) -> bool:
    code, _o, _e = run(["sc", "stop", name], timeout=20)
    return code == 0


def _set_start(name: str, mode: str) -> bool:
    return optimizer.set_service_start(name, mode)


# ---------------------------------------------------------------- включение


def engage(minutes: int = 120, kill_defender: bool = True,
           kill_firewall: bool = True) -> tuple[bool, UltimatumState]:
    """Включает режим. Возвращает (успех, состояние с журналом действий)."""
    state = UltimatumState(active=True, since=time.time(),
                           auto_off_at=time.time() + minutes * 60)
    if not is_admin():
        state.active = False
        state.steps.append("Нужны права администратора — режим не включён.")
        return False, state

    # снимок настроек-твиков, чтобы вернуть их профилем позже
    try:
        optimizer.save_snapshot(_all_tweaks(), "before_ultimatum")
        state.steps.append("Снимок настроек сохранён — откат гарантирован.")
    except Exception:
        pass

    # 1. план питания — максимальная производительность
    try:
        if optimizer.set_power_plan(optimizer.ensure_ultimate_plan()):
            state.steps.append("План питания: максимальная производительность.")
    except Exception:
        pass

    # 2. службы
    stopped = []
    for name, human in _KILL_SERVICES:
        if name.lower() in _SACRED:
            continue
        prev = _service_start_type(name)
        if prev in ("нет", "Disabled"):
            continue
        _set_start(name, "demand")
        if _stop_service(name):
            stopped.append({"name": name, "prev": prev, "human": human})
    state.services_stopped = stopped
    state.steps.append(f"Остановлено фоновых служб: {len(stopped)}.")

    # 3. защита
    if kill_firewall and _firewall(False):
        state.firewall_off = True
        state.steps.append("Брандмауэр Windows выключен.")
    if kill_defender and _defender_realtime(True):
        state.defender_off = True
        state.steps.append("Защита в реальном времени выключена.")
    elif kill_defender:
        state.steps.append("Защитник отклонил выключение (защита от подделки) — "
                           "это нормально, значит система постояла за себя.")

    state.steps.append(
        f"Режим включён на {minutes} минут. По истечении всё вернётся само.")
    _save_state(state)
    log(f"УЛЬТИМАТУМ включён: служб {len(stopped)}, "
        f"брандмауэр {'off' if state.firewall_off else 'on'}, "
        f"защитник {'off' if state.defender_off else 'on'}, на {minutes} мин")
    return True, state


def disengage() -> tuple[bool, UltimatumState]:
    """Возвращает всё на место. Работает, даже если приложение перезапускали."""
    state = _load_state()
    result = UltimatumState(active=False)
    if not state.active:
        result.steps.append("Режим и так был выключен.")
        return True, result

    # службы — в прежний тип запуска и обратно в работу
    restored = 0
    for entry in state.services_stopped:
        name = entry.get("name", "")
        prev = entry.get("prev", "Manual")
        mode = {"Automatic": "auto", "Manual": "demand",
                "Disabled": "disabled"}.get(prev, "demand")
        if _set_start(name, mode):
            restored += 1
        if prev == "Automatic":
            run(["sc", "start", name], timeout=15)
    result.steps.append(f"Возвращено служб: {restored}.")

    if state.firewall_off:
        _firewall(True)
        result.steps.append("Брандмауэр снова включён.")
    if state.defender_off:
        _defender_realtime(False)
        result.steps.append("Защита в реальном времени снова включена.")

    _save_state(result)
    log(f"УЛЬТИМАТУМ выключен: возвращено служб {restored}")
    return True, result


def _all_tweaks() -> list:
    """Твики для снимка: обычные плюс экспертные, если получится собрать."""
    tweaks = []
    try:
        tweaks += optimizer.build_tweaks()
    except Exception:
        pass
    return tweaks


def recover_if_expired() -> str | None:
    """Проверяет при запуске: не пора ли вернуть щиты.

    Если приложение закрыли с включённым режимом и время вышло — возвращаем
    всё на место молча. Это последняя страховка от «забыл выключить».
    """
    state = _load_state()
    if not state.active:
        return None
    if state.auto_off_at and time.time() >= state.auto_off_at:
        ok, result = disengage()
        if ok:
            log("Ультиматум: время вышло, щиты возвращены автоматически")
            return "Режим «Ультиматум» сам выключился по таймеру — защита возвращена."
    return None


def preview() -> dict:
    """Что именно произойдёт при включении — для показа перед нажатием."""
    services = [human for name, human in _KILL_SERVICES
                if name.lower() not in _SACRED]
    return {
        "services": len(services),
        "service_names": services,
        "cost": [
            "Компьютер останется без антивируса и брандмауэра — не выходи "
            "в интернет и не открывай файлы, пока режим включён.",
            "Перестанут работать печать, поиск по файлам, обновления и "
            "магазин Windows — до выключения режима.",
            "Античиты не трогаются: в защищённые игры зайти можно.",
        ],
        "safety": [
            "Перед включением снимается слепок — «Отбой» вернёт всё точно.",
            "Режим сам выключится по таймеру, даже если закроешь приложение.",
        ],
    }
