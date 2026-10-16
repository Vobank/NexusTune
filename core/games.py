"""Поиск установленных игр: Steam, Epic Games, EA App, GOG, Battle.net."""
from __future__ import annotations

import json
import os
import re
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from .util import log


@dataclass
class Game:
    name: str
    launcher: str = "Steam"          # Steam / Epic / EA / GOG / Battle.net / Ручное
    app_id: str = ""
    install_dir: str = ""
    size_gb: float = 0.0
    exe: str = ""

    @property
    def launch_url(self) -> str:
        """URL или путь для запуска игры."""
        if self.launcher == "Steam" and self.app_id:
            return f"steam://rungameid/{self.app_id}"
        if self.launcher == "Epic" and self.app_id:
            return f"com.epicgames.launcher://apps/{self.app_id}?action=launch&silent=true"
        return self.exe or self.install_dir

    @property
    def key(self) -> str:
        return f"{self.launcher}:{self.app_id or self.name}"


def _reg_value(hive, path: str, name: str):
    """Читает значение реестра, None если нет."""
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


# ---------------------------------------------------------------- Steam


def _parse_vdf_flat(text: str) -> list[tuple[str, str]]:
    """Вытаскивает все пары "ключ" "значение" из VDF-файла."""
    return re.findall(r'"([^"]+)"\s+"([^"]*)"', text)


def steam_path() -> str | None:
    for hive, path in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
        val = _reg_value(hive, path, "SteamPath") or _reg_value(hive, path, "InstallPath")
        if val and Path(val).exists():
            return str(Path(val))
    return None


def _steam_libraries(root: Path) -> list[Path]:
    """Все библиотеки Steam, включая на других дисках."""
    libs = [root / "steamapps"]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for key, value in _parse_vdf_flat(text):
                if key == "path":
                    p = Path(value.replace("\\\\", "\\")) / "steamapps"
                    if p.exists() and p not in libs:
                        libs.append(p)
        except OSError:
            pass
    return [p for p in libs if p.exists()]


# служебные записи Steam, которые не являются играми
_STEAM_SKIP = {"228980", "1070560", "1391110", "1826330", "1493710", "2180100"}
_SKIP_WORDS = ("redistributable", "steamworks", "proton", "steam linux runtime",
               "directx", "visual c++", "dotnet")


def scan_steam() -> list[Game]:
    root = steam_path()
    if not root:
        return []
    games: list[Game] = []
    for lib in _steam_libraries(Path(root)):
        for manifest in lib.glob("appmanifest_*.acf"):
            try:
                data = dict(_parse_vdf_flat(manifest.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
            app_id = data.get("appid", "")
            name = data.get("name", "").strip()
            if not name or app_id in _STEAM_SKIP:
                continue
            if any(w in name.lower() for w in _SKIP_WORDS):
                continue
            install = lib / "common" / data.get("installdir", "")
            try:
                size = round(int(data.get("SizeOnDisk", 0)) / (1024 ** 3), 1)
            except ValueError:
                size = 0.0
            games.append(Game(name=name, launcher="Steam", app_id=app_id,
                              install_dir=str(install), size_gb=size))
    return games


# ---------------------------------------------------------------- Epic Games


def scan_epic() -> list[Game]:
    manifest_dir = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) \
        / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if not manifest_dir.exists():
        return []
    games: list[Game] = []
    for item in manifest_dir.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        name = (data.get("DisplayName") or "").strip()
        if not name or any(w in name.lower() for w in _SKIP_WORDS):
            continue
        games.append(Game(
            name=name, launcher="Epic",
            app_id=data.get("CatalogNamespace", "") + "%3A" + data.get("CatalogItemId", "")
                   + "%3A" + data.get("AppName", "") if data.get("CatalogNamespace") else "",
            install_dir=data.get("InstallLocation", ""),
            size_gb=round((data.get("InstallSize") or 0) / (1024 ** 3), 1),
            exe=str(Path(data.get("InstallLocation", "")) / data.get("LaunchExecutable", ""))
                if data.get("LaunchExecutable") else "",
        ))
    return games


# ---------------------------------------------------------------- прочие лаунчеры


def _scan_uninstall_keys(patterns: list[tuple[str, str]]) -> list[Game]:
    """Ищет игры среди установленных программ по издателю/пути."""
    found: list[Game] = []
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, base in roots:
        try:
            with winreg.OpenKey(hive, base) as key:
                count = winreg.QueryInfoKey(key)[0]
        except OSError:
            continue
        for i in range(count):
            try:
                with winreg.OpenKey(hive, base) as key:
                    sub = winreg.EnumKey(key, i)
                with winreg.OpenKey(hive, f"{base}\\{sub}") as sk:
                    def val(n):
                        try:
                            return str(winreg.QueryValueEx(sk, n)[0])
                        except OSError:
                            return ""
                    name, loc, pub = val("DisplayName"), val("InstallLocation"), val("Publisher")
            except OSError:
                continue
            if not name or not loc:
                continue
            blob = f"{loc} {pub}".lower()
            for marker, launcher in patterns:
                if marker in blob:
                    found.append(Game(name=name.strip(), launcher=launcher, install_dir=loc))
                    break
    return found


def scan_other_launchers() -> list[Game]:
    return _scan_uninstall_keys([
        ("ea games", "EA"), ("origin games", "EA"), ("electronic arts", "EA"),
        ("gog galaxy", "GOG"), ("gog.com", "GOG"),
        ("battle.net", "Battle.net"), ("blizzard", "Battle.net"),
        ("ubisoft", "Ubisoft"),
    ])


def scan_all() -> list[Game]:
    """Полное сканирование всех лаунчеров, без дубликатов."""
    games: list[Game] = []
    for scanner in (scan_steam, scan_epic, scan_other_launchers):
        try:
            games.extend(scanner())
        except Exception as exc:                       # сканер не должен ронять приложение
            log(f"Сканер {scanner.__name__} упал: {exc}")
    seen: set[str] = set()
    unique: list[Game] = []
    for g in sorted(games, key=lambda x: (-x.size_gb, x.name.lower())):
        norm = re.sub(r"[^a-zа-я0-9]", "", g.name.lower())
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(g)
    log(f"Найдено игр: {len(unique)}")
    return unique
