"""Политики Windows — то, что в домашней редакции недоступно вовсе.

В редакции Pro эти переключатели живут в редакторе групповых политик
(gpedit.msc). В домашней редакции редактора нет совсем, хотя сама Windows
политики читает — просто менять их приходится через реестр вручную.

Здесь собрано то, что реально важно человеку, который играет и не хочет,
чтобы система жила своей жизнью:

  * Windows Update не должен подменять драйвер видеокарты своим;
  * компьютер не должен перезагружаться сам посреди игры;
  * система не должна доустанавливать рекламные приложения;
  * обновления функций можно отложить, пока в них не починят ошибки.

Каждая политика — обычное значение реестра, и каждая снимается обратно.
Ничего необратимого здесь нет.
"""
from __future__ import annotations

import winreg

from .optimizer import HKCU, HKLM, Tweak, _reg_tweak, reg_delete, reg_read, reg_write
from .util import log, run

# ---------------------------------------------------------------- ветки политик

_POL = r"SOFTWARE\Policies\Microsoft\Windows"
_WU = _POL + r"\WindowsUpdate"
_WU_AU = _WU + r"\AU"
_CLOUD = _POL + r"\CloudContent"
_DATA = _POL + r"\DataCollection"
_SYSTEM = _POL + r"\System"
_STORE = r"SOFTWARE\Policies\Microsoft\WindowsStore"
_DRIVER_SEARCH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching"
_MAINTENANCE = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
                r"\Schedule\Maintenance")
_ADS = r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo"
_CONTENT = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"

# Сколько дней откладывать обновления функций. Полгода — обычный срок,
# за который Microsoft успевает починить крупные поломки новой версии.
DEFER_DAYS = 180


def _defer_state() -> bool:
    return (reg_read(HKLM, _WU, "DeferFeatureUpdates") == 1
            and (reg_read(HKLM, _WU, "DeferFeatureUpdatesPeriodInDays") or 0) >= 30)


def _defer_on() -> bool:
    return (reg_write(HKLM, _WU, "DeferFeatureUpdates", 1)
            and reg_write(HKLM, _WU, "DeferFeatureUpdatesPeriodInDays", DEFER_DAYS)
            and reg_write(HKLM, _WU, "BranchReadinessLevel", 16))


def _defer_off() -> bool:
    return (reg_delete(HKLM, _WU, "DeferFeatureUpdates")
            and reg_delete(HKLM, _WU, "DeferFeatureUpdatesPeriodInDays")
            and reg_delete(HKLM, _WU, "BranchReadinessLevel"))


def _content_off(names: list[str]) -> bool:
    """Выключает подборки рекламного содержимого в меню «Пуск» и на экране блокировки."""
    ok = True
    for name in names:
        ok = reg_write(HKCU, _CONTENT, name, 0) and ok
    return ok


def _content_on(names: list[str]) -> bool:
    ok = True
    for name in names:
        ok = reg_write(HKCU, _CONTENT, name, 1) and ok
    return ok


_CONTENT_KEYS = [
    "SubscribedContent-338388Enabled",   # предложения в «Пуске»
    "SubscribedContent-338389Enabled",   # советы и подсказки
    "SubscribedContent-310093Enabled",   # приветственные экраны
    "SubscribedContent-338387Enabled",   # интересное на экране блокировки
    "SilentInstalledAppsEnabled",        # тихая установка рекламных приложений
    "SoftLandingEnabled",                # всплывающие советы
    "RotatingLockScreenOverlayEnabled",  # текст поверх экрана блокировки
]


def build_policy_tweaks() -> list[Tweak]:
    """Каталог политик.

    Все они помечаются как экспертные: риск низкий, но это именно те
    переключатели, которых в домашней редакции Windows нет, и человек должен
    понимать, что меняет системную политику, а не галочку в настройках.
    """
    t: list[Tweak] = []

    # ================================================ Обновления Windows
    t.append(_reg_tweak(
        "pol_no_driver_updates", "Не обновлять драйверы через Windows Update",
        "ExcludeWUDriversInQualityUpdate = 1 — обновления Windows перестают "
        "подменять драйверы устройств.",
        "Самая полезная политика для игрового ПК. Windows Update регулярно "
        "ставит поверх свежего драйвера NVIDIA свою старую версию — и в играх "
        "внезапно падают кадры или ломается разгон. Драйверы ты будешь ставить "
        "сам, с сайта производителя.",
        "Обновления Windows", HKLM, _WU, "ExcludeWUDriversInQualityUpdate", 1, None,
        risk="низкий"))

    t.append(_reg_tweak(
        "pol_no_driver_search", "Не искать драйверы в интернете автоматически",
        "SearchOrderConfig = 0 — Windows не лезет в сеть за драйвером при "
        "подключении устройства.",
        "Дополняет предыдущую политику: новое устройство получит драйвер из "
        "системы, а не случайную версию из интернета.",
        "Обновления Windows", HKLM, _DRIVER_SEARCH, "SearchOrderConfig", 0, 1,
        risk="низкий"))

    t.append(_reg_tweak(
        "pol_no_autoreboot", "Не перезагружаться самому, если я за компьютером",
        "NoAutoRebootWithLoggedOnUsers = 1.",
        "Windows не выкинет тебя из игры ради установки обновления. "
        "Перезагрузку она предложит, но решать будешь ты.",
        "Обновления Windows", HKLM, _WU_AU, "NoAutoRebootWithLoggedOnUsers", 1, None,
        risk="низкий"))

    t.append(Tweak(
        id="pol_defer_features",
        title=f"Отложить крупные обновления Windows на {DEFER_DAYS} дней",
        desc=f"DeferFeatureUpdates = 1, период {DEFER_DAYS} дней.",
        why="Крупные обновления Windows первые месяцы ломают драйверы и игры. "
            "Полгода — обычный срок, за который Microsoft успевает всё починить. "
            "Обновления безопасности при этом продолжают приходить как обычно.",
        category="Обновления Windows", risk="низкий",
        is_on=_defer_state, turn_on=_defer_on, turn_off=_defer_off))

    t.append(_reg_tweak(
        "pol_no_restart_notify", "Предупреждать о перезагрузке заранее",
        "RestartNotificationsAllowed2 = 1 — Windows покажет уведомление, "
        "а не перезагрузится молча.",
        "Мелочь, но она даёт время сохранить игру.",
        "Обновления Windows", HKLM, _WU_AU, "RestartNotificationsAllowed2", 1, None,
        risk="низкий"))

    # ================================================ Реклама и навязанные приложения
    t.append(_reg_tweak(
        "pol_no_consumer", "Не доустанавливать рекламные приложения",
        "DisableWindowsConsumerFeatures = 1.",
        "Windows перестанет сама подсовывать в «Пуск» игры и программы вроде "
        "Candy Crush и TikTok, которые ты не ставил.",
        "Реклама и навязанное", HKLM, _CLOUD, "DisableWindowsConsumerFeatures", 1, None,
        risk="низкий"))

    t.append(_reg_tweak(
        "pol_no_spotlight", "Отключить «интересное» на экране блокировки",
        "DisableWindowsSpotlightFeatures = 1.",
        "Экран блокировки перестанет показывать рекламные подборки и советы.",
        "Реклама и навязанное", HKLM, _CLOUD, "DisableWindowsSpotlightFeatures", 1, None,
        risk="низкий"))

    t.append(_reg_tweak(
        "pol_no_softlanding", "Отключить всплывающие советы Windows",
        "DisableSoftLanding = 1.",
        "Пропадут всплывающие подсказки «попробуйте новую функцию».",
        "Реклама и навязанное", HKLM, _CLOUD, "DisableSoftLanding", 1, None,
        risk="низкий"))

    t.append(Tweak(
        id="pol_no_content_ads", title="Убрать рекламные подборки из «Пуска»",
        desc="Отключает семь источников рекомендательного содержимого "
             "в меню «Пуск» и на экране блокировки.",
        why="Меню «Пуск» перестанет предлагать приложения из магазина, "
            "а экран блокировки — показывать текст поверх картинки.",
        category="Реклама и навязанное", risk="низкий", admin=False,
        is_on=lambda: all(reg_read(HKCU, _CONTENT, name) == 0
                          for name in _CONTENT_KEYS),
        turn_on=lambda: _content_off(_CONTENT_KEYS),
        turn_off=lambda: _content_on(_CONTENT_KEYS)))

    t.append(_reg_tweak(
        "pol_no_adid", "Отключить рекламный идентификатор",
        "AdvertisingInfo\\Enabled = 0.",
        "Приложения перестанут получать твой рекламный идентификатор и "
        "связывать по нему действия в разных программах.",
        "Реклама и навязанное", HKCU, _ADS, "Enabled", 0, 1,
        admin=False, risk="низкий"))

    t.append(_reg_tweak(
        "pol_no_store_auto", "Не обновлять приложения магазина автоматически",
        "AutoDownload = 2.",
        "Магазин перестанет качать обновления приложений в фоне — канал и диск "
        "свободны. Обновить вручную можно в любой момент.",
        "Реклама и навязанное", HKLM, _STORE, "AutoDownload", 2, None,
        risk="низкий", recommended=False))

    # ================================================ Слежение
    t.append(_reg_tweak(
        "pol_telemetry_min", "Свести телеметрию к минимуму политикой",
        "AllowTelemetry = 0 — уровень «Безопасность».",
        "Домашняя редакция Windows не даёт выбрать этот уровень в настройках, "
        "но политику читает. Это самый низкий уровень сбора данных, доступный "
        "без правки системных файлов.",
        "Слежение", HKLM, _DATA, "AllowTelemetry", 0, 1, risk="низкий"))

    t.append(Tweak(
        id="pol_no_activity", title="Отключить журнал действий и его отправку",
        desc="EnableActivityFeed, PublishUserActivities и UploadUserActivities = 0.",
        why="Windows перестанет собирать «временную шкалу» твоих действий "
            "и отправлять её в облако Microsoft.",
        category="Слежение", risk="низкий",
        is_on=lambda: all(reg_read(HKLM, _SYSTEM, name) == 0 for name in
                          ("EnableActivityFeed", "PublishUserActivities",
                           "UploadUserActivities")),
        turn_on=lambda: all(reg_write(HKLM, _SYSTEM, name, 0) for name in
                            ("EnableActivityFeed", "PublishUserActivities",
                             "UploadUserActivities")),
        turn_off=lambda: all(reg_delete(HKLM, _SYSTEM, name) for name in
                             ("EnableActivityFeed", "PublishUserActivities",
                              "UploadUserActivities"))))

    t.append(_reg_tweak(
        "pol_no_handwriting", "Не отправлять отчёты рукописного ввода",
        "PreventHandwritingErrorReports = 1.",
        "Мелочь, но это ещё один канал отправки данных наружу.",
        "Слежение", HKLM, _POL + r"\HandwritingErrorReports",
        "PreventHandwritingErrorReports", 1, None, risk="низкий",
        recommended=False))

    # ================================================ Фоновое обслуживание
    t.append(_reg_tweak(
        "pol_no_maintenance", "Отключить автоматическое обслуживание Windows",
        "MaintenanceDisabled = 1.",
        "Windows перестанет по ночам будить компьютер и заниматься "
        "дефрагментацией, проверками и обновлениями. Обратная сторона: эти "
        "работы придётся иногда запускать самому — для этого есть страница "
        "«Настройки» с обслуживанием по расписанию.",
        "Фоновое обслуживание", HKLM, _MAINTENANCE, "MaintenanceDisabled", 1, 0,
        risk="средний", recommended=False))

    t.append(_reg_tweak(
        "pol_no_delivery", "Не раздавать обновления другим компьютерам",
        "DODownloadMode = 0 — только с серверов Microsoft.",
        "Компьютер перестанет отдавать куски обновлений соседям по сети "
        "и в интернет. Освобождается исходящий канал — заметно в онлайн-играх.",
        "Фоновое обслуживание", HKLM, _POL + r"\DeliveryOptimization",
        "DODownloadMode", 0, None, risk="низкий"))

    for tweak in t:
        tweak.expert = True
    return t


# ---------------------------------------------------------------- зарезервированное место


def reserved_storage_state() -> bool | None:
    """Включено ли резервирование места под обновления. None — не удалось узнать."""
    from .util import run_ps
    code, out, _err = run_ps(
        "(Get-WindowsReservedStorageState).ReservedStorageState", timeout=40)
    if code != 0 or not out:
        return None
    text = out.strip().lower()
    if "enabled" in text or text == "1":
        return True
    if "disabled" in text or text == "0":
        return False
    return None


def set_reserved_storage(enabled: bool) -> tuple[bool, str]:
    """Включает или отключает резервирование места под обновления.

    Windows держит про запас около семи гигабайт «на всякий случай» для
    установки обновлений. На небольшом системном диске это заметная потеря.
    Отключение освобождает их сразу; включить обратно можно в любой момент.
    """
    state = "Enabled" if enabled else "Disabled"
    code, out, err = run(
        ["dism", "/Online", "/Set-ReservedStorageState",
         f"/State:{state}", "/English"], timeout=180)
    text = (out + " " + err).lower()
    if code == 0 or "completed successfully" in text:
        log(f"Резервирование места под обновления: {state}")
        return True, ("Резервирование включено — Windows снова отложит около "
                      "7 ГБ под обновления."
                      if enabled else
                      "Резервирование отключено — освободилось около 7 ГБ. "
                      "Обновления по-прежнему будут ставиться, просто место "
                      "под них Windows возьмёт из свободного.")
    if "3017" in text or "pending" in text:
        return False, ("Windows сейчас занята установкой обновления. "
                       "Попробуй после перезагрузки.")
    return False, (err or out or "").strip() or "DISM отклонил изменение."
