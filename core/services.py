"""Службы Windows: полный список, объяснения и безопасное управление.

Списки «отключи эти 40 служб» в интернете опасны тем, что не учитывают, чем
человек пользуется. Отключил «Диспетчер печати» — и принтер молча перестал
работать. Отключил Bluetooth — и наушники не подключаются. Через полгода
никто не помнит, что именно трогал.

Здесь другой подход. Каждая служба отнесена к одному из четырёх классов:

  критическая  — трогать нельзя, интерфейс не даст;
  безопасная   — на игровом компьютере не нужна почти никому, есть объяснение;
  по ситуации  — зависит от того, чем ты пользуешься; решать тебе, но приложение
                 расскажет, что именно перестанет работать;
  неизвестная  — чужая служба, приложение о ней ничего не знает и советов не даёт.

Перед изменениями сохраняется снимок: вернуть всё как было можно одной кнопкой.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import psutil

from .util import APP_DIR, is_admin, log, run

SNAPSHOT_FILE = APP_DIR / "services_snapshot.json"

CRITICAL = "критическая"
SAFE = "безопасная"
SITUATIONAL = "по ситуации"
UNKNOWN = "неизвестная"

START_TYPES = {
    "automatic": "автоматически",
    "manual": "вручную",
    "disabled": "отключена",
    "forced manual": "вручную",
    "unknown": "неизвестно",
}

# Режимы для sc config
_SC_MODES = {"automatic": "auto", "manual": "demand", "disabled": "disabled"}


# ---------------------------------------------------------------- база знаний

# имя службы -> (класс, что это, что произойдёт при отключении)
KNOWN: dict[str, tuple[str, str, str]] = {
    # ---- критические: без них система разваливается
    "RpcSs": (CRITICAL, "Удалённый вызов процедур — основа всего в Windows.",
              "Система не загрузится."),
    "DcomLaunch": (CRITICAL, "Запуск COM-серверов и процессов служб.",
                   "Система не загрузится."),
    "Power": (CRITICAL, "Управление электропитанием.", "Компьютер не уснёт и не проснётся."),
    "PlugPlay": (CRITICAL, "Обнаружение устройств.", "Перестанет работать железо."),
    "BrokerInfrastructure": (CRITICAL, "Инфраструктура фоновых задач.",
                             "Половина Windows перестанет работать."),
    "CoreMessagingRegistrar": (CRITICAL, "Обмен сообщениями внутри системы.",
                               "Система станет нестабильной."),
    "LSM": (CRITICAL, "Диспетчер локальных сеансов.", "Не получится войти в систему."),
    "Winmgmt": (CRITICAL, "WMI — через него работает половина программ.",
                "Перестанут работать датчики и диспетчер задач."),
    "EventLog": (CRITICAL, "Журнал событий Windows.",
                 "Пропадёт диагностика, часть служб не запустится."),
    "Schedule": (CRITICAL, "Планировщик заданий.",
                 "Перестанут работать обновления и обслуживание, в том числе наше."),
    "AudioEndpointBuilder": (CRITICAL, "Построение звуковых устройств.",
                             "Пропадёт звук."),
    "Audiosrv": (CRITICAL, "Звук Windows.", "Пропадёт звук."),
    "Themes": (CRITICAL, "Оформление рабочего стола.",
               "Интерфейс станет как в Windows 95 и начнёт глючить."),
    "UserManager": (CRITICAL, "Управление пользовательскими сеансами.",
                    "Не получится войти."),
    "ProfSvc": (CRITICAL, "Загрузка профиля пользователя.", "Не получится войти."),
    "Dhcp": (CRITICAL, "Получение адреса в сети.", "Пропадёт интернет."),
    "Dnscache": (CRITICAL, "Кэш DNS.",
                 "Сайты будут открываться заметно медленнее или не открываться."),
    "nsi": (CRITICAL, "Служба сетевых интерфейсов.", "Пропадёт сеть."),
    "NlaSvc": (CRITICAL, "Определение сетевого расположения.",
               "Сеть будет считаться неопознанной, часть программ перестанет работать."),
    "CryptSvc": (CRITICAL, "Криптография и проверка подписей.",
                 "Перестанут ставиться обновления и часть программ."),
    "BFE": (CRITICAL, "Базовый механизм фильтрации — фундамент брандмауэра.",
            "Пропадёт защита сети и сломается интернет."),
    "mpssvc": (CRITICAL, "Брандмауэр Windows.", "Компьютер останется без защиты."),
    "gpsvc": (CRITICAL, "Групповые политики.", "Система станет нестабильной."),
    "SamSs": (CRITICAL, "Диспетчер учётных записей.", "Не получится войти."),
    "StateRepository": (CRITICAL, "Хранилище состояния приложений.",
                        "Сломается меню «Пуск» и магазин."),
    "TextInputManagementService": (CRITICAL, "Ввод текста.",
                                   "Перестанет работать клавиатура в части программ."),

    # ---- безопасные: игровому компьютеру не нужны
    "DiagTrack": (SAFE, "Телеметрия: собирает данные о том, как ты пользуешься ПК.",
                  "Ничего не сломается. Microsoft перестанет получать статистику."),
    "dmwappushservice": (SAFE, "Передача данных телеметрии по push.",
                         "Ничего не сломается."),
    "diagnosticshub.standardcollector.service":
        (SAFE, "Сбор диагностики для разработчиков.",
         "Ничего не сломается, если ты не отлаживаешь программы."),
    "SysMain": (SAFE, "SuperFetch: предугадывает, что ты запустишь, и греет диск.",
                "На SSD пользы нет, а фоновая запись прекратится. "
                "На старом жёстком диске лучше оставить."),
    "WSearch": (SAFE, "Индексация поиска по файлам.",
                "Поиск по содержимому файлов станет медленным. "
                "Поиск по именам продолжит работать."),
    "MapsBroker": (SAFE, "Скачивание офлайн-карт.",
                   "Ничего не сломается, если не пользуешься приложением «Карты»."),
    "RetailDemo": (SAFE, "Демонстрационный режим для магазинов техники.",
                   "Не нужен вообще никому дома."),
    "WalletService": (SAFE, "Кошелёк Windows.", "Ничего не сломается."),
    "PcaSvc": (SAFE, "Помощник по совместимости программ.",
               "Перестанут выскакивать окна «программа работает неправильно»."),
    "WerSvc": (SAFE, "Отправка отчётов об ошибках в Microsoft.",
               "Отчёты перестанут уходить. Локальные дампы останутся."),
    "Fax": (SAFE, "Факс.", "Тебе не нужен факс."),
    "RemoteRegistry": (SAFE, "Правка твоего реестра по сети.",
                       "Отключение делает компьютер безопаснее."),
    "RemoteAccess": (SAFE, "Маршрутизация и удалённый доступ.",
                     "Ничего не сломается на домашнем ПК."),
    "SharedAccess": (SAFE, "Общий доступ к интернету (раздача с ПК).",
                     "Перестанет работать раздача интернета с компьютера."),
    "lfsvc": (SAFE, "Геолокация.",
              "Приложения перестанут узнавать твоё местоположение."),
    "WMPNetworkSvc": (SAFE, "Сетевой доступ Windows Media Player.",
                      "Ничего не сломается."),
    "XblAuthManager": (SAFE, "Авторизация Xbox Live.",
                       "Не трогай, если играешь в игры из Microsoft Store "
                       "или Game Pass — иначе они не запустятся."),
    "XblGameSave": (SAFE, "Сохранения игр Xbox в облаке.",
                    "Сохранения игр Game Pass перестанут синхронизироваться."),
    "XboxNetApiSvc": (SAFE, "Сетевые функции Xbox.",
                      "Мультиплеер в играх Game Pass может перестать работать."),
    "AJRouter": (SAFE, "AllJoyn: связь с умным домом.", "Ничего не сломается."),
    "PhoneSvc": (SAFE, "Телефония.", "Ничего не сломается на настольном ПК."),
    "TabletInputService": (SAFE, "Рукописный ввод и экранная клавиатура.",
                           "Отключай, только если нет сенсорного экрана."),
    "SEMgrSvc": (SAFE, "Платежи и NFC.", "Ничего не сломается на настольном ПК."),
    "MessagingService": (SAFE, "SMS-сообщения.", "Ничего не сломается."),
    "OneSyncSvc": (SAFE, "Синхронизация почты, контактов и календаря.",
                   "Стандартные приложения «Почта» и «Календарь» перестанут обновляться."),
    "DoSvc": (SAFE, "Оптимизация доставки: раздаёт обновления другим ПК в сети.",
              "Обновления будут качаться только от Microsoft. "
              "Освобождается канал и место на диске."),
    "WpcMonSvc": (SAFE, "Родительский контроль.", "Ничего не сломается, если он не нужен."),
    "SCardSvr": (SAFE, "Смарт-карты.", "Ничего не сломается без считывателя карт."),
    "ScDeviceEnum": (SAFE, "Перечисление смарт-карт.", "То же самое."),
    "SessionEnv": (SAFE, "Настройка удалённого рабочего стола.",
                   "Перестанет работать подключение к этому ПК извне."),
    "TermService": (SITUATIONAL, "Службы удалённых рабочих столов.",
                    "Нельзя будет подключиться к компьютеру удалённо. "
                    "Если пользуешься RDP или Parsec — оставь."),

    # ---- по ситуации: зависит от твоего железа и привычек
    "Spooler": (SITUATIONAL, "Диспетчер печати.",
                "Печать перестанет работать полностью. Отключай, только если "
                "принтера нет и не будет."),
    "bthserv": (SITUATIONAL, "Поддержка Bluetooth.",
                "Перестанут подключаться Bluetooth-наушники, мыши и геймпады."),
    "BTAGService": (SITUATIONAL, "Голосовой шлюз Bluetooth.",
                    "Bluetooth-гарнитура потеряет микрофон."),
    "BthAvctpSvc": (SITUATIONAL, "Управление Bluetooth-аудио.",
                    "Кнопки на Bluetooth-наушниках перестанут работать."),
    "WlanSvc": (SITUATIONAL, "Wi-Fi.",
                "Пропадёт Wi-Fi. Отключай только если подключён кабелем "
                "и Wi-Fi не нужен."),
    "wscsvc": (SITUATIONAL, "Центр безопасности Windows.",
               "Пропадут уведомления о защите. Сам антивирус продолжит работать."),
    "wuauserv": (SITUATIONAL, "Центр обновления Windows.",
                 "Обновления перестанут приходить, включая исправления "
                 "безопасности. Windows со временем всё равно включит службу обратно."),
    "BITS": (SITUATIONAL, "Фоновая передача файлов.",
             "Сломаются обновления Windows и часть загрузчиков."),
    "InstallService": (SITUATIONAL, "Установка приложений из магазина.",
                       "Магазин Windows перестанет ставить программы."),
    "FontCache": (SITUATIONAL, "Кэш шрифтов.",
                  "Текст будет отрисовываться чуть медленнее. Экономия крошечная."),
    "stisvc": (SITUATIONAL, "Сканеры и камеры.",
               "Перестанут работать сканер и часть веб-камер."),
    "WbioSrvc": (SITUATIONAL, "Биометрия.",
                 "Перестанет работать вход по отпечатку и Windows Hello."),
    "SensorService": (SITUATIONAL, "Датчики: освещённость, поворот экрана.",
                      "На настольном ПК обычно не нужны."),
    "TokenBroker": (SITUATIONAL, "Вход в приложения Microsoft.",
                    "Магазин и приложения Microsoft перестанут логиниться."),
    "CDPUserSvc": (SITUATIONAL, "Связь с телефоном и другими устройствами.",
                   "Перестанет работать «Ваш телефон» и передача между устройствами."),
    "camsvc": (SITUATIONAL, "Доступ приложений к возможностям системы.",
               "Приложения из магазина могут потерять доступ к камере и микрофону."),
}

# Признаки служб антивируса, драйверов и античитов — их не трогаем никогда
_PROTECTED_HINTS = (
    "defender", "sense", "antivirus", "kaspersky", "eset", "avast", "avg",
    "bitdefender", "malware", "nvidia", "amd", "intel(r)", "realtek",
    "easyanticheat", "battleye", "vanguard", "vgc", "faceit", "steam",
    "epicgames", "riot", "wacom", "logi", "razer", "corsair", "steelseries",
    "asus", "msi", "gigabyte", "focusrite", "elgato",
)


@dataclass
class ServiceInfo:
    """Одна служба Windows."""
    name: str
    display: str
    status: str                  # running / stopped
    start_type: str              # automatic / manual / disabled
    pid: int = 0
    memory_mb: float = 0.0
    description: str = ""
    kind: str = UNKNOWN
    what: str = ""
    consequence: str = ""
    protected: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def start_text(self) -> str:
        return START_TYPES.get(self.start_type, self.start_type)

    @property
    def can_change(self) -> bool:
        return self.kind != CRITICAL and not self.protected

    @property
    def tone(self) -> str:
        return {CRITICAL: "danger", SAFE: "ok",
                SITUATIONAL: "warn"}.get(self.kind, "dim")


def _classify(name: str, display: str, binpath: str) -> tuple[str, str, str, bool]:
    """Класс службы, описание, последствия и признак «не трогать»."""
    known = KNOWN.get(name)
    if known:
        return known[0], known[1], known[2], known[0] == CRITICAL

    haystack = f"{name} {display} {binpath}".lower()
    if any(hint in haystack for hint in _PROTECTED_HINTS):
        return (UNKNOWN, "Служба стороннего производителя: драйвер, антивирус "
                         "или защита игры.",
                "Приложение такие службы не трогает — цена ошибки слишком велика.",
                True)
    return UNKNOWN, "", "", False


def read_services(include_stopped: bool = True) -> list[ServiceInfo]:
    """Полный список служб с классификацией."""
    result: list[ServiceInfo] = []
    memory: dict[int, float] = {}

    for service in psutil.win_service_iter():
        try:
            data = service.as_dict()
        except Exception:
            continue

        status = str(data.get("status") or "")
        if not include_stopped and status != "running":
            continue

        name = str(data.get("name") or "")
        display = str(data.get("display_name") or name)
        binpath = str(data.get("binpath") or "")
        kind, what, consequence, protected = _classify(name, display, binpath)

        pid = int(data.get("pid") or 0)
        mb = 0.0
        if pid:
            if pid not in memory:
                try:
                    memory[pid] = psutil.Process(pid).memory_info().rss / (1024 ** 2)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    memory[pid] = 0.0
            mb = round(memory[pid], 1)

        result.append(ServiceInfo(
            name=name, display=display, status=status,
            start_type=str(data.get("start_type") or "unknown"),
            pid=pid, memory_mb=mb,
            description=str(data.get("description") or ""),
            kind=kind, what=what, consequence=consequence, protected=protected))

    result.sort(key=lambda s: (s.kind != SAFE, s.display.lower()))
    return result


def total_memory_mb(services: list[ServiceInfo]) -> float:
    """Сколько памяти реально занимают службы.

    Складывать память по каждой службе нельзя: десятки служб живут внутри
    общих процессов svchost, и один и тот же процесс посчитался бы много раз.
    Поэтому каждый процесс учитывается ровно один раз.
    """
    seen: dict[int, float] = {}
    for service in services:
        if service.running and service.pid:
            seen[service.pid] = service.memory_mb
    return round(sum(seen.values()), 1)


def summary(services: list[ServiceInfo]) -> str:
    """Короткий итог для шапки страницы."""
    running = sum(1 for s in services if s.running)
    can_disable = recommended_to_disable(services)
    memory = total_memory_mb(services)
    text = (f"Всего служб: {len(services)} · запущено {running} · "
            f"они занимают {memory:.0f} МБ в {len({s.pid for s in services if s.running and s.pid})} процессах")
    if can_disable:
        working = sum(1 for s in can_disable if s.running)
        text += (f" · безопасно отключить можно {len(can_disable)}"
                 + (f", из них работают прямо сейчас {working}" if working else
                    ", но сейчас ни одна из них не запущена"))
    return text + "."


# ---------------------------------------------------------------- изменения


def set_start_type(name: str, mode: str) -> tuple[bool, str]:
    """Меняет тип запуска службы: automatic / manual / disabled."""
    if not is_admin():
        return False, "Нужны права администратора."
    sc_mode = _SC_MODES.get(mode)
    if not sc_mode:
        return False, "Неизвестный режим запуска."

    code, out, err = run(["sc", "config", name, f"start={sc_mode}"], timeout=20)
    if code != 0:
        return False, (err or out or "").strip() or "Windows отклонила изменение."
    log(f"Служба {name}: тип запуска -> {mode}")
    return True, f"Тип запуска изменён на «{START_TYPES.get(mode, mode)}»."


def stop_service(name: str) -> tuple[bool, str]:
    if not is_admin():
        return False, "Нужны права администратора."
    code, out, err = run(["sc", "stop", name], timeout=30)
    text = (out + " " + err).lower()
    if code == 0 or "1062" in text:          # 1062 — служба и так не запущена
        log(f"Служба {name} остановлена")
        return True, "Служба остановлена."
    return False, (err or out or "").strip() or "Не удалось остановить службу."


def start_service(name: str) -> tuple[bool, str]:
    if not is_admin():
        return False, "Нужны права администратора."
    code, out, err = run(["sc", "start", name], timeout=30)
    text = (out + " " + err).lower()
    if code == 0 or "1056" in text:          # 1056 — уже запущена
        log(f"Служба {name} запущена")
        return True, "Служба запущена."
    return False, (err or out or "").strip() or "Не удалось запустить службу."


def disable(name: str, services: list[ServiceInfo] | None = None) -> tuple[bool, str]:
    """Останавливает службу и запрещает её запуск."""
    info = None
    if services:
        info = next((s for s in services if s.name == name), None)
    if info is not None and not info.can_change:
        return False, ("Эта служба помечена как критическая или чужая — "
                       "приложение её не трогает.")

    ok, message = set_start_type(name, "disabled")
    if not ok:
        return False, message
    stop_service(name)
    return True, "Служба отключена и остановлена."


# ---------------------------------------------------------------- снимок


def save_snapshot(services: list[ServiceInfo]) -> None:
    """Запоминает тип запуска всех служб перед изменениями."""
    data = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "states": {s.name: s.start_type for s in services},
    }
    try:
        SNAPSHOT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        log(f"Снимок служб сохранён: {len(data['states'])} записей")
    except OSError as exc:
        log(f"Не удалось сохранить снимок служб: {exc}")


def load_snapshot() -> dict | None:
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def restore_snapshot(progress=None) -> tuple[int, int]:
    """Возвращает типы запуска из снимка. Отдаёт (изменено, всего)."""
    snapshot = load_snapshot()
    if not snapshot:
        return 0, 0
    states = snapshot.get("states", {})
    current = {s.name: s for s in read_services()}

    changed = 0
    total = 0
    for name, wanted in states.items():
        info = current.get(name)
        if info is None or info.start_type == wanted:
            continue
        total += 1
        if progress:
            progress(info.display)
        if wanted in _SC_MODES and set_start_type(name, wanted)[0]:
            changed += 1
    log(f"Откат служб по снимку: {changed} из {total}")
    return changed, total


def recommended_to_disable(services: list[ServiceInfo]) -> list[ServiceInfo]:
    """Только безопасные и сейчас работающие — то, что реально даст эффект."""
    return [s for s in services
            if s.kind == SAFE and s.can_change and s.start_type != "disabled"]
