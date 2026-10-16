"""Автопилот: сам замечает запуск игры, включает профиль и возвращает всё после выхода.

Дополнительно ведёт дневник игровых сессий — сколько играл, как грелось железо
и сколько при этом ел компьютер.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import psutil

from .games import Game
from .util import APP_DIR, log

SESSIONS_FILE = APP_DIR / "game_sessions.json"

# Исполняемые файлы популярных игр — на случай, если игра стоит вне известных лаунчеров
KNOWN_GAME_EXES = {
    "cs2.exe": "Counter-Strike 2",
    "csgo.exe": "CS:GO",
    "valorant-win64-shipping.exe": "Valorant",
    "dota2.exe": "Dota 2",
    "league of legends.exe": "League of Legends",
    "r5apex.exe": "Apex Legends",
    "fortniteclient-win64-shipping.exe": "Fortnite",
    "gta5.exe": "Grand Theft Auto V",
    "gta5_enhanced.exe": "Grand Theft Auto V Enhanced",
    "rdr2.exe": "Red Dead Redemption 2",
    "cyberpunk2077.exe": "Cyberpunk 2077",
    "witcher3.exe": "The Witcher 3",
    "eldenring.exe": "Elden Ring",
    "bg3.exe": "Baldur's Gate 3",
    "bg3_dx11.exe": "Baldur's Gate 3",
    "starfield.exe": "Starfield",
    "hogwartslegacy.exe": "Hogwarts Legacy",
    "alanwake2.exe": "Alan Wake 2",
    "b1-win64-shipping.exe": "Black Myth: Wukong",
    "minecraft.windows.exe": "Minecraft",
    "javaw.exe": "Minecraft (Java)",
    "robloxplayerbeta.exe": "Roblox",
    "rustclient.exe": "Rust",
    "escapefromtarkov.exe": "Escape from Tarkov",
    "overwatch.exe": "Overwatch 2",
    "rainbowsix.exe": "Rainbow Six Siege",
    "modernwarfare.exe": "Call of Duty",
    "cod.exe": "Call of Duty",
    "destiny2.exe": "Destiny 2",
    "palworld-win64-shipping.exe": "Palworld",
    "helldivers2.exe": "Helldivers 2",
    "marvel-win64-shipping.exe": "Marvel Rivals",
    "discovery.exe": "The Finals",
    "wuthering waves.exe": "Wuthering Waves",
    "genshinimpact.exe": "Genshin Impact",
    "warthunder.exe": "War Thunder",
    "worldoftanks.exe": "World of Tanks",
    "factorio.exe": "Factorio",
    "satisfactory.exe": "Satisfactory",
    "cities2.exe": "Cities: Skylines II",
}

# Это не игры, хотя и живут в папках игр
NOT_GAMES = {
    "steam.exe", "steamwebhelper.exe", "gameoverlayui.exe", "steamservice.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe", "eadesktop.exe", "eabackgroundservice.exe",
    "battle.net.exe", "agent.exe", "galaxyclient.exe", "upc.exe", "ubisoftconnect.exe",
    "easyanticheat.exe", "battleye.exe", "vgtray.exe", "crashhandler.exe",
    "unins000.exe", "launcher.exe", "setup.exe", "vc_redist.x64.exe",
}


@dataclass
class GameSession:
    """Одна игровая сессия."""
    game: str = ""
    process: str = ""
    started: float = 0.0
    ended: float = 0.0
    minutes: float = 0.0
    avg_gpu_temp: float = 0.0
    max_gpu_temp: float = 0.0
    avg_power_w: float = 0.0
    max_power_w: float = 0.0
    energy_wh: float = 0.0

    @property
    def when(self) -> str:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(self.started))


class GameWatcher:
    """Следит за запуском игр и сам переключает профили."""

    def __init__(self, ctx, on_game_start=None, on_game_stop=None):
        self.ctx = ctx
        self.on_game_start = on_game_start
        self.on_game_stop = on_game_stop

        self.enabled = False
        self.game_profile = "gaming"
        self.restore_profile = "stock"
        self.auto_apply = True                    # переключать профиль автоматически

        self.current: GameSession | None = None
        self._game_paths: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples: list[tuple[float, float]] = []   # (температура, ватты)

    # ------------------------------------------------------------ настройка

    def set_library(self, games: list[Game]) -> None:
        """Запоминает папки установленных игр, чтобы узнавать их процессы."""
        self._game_paths = [
            str(Path(g.install_dir).resolve()).lower()
            for g in games if g.install_dir and Path(g.install_dir).exists()
        ]
        log(f"Автопилот знает {len(self._game_paths)} папок с играми")

    # ------------------------------------------------------------ поиск игры

    # Игры всегда занимают заметно больше памяти, чем служебные процессы.
    # Порог отсекает сотни системных процессов, которым незачем читать путь.
    _MIN_GAME_MEMORY_MB = 120

    def _identify(self, name: str, proc: psutil.Process) -> str | None:
        """Является ли процесс игрой, и как она называется."""
        if name in NOT_GAMES:
            return None
        if name in KNOWN_GAME_EXES:
            return KNOWN_GAME_EXES[name]
        if not self._game_paths:
            return None

        try:
            exe = (proc.exe() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        for folder in self._game_paths:
            if exe.startswith(folder):
                # имя папки игры понятнее, чем имя исполняемого файла
                return Path(folder).name
        return None

    def _find_running_game(self) -> tuple[str, str] | None:
        """Ищет запущенную игру: (название, имя процесса)."""
        for proc in psutil.process_iter(["name", "memory_info"]):
            try:
                name = (proc.info["name"] or "").lower()
                if not name:
                    continue
                # известную игру узнаём по имени сразу, остальные — только если
                # процесс достаточно тяжёлый, чтобы вообще быть игрой
                if name not in KNOWN_GAME_EXES:
                    info = proc.info["memory_info"]
                    if not info or info.rss < self._MIN_GAME_MEMORY_MB * 1024 * 1024:
                        continue
                title = self._identify(name, proc)
                if title:
                    return title, proc.info["name"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def _process_alive(self, process_name: str) -> bool:
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() == process_name.lower():
                return True
        return False

    # ------------------------------------------------------------ работа потока

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.enabled = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("Автопилот включён")

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.current:
            self._finish_session()
        log("Автопилот выключен")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log(f"Автопилот споткнулся: {exc}")
            self._stop.wait(4.0)

    def _tick(self) -> None:
        if self.current:
            # копим статистику по текущей сессии
            reading = self.ctx.last_reading or {}
            temp = reading.get("gpu_temp") or 0
            power = 0.0
            meter = getattr(self.ctx, "power_meter", None)
            if meter and meter.last:
                power = meter.last.wall_w
            if temp or power:
                self._samples.append((float(temp), float(power)))

            if not self._process_alive(self.current.process):
                self._finish_session()
            return

        found = self._find_running_game()
        if found:
            self._begin_session(*found)

    # ------------------------------------------------------------ сессии

    def _begin_session(self, title: str, process_name: str) -> None:
        self.current = GameSession(game=title, process=process_name, started=time.time())
        self._samples.clear()
        log(f"Автопилот заметил игру: {title} ({process_name})")

        if self.auto_apply:
            # У игры может быть своё правило — оно важнее общего профиля
            rule = self._rule_for(title, process_name)
            if rule is not None:
                self._apply_rule(rule, process_name)
            else:
                self._apply(self.game_profile)
        if self.on_game_start:
            try:
                self.on_game_start(title)
            except Exception:
                pass

    def _finish_session(self) -> None:
        session = self.current
        self.current = None
        if not session:
            return
        session.ended = time.time()
        session.minutes = round((session.ended - session.started) / 60.0, 1)

        temps = [t for t, _p in self._samples if t]
        powers = [p for _t, p in self._samples if p]
        if temps:
            session.avg_gpu_temp = round(sum(temps) / len(temps), 1)
            session.max_gpu_temp = max(temps)
        if powers:
            session.avg_power_w = round(sum(powers) / len(powers), 1)
            session.max_power_w = max(powers)
            session.energy_wh = round(session.avg_power_w * session.minutes / 60.0, 1)

        # совсем короткие запуски не засоряют дневник
        if session.minutes >= 1.0:
            save_session(session)
        log(f"Сессия завершена: {session.game}, {session.minutes} мин")

        if self.auto_apply:
            self._apply(self.restore_profile)
        if self.on_game_stop:
            try:
                self.on_game_stop(session)
            except Exception:
                pass

    def _rule_for(self, title: str, process_name: str):
        """Персональное правило игры, если оно задано."""
        try:
            from . import gameprofiles
            return gameprofiles.match_rule(title, process_name)
        except Exception as exc:
            log(f"Автопилот: правило игры не прочиталось — {exc}")
            return None

    def _apply_rule(self, rule, process_name: str) -> None:
        """Применяет персональное правило игры вместо общего профиля."""
        from . import gameprofiles
        try:
            steps = gameprofiles.apply_rule(self.ctx, rule, process_name)
            log(f"Автопилот применил правило игры «{rule.game}»: "
                + ("; ".join(steps) if steps else "нечего было делать"))
        except Exception as exc:
            log(f"Автопилот: правило игры не применилось — {exc}")

    def _apply(self, profile_key: str) -> None:
        from . import profiles
        profile = profiles.by_key(profile_key)
        if not profile:
            return
        try:
            profiles.apply_profile(self.ctx, profile)
        except Exception as exc:
            log(f"Автопилот не смог применить профиль {profile_key}: {exc}")


# ---------------------------------------------------------------- дневник сессий


def load_sessions() -> list[GameSession]:
    if not SESSIONS_FILE.exists():
        return []
    try:
        raw = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sessions = []
    for item in raw:
        try:
            sessions.append(GameSession(**item))
        except TypeError:
            continue
    return sorted(sessions, key=lambda s: s.started, reverse=True)


def save_session(session: GameSession) -> None:
    sessions = load_sessions()
    sessions.insert(0, session)
    sessions = sessions[:300]
    try:
        SESSIONS_FILE.write_text(
            json.dumps([asdict(s) for s in sessions], indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить сессию: {exc}")


def stats_by_game() -> list[dict]:
    """Сводка по играм: сколько времени, сколько энергии, как грелось."""
    totals: dict[str, dict] = {}
    for session in load_sessions():
        entry = totals.setdefault(session.game, {
            "game": session.game, "minutes": 0.0, "sessions": 0,
            "energy_wh": 0.0, "max_temp": 0.0,
        })
        entry["minutes"] += session.minutes
        entry["sessions"] += 1
        entry["energy_wh"] += session.energy_wh
        entry["max_temp"] = max(entry["max_temp"], session.max_gpu_temp)
    rows = sorted(totals.values(), key=lambda r: -r["minutes"])
    for row in rows:
        row["hours"] = round(row["minutes"] / 60.0, 1)
        row["kwh"] = round(row["energy_wh"] / 1000.0, 3)
    return rows
