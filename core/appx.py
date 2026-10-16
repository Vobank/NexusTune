"""Удаление предустановленных приложений Windows.

Windows приносит с собой десятки приложений, которыми почти никто не пользуется.
Они занимают место, обновляются в фоне и висят в памяти.

Правило модуля: удаляется только то, что можно вернуть. Всё, что чинит систему
или трогает магазин приложений, помечено как защищённое и не удаляется вообще.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import is_admin, log, ps_json, run_ps


@dataclass
class AppPackage:
    """Одно приложение Microsoft Store."""
    full_name: str
    package_name: str                # короткое имя семейства
    display_name: str = ""
    publisher: str = ""
    size_mb: int = 0
    protected: bool = False
    known: str = ""                  # понятное описание, если приложение нам знакомо
    removable: bool = True
    user_installed: bool = False     # похоже, что это поставил сам пользователь


# Эти трогать нельзя: без них ломается интерфейс, магазин или безопасность
PROTECTED_PREFIXES = (
    "microsoft.windowsstore",
    "microsoft.desktopappinstaller",
    "microsoft.vclibs",
    "microsoft.net.native",
    "microsoft.ui.xaml",
    "microsoft.services.store",
    "microsoft.windows.photos",          # часто используется как просмотрщик по умолчанию
    "microsoft.windowsnotepad",
    "microsoft.windowscalculator",
    "microsoft.windowsterminal",
    "microsoft.sechealthui",
    "microsoft.windowssecurity",
    "microsoft.accountscontrol",
    "windows.immersivecontrolpanel",
    "microsoft.windows.shellexperiencehost",
    "microsoft.windows.startmenuexperiencehost",
    "microsoft.windows.search",
    "microsoft.aad.brokerplugin",
    "microsoft.windows.cloudexperiencehost",
    "nvidia",                             # панель управления видеокартой
    "realtek", "intel", "amd",
    # среды выполнения и служебные пакеты: без них ломаются другие приложения
    "microsoftcorporationii.winapp",
    "microsoft.winget",
    "microsoft.languageexperiencepack",
    "microsoft.storepurchaseapp",
    "microsoftwindows.crossdevice",
    "microsoftwindows.client",
    # инструменты, которые пользователь ставит осознанно
    "microsoft.powertoys",
    "microsoft.visualstudiocode",
)

# Приложения, которые обычно не нужны, с объяснением
KNOWN_BLOAT = {
    "microsoft.bingnews": "Новости Bing — плитка новостей, которую почти никто не открывает.",
    "microsoft.bingweather": "Погода Bing — работает в фоне ради виджета.",
    "microsoft.bingsearch": "Поиск Bing в меню Пуск.",
    "microsoft.gethelp": "«Техническая поддержка» — открывает справку в браузере.",
    "microsoft.getstarted": "«Советы» — вводный тур по Windows.",
    "microsoft.microsoftofficehub": "Реклама подписки Office, не сам Office.",
    "microsoft.microsoftsolitairecollection": "Коллекция пасьянсов с рекламой.",
    "microsoft.mixedreality.portal": "Портал смешанной реальности для VR-шлемов Windows.",
    "microsoft.office.onenote": "OneNote из магазина, отдельный от настольного Office.",
    "microsoft.people": "«Люди» — старая адресная книга.",
    "microsoft.skypeapp": "Skype из магазина.",
    "microsoft.wallet": "Кошелёк Microsoft, в большинстве стран не работает.",
    "microsoft.windowsalarms": "Будильники и часы.",
    "microsoft.windowsfeedbackhub": "Центр отзывов о Windows.",
    "microsoft.windowsmaps": "Карты Windows, качают офлайн-карты в фоне.",
    "microsoft.windowssoundrecorder": "Диктофон.",
    "microsoft.xboxapp": "Приложение Xbox. Нужно только для Game Pass.",
    "microsoft.xbox.tcui": "Служебный интерфейс Xbox для игр из магазина.",
    "microsoft.xboxgameoverlay": "Игровой оверлей Xbox — тот самый Win+G.",
    "microsoft.xboxgamingoverlay": "Игровая панель Xbox, съедает кадры в играх.",
    "microsoft.xboxidentityprovider": "Вход в Xbox. Нужен для игр из Game Pass.",
    "microsoft.xboxspeechtotextoverlay": "Субтитры голосового чата Xbox.",
    "microsoft.yourphone": "«Связь с телефоном» — постоянно висит в памяти.",
    "microsoft.zunemusic": "Groove Музыка, устаревший плеер.",
    "microsoft.zunevideo": "Кино и ТВ Microsoft.",
    "microsoft.3dbuilder": "Редактор трёхмерных моделей.",
    "microsoft.print3d": "Печать на 3D-принтере.",
    "microsoft.microsoftstickynotes": "Записки на рабочем столе.",
    "microsoft.todos": "Список дел Microsoft To Do.",
    "microsoft.powerautomatedesktop": "Автоматизация задач Power Automate.",
    "clipchamp.clipchamp": "Видеоредактор Clipchamp.",
    "microsoftteams": "Teams для дома — запускается сам и висит в фоне.",
    "microsoft.copilot": "Помощник Copilot.",
    "microsoft.windows.devhome": "Панель разработчика Dev Home.",
    "microsoft.outlookforwindows": "Новый Outlook из магазина.",
    "microsoft.windowscommunicationsapps": "Почта и Календарь Windows.",
    "spotifyab.spotifymusic": "Spotify из магазина.",
    "disney.37853fc22b2ce": "Disney+ — предустановленная реклама.",
    "facebook": "Предустановленное приложение Facebook.",
    "instagram": "Предустановленное приложение Instagram.",
    "tiktok": "Предустановленное приложение TikTok.",
    "amazon.com.amazon": "Предустановленное приложение Amazon.",
    "netflix": "Предустановленное приложение Netflix.",
}


def _is_protected(package_name: str) -> bool:
    low = package_name.lower()
    return any(low.startswith(prefix) or prefix in low for prefix in PROTECTED_PREFIXES)


def _known_for(package_name: str) -> str:
    low = package_name.lower()
    for key, text in KNOWN_BLOAT.items():
        if low.startswith(key) or key in low:
            return text
    return ""


def list_packages(all_users: bool = False) -> list[AppPackage]:
    """Список установленных приложений магазина."""
    scope = "-AllUsers " if (all_users and is_admin()) else ""
    rows = ps_json(
        f"Get-AppxPackage {scope}| Where-Object {{ -not $_.IsFramework }} | "
        "Select-Object Name,PackageFullName,PackageFamilyName,Publisher,"
        "NonRemovable,InstallLocation", timeout=90)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    packages: list[AppPackage] = []
    for row in rows:
        name = str(row.get("Name") or "")
        if not name:
            continue
        non_removable = row.get("NonRemovable")
        package = AppPackage(
            full_name=str(row.get("PackageFullName") or ""),
            package_name=name,
            display_name=name.split(".")[-1] if "." in name else name,
            publisher=str(row.get("Publisher") or ""),
            removable=not bool(non_removable),
        )
        package.known = _known_for(name)
        package.protected = _is_protected(name) or not package.removable
        package.user_installed = not package.protected and not package.known
        packages.append(package)

    # знакомый мусор наверх, защищённое вниз
    packages.sort(key=lambda p: (p.protected, not bool(p.known), p.package_name.lower()))
    log(f"Найдено приложений магазина: {len(packages)}")
    return packages


def bloat_only(packages: list[AppPackage]) -> list[AppPackage]:
    """Только те, что мы уверенно считаем лишними."""
    return [p for p in packages if p.known and not p.protected]


def user_apps(packages: list[AppPackage]) -> list[AppPackage]:
    """Приложения, которые пользователь ставил сам. Удалять их по своей инициативе нельзя."""
    return [p for p in packages if p.user_installed]


def remove_package(package: AppPackage) -> tuple[bool, str]:
    """Удаляет приложение для текущего пользователя."""
    if package.protected:
        return False, f"{package.display_name}: это приложение трогать нельзя."
    if not package.full_name:
        return False, f"{package.display_name}: не удалось определить полное имя пакета."

    code, _out, err = run_ps(
        f"Remove-AppxPackage -Package '{package.full_name}' "
        "-ErrorAction SilentlyContinue; if ($?) {'OK'} else {'FAIL'}", timeout=90)
    if code == 0:
        log(f"Удалено приложение: {package.package_name}")
        return True, f"{package.display_name} удалено."
    return False, err or f"{package.display_name}: удалить не удалось."


def remove_many(packages: list[AppPackage], progress=None) -> tuple[int, list[str]]:
    """Удаляет несколько приложений подряд."""
    removed = 0
    messages: list[str] = []
    for index, package in enumerate(packages, 1):
        if progress:
            progress(index, len(packages), package.display_name)
        ok, message = remove_package(package)
        messages.append(message)
        if ok:
            removed += 1
    return removed, messages


def restore_hint() -> str:
    """Как вернуть удалённое."""
    return ("Всё удалённое возвращается из Microsoft Store: найди приложение по "
            "названию и установи заново. Системные компоненты не трогаются вообще — "
            "они помечены как защищённые и недоступны для удаления.")
