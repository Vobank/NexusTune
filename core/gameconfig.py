"""Запись настроек прямо в конфиги игр.

Правила, которых модуль держится, чтобы не сломать чужие сохранения:

* файл конфига никогда не создаётся с нуля — только правится существующий;
* меняются только те параметры, которые в файле уже есть (для Unreal —
  ещё и штатные ключи раздела ScalabilityGroups, их движок читает всегда);
* перед первой записью делается резервная копия, откат доступен всегда;
* пока игра запущена, запись запрещена: игра перезапишет файл при выходе;
* после записи файл перечитывается и значения сверяются с задуманными.

Поддержаны движки, формат которых однозначен. Игры, где настройки лежат
в собственном закрытом формате, честно помечаются как недоступные — вслепую
туда писать нельзя.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .hwinfo import Machine
from .util import SNAPSHOT_DIR, log

BACKUP_DIR = SNAPSHOT_DIR / "gameconfig"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
ROAMING = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
DOCUMENTS = Path.home() / "Documents"


@dataclass
class ConfigChange:
    """Одно изменение в файле настроек игры."""
    title: str
    key: str
    old: str
    new: str
    why: str


@dataclass
class GameConfig:
    """Найденный файл настроек конкретной игры."""
    key: str
    title: str
    engine: str
    path: Path
    detail: str = ""
    processes: list = field(default_factory=list)

    @property
    def backup_dir(self) -> Path:
        return BACKUP_DIR / self.key

    def has_backup(self) -> bool:
        return self.backup_dir.is_dir() and any(self.backup_dir.iterdir())


# ---------------------------------------------------------------- работа с INI


def _ini_get(text: str, section: str, key: str) -> str | None:
    """Значение ключа внутри раздела INI. None, если раздела или ключа нет."""
    body = _ini_section_body(text, section)
    if body is None:
        return None
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", body,
                      re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def _ini_section_body(text: str, section: str) -> str | None:
    match = re.search(rf"^\[{re.escape(section)}\]\s*$", text,
                      re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    start = match.end()
    nxt = re.search(r"^\[.+?\]\s*$", text[start:], re.MULTILINE)
    return text[start: start + nxt.start()] if nxt else text[start:]


def _ini_set(text: str, section: str, key: str, value: str) -> str:
    """Меняет значение ключа; если ключа нет — добавляет его в конец раздела."""
    match = re.search(rf"^\[{re.escape(section)}\]\s*$", text,
                      re.IGNORECASE | re.MULTILINE)
    if not match:
        return text
    start = match.end()
    nxt = re.search(r"^\[.+?\]\s*$", text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end]

    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*?$",
                         re.IGNORECASE | re.MULTILINE)
    if pattern.search(body):
        body = pattern.sub(lambda m: m.group(1) + value, body, count=1)
    else:
        body = body.rstrip("\r\n") + f"\n{key}={value}\n"
    return text[:start] + body + text[end:]


# ---------------------------------------------------------------- работа с XML


def _xml_get(text: str, tag: str) -> str | None:
    """Значение атрибута value у одиночного тега. None, если тег не единственный."""
    found = re.findall(rf"<{re.escape(tag)}\s+value=\"([^\"]*)\"", text)
    return found[0] if len(found) == 1 else None


def _xml_set(text: str, tag: str, value: str) -> str:
    return re.sub(rf"(<{re.escape(tag)}\s+value=\")[^\"]*(\")",
                  lambda m: m.group(1) + value + m.group(2), text, count=1)


# ---------------------------------------------------------------- Unreal Engine

_UE_SCALABILITY = "ScalabilityGroups"
_UE_SETTINGS_DEFAULT = "/Script/Engine.GameUserSettings"


def _ue_settings_section(text: str) -> str:
    """Раздел с общими настройками. У каждой игры он свой.

    Fortnite, например, хранит их в /Script/FortniteGame.FortGameUserSettings,
    а базовый движок — в /Script/Engine.GameUserSettings.
    """
    match = re.search(r"^\[(/Script/[^\]]*GameUserSettings)\]\s*$", text,
                      re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else _UE_SETTINGS_DEFAULT

# Уровни Unreal: 0 низкий, 1 средний, 2 высокий, 3 эпический, 4 кинематографический
_UE_PRESETS = {
    #                        вид  тени   GI  отраж  пост  текст  эффек  листва  шейдер  сглаж
    "esports":  dict(view=2, shadow=0, gi=0, refl=0, post=0, tex=2, fx=1, foliage=0, shade=0, aa=1),
    "balanced": dict(view=3, shadow=1, gi=1, refl=1, post=2, tex=3, fx=2, foliage=2, shade=2, aa=2),
    "quality":  dict(view=3, shadow=2, gi=2, refl=2, post=3, tex=3, fx=3, foliage=3, shade=3, aa=3),
    "ultra":    dict(view=3, shadow=3, gi=3, refl=3, post=3, tex=3, fx=3, foliage=3, shade=3, aa=3),
}

_UE_KEYS = [
    ("sg.ViewDistanceQuality", "view", "Дальность прорисовки",
     "В соревновательных играх важнее всего: даёт увидеть противника раньше."),
    ("sg.ShadowQuality", "shadow", "Тени",
     "Самый дорогой параметр в большинстве игр. Снижение теней на ступень "
     "обычно даёт 10-20% кадров при почти незаметной разнице."),
    ("sg.GlobalIlluminationQuality", "gi", "Глобальное освещение",
     "Просчёт отражённого света. Красиво, но тяжело для видеокарты."),
    ("sg.ReflectionQuality", "refl", "Отражения",
     "Отражения в лужах и стекле. Второй по стоимости параметр после теней."),
    ("sg.PostProcessQuality", "post", "Постобработка",
     "Размытие в движении, свечение, глубина резкости. В динамике только мешает."),
    ("sg.TextureQuality", "tex", "Текстуры",
     "Упирается в видеопамять, а не в скорость чипа. Пока памяти хватает, "
     "высокие текстуры почти бесплатны."),
    ("sg.EffectsQuality", "fx", "Эффекты",
     "Дым, искры, частицы. Проседания случаются в перестрелках и взрывах."),
    ("sg.FoliageQuality", "foliage", "Растительность",
     "Трава и листва. На открытых картах съедает заметную часть кадров."),
    ("sg.ShadingQuality", "shade", "Затенение",
     "Точность расчёта освещения материалов."),
    ("sg.AntiAliasingQuality", "aa", "Сглаживание",
     "Убирает лесенки на гранях. Дешевле остальных параметров."),
    ("sg.LandscapeQuality", "view", "Качество ландшафта",
     "Детализация рельефа. Есть не во всех играх движка."),
]


def _ue_changes(text: str, preset: str, machine: Machine) -> list[ConfigChange]:
    values = dict(_UE_PRESETS[preset])

    # Текстуры ограничиваем объёмом видеопамяти: иначе игра начнёт подгружать
    # их с диска прямо в бою — это выглядит как рывки на ровном месте.
    vram = round((machine.gpu.vram_mb or 0) / 1024)
    if vram and vram < 6:
        values["tex"] = min(values["tex"], 1)
    elif vram and vram < 10:
        values["tex"] = min(values["tex"], 2)

    section = _ue_settings_section(text)
    changes: list[ConfigChange] = []
    for key, slot, title, why in _UE_KEYS:
        new = str(values[slot])
        old = _ini_get(text, _UE_SCALABILITY, key)
        if old is None:
            if key == "sg.LandscapeQuality":
                continue          # необязательный ключ, есть не у всех игр
            old = "по умолчанию"
        if old.strip() == new:
            continue
        if slot == "tex" and vram and vram < 10:
            why += f" У этой карты {vram} ГБ, поэтому потолок ограничен."
        changes.append(ConfigChange(title, f"{_UE_SCALABILITY}/{key}", old, new, why))

    # Масштаб разрешения ниже 100 незаметно мылит картинку — возвращаем его.
    # В соревновательном профиле масштаб не трогаем: игроки сознательно
    # опускают его ради кадров и знают, на что идут.
    res = _ini_get(text, _UE_SCALABILITY, "sg.ResolutionQuality")
    if (res is not None and preset != "esports"
            and res.strip() not in ("100", "100.000000")):
        changes.append(ConfigChange(
            "Масштаб разрешения", f"{_UE_SCALABILITY}/sg.ResolutionQuality",
            res, "100",
            "Игра рисует картинку в пониженном разрешении и растягивает её. "
            "Лучше вернуть 100 и снизить тени — картинка будет резче."))

    vsync = _ini_get(text, section, "bUseVSync")
    if (vsync is not None and preset in ("esports", "balanced")
            and vsync.strip().lower() == "true"):
        changes.append(ConfigChange(
            "Вертикальная синхронизация", f"{section}/bUseVSync", vsync, "False",
            "Синхронизация убирает разрывы, но добавляет к задержке ввода "
            "целый кадр. В динамичных играх её выключают."))

    limit = _ini_get(text, section, "FrameRateLimit")
    if limit is not None:
        target = "0.000000" if preset == "esports" else f"{float(machine.refresh):.6f}"
        if limit.strip() != target:
            why = ("Снимаем потолок кадров: в соревновательных играх лишние кадры "
                   "снижают задержку ввода." if preset == "esports" else
                   f"Ограничение по частоте монитора ({machine.refresh} Гц): "
                   "видеокарта перестаёт греться впустую, кадр становится ровнее.")
            changes.append(ConfigChange("Предел кадров",
                                        f"{section}/FrameRateLimit",
                                        limit, target, why))
    return changes


def _ue_apply(text: str, changes: list[ConfigChange]) -> str:
    for change in changes:
        section, key = change.key.split("/", 1)
        text = _ini_set(text, section, key, change.new)
    return text


# ---------------------------------------------------------------- GTA V

# (тег, человеческое имя, значения по профилям, пояснение)
_GTA_KEYS = [
    ("ShadowQuality", "Качество теней", {"esports": "0", "balanced": "2",
                                         "quality": "3", "ultra": "3"},
     "В GTA V тени — самый дорогой параметр. Между «высоко» и «очень высоко» "
     "разница видна только на скриншотах."),
    ("Shadow_SoftShadows", "Мягкость теней", {"esports": "0", "balanced": "3",
                                              "quality": "4", "ultra": "5"},
     "Сглаживание краёв теней. Значения выше третьего стоят кадров, но "
     "заметны только вблизи."),
    ("ReflectionQuality", "Отражения", {"esports": "0", "balanced": "2",
                                        "quality": "3", "ultra": "3"},
     "Отражения в стекле и на мокром асфальте."),
    ("ReflectionMSAA", "Сглаживание отражений", {"esports": "0", "balanced": "0",
                                                 "quality": "2", "ultra": "4"},
     "Отдельное сглаживание для отражений — дорогая мелочь."),
    ("MSAA", "Сглаживание", {"esports": "0", "balanced": "0",
                             "quality": "2", "ultra": "4"},
     "MSAA в GTA V бьёт по кадрам сильнее всего. Обычно его выключают и "
     "включают FXAA — разница в чёткости мизерная."),
    ("TextureQuality", "Текстуры", {"esports": "2", "balanced": "2",
                                    "quality": "2", "ultra": "2"},
     "Текстуры упираются в видеопамять, а не в скорость чипа."),
    ("ParticleQuality", "Частицы", {"esports": "0", "balanced": "1",
                                    "quality": "2", "ultra": "2"},
     "Взрывы и дым. Просадки случаются именно в перестрелках."),
    ("GrassQuality", "Трава", {"esports": "0", "balanced": "1",
                               "quality": "2", "ultra": "3"},
     "За городом трава съедает до 15% кадров."),
    ("ShaderQuality", "Шейдеры", {"esports": "1", "balanced": "2",
                                  "quality": "2", "ultra": "2"},
     "Качество материалов и освещения."),
    ("PostFX", "Постобработка", {"esports": "0", "balanced": "2",
                                 "quality": "3", "ultra": "3"},
     "Свет, блики, засветы. На «ультра» включается дорогое размытие."),
    ("SSAO", "Затенение", {"esports": "0", "balanced": "1",
                           "quality": "2", "ultra": "2"},
     "Мягкие тени в углах и под предметами."),
    ("AnisotropicFiltering", "Анизотропная фильтрация",
     {"esports": "16", "balanced": "16", "quality": "16", "ultra": "16"},
     "16x стоит меньше процента кадров, а дорога перестаёт мылиться вдали. "
     "Выключать её нет смысла ни в одном профиле."),
    ("WaterQuality", "Вода", {"esports": "0", "balanced": "1",
                              "quality": "2", "ultra": "2"},
     "Волны и прозрачность воды. За городом почти не встречается."),
    ("FXAA_Enabled", "Быстрое сглаживание",
     {"esports": "true", "balanced": "true", "quality": "true", "ultra": "true"},
     "FXAA почти ничего не стоит и убирает большую часть лесенок. "
     "Дешёвая замена дорогому MSAA."),
    ("UltraShadows_Enabled", "Тени высокой чёткости",
     {"esports": "false", "balanced": "false", "quality": "false", "ultra": "true"},
     "Отдельный дорогой режим теней. Заметен только на стоящих объектах."),
    ("Shadow_ParticleShadows", "Тени от частиц",
     {"esports": "false", "balanced": "true", "quality": "true", "ultra": "true"},
     "Тени от дыма и искр. В перестрелках отнимают кадры именно тогда, "
     "когда они нужнее всего."),
    ("LodScale", "Дальность детализации",
     {"esports": "0.000000", "balanced": "0.000000",
      "quality": "0.500000", "ultra": "1.000000"},
     "Насколько далеко объекты остаются подробными. Считает процессор, "
     "в городе бьёт по кадрам сильнее видеокарты."),
    ("CityDensity", "Плотность города",
     {"esports": "0.000000", "balanced": "0.500000",
      "quality": "0.800000", "ultra": "1.000000"},
     "Сколько машин и людей на улицах. Чистая нагрузка на процессор: "
     "в центре города именно она роняет кадры."),
    ("Tessellation", "Тесселяция", {"esports": "0", "balanced": "1",
                                    "quality": "2", "ultra": "3"},
     "Объёмная геометрия дорог и деревьев."),
    ("DoF", "Глубина резкости", {"esports": "false", "balanced": "false",
                                 "quality": "true", "ultra": "true"},
     "Размытие дальнего плана. В динамике только мешает целиться."),
    ("MotionBlurStrength", "Размытие в движении",
     {"esports": "0.000000", "balanced": "0.000000",
      "quality": "0.000000", "ultra": "0.500000"},
     "Смазывает картинку при повороте камеры и добавляет ощущение задержки."),
]


def _gta_changes(text: str, preset: str, machine: Machine) -> list[ConfigChange]:
    changes: list[ConfigChange] = []
    for tag, title, values, why in _GTA_KEYS:
        old = _xml_get(text, tag)
        if old is None:
            continue
        new = values[preset]
        vram = round((machine.gpu.vram_mb or 0) / 1024)
        if tag == "TextureQuality" and vram and vram < 6:
            new = "1"
            why += f" У карты {vram} ГБ памяти — потолок снижен."
        if old.strip() == new:
            continue
        changes.append(ConfigChange(title, tag, old, new, why))

    vsync = _xml_get(text, "VSync")
    if vsync is not None and preset in ("esports", "balanced") and \
            vsync.strip().lower() in ("true", "1"):
        changes.append(ConfigChange(
            "Вертикальная синхронизация", "VSync", vsync, "false",
            "Добавляет к задержке ввода целый кадр."))

    pause = _xml_get(text, "PauseOnFocusLoss")
    if pause is not None and pause.strip().lower() in ("true", "1"):
        changes.append(ConfigChange(
            "Пауза при сворачивании", "PauseOnFocusLoss", pause, "false",
            "Игра перестанет вставать на паузу, когда переключаешься на второй "
            "монитор. К кадрам отношения не имеет, просто удобнее."))
    return changes


def _gta_apply(text: str, changes: list[ConfigChange]) -> str:
    for change in changes:
        text = _xml_set(text, change.key, change.new)
    return text


# ---------------------------------------------------------------- Minecraft

_MC_KEYS = [
    ("renderDistance", "Дальность прорисовки",
     {"esports": "8", "balanced": "12", "quality": "16", "ultra": "24"},
     "Главный пожиратель кадров в Minecraft: каждый чанк считает процессор."),
    ("simulationDistance", "Дальность симуляции",
     {"esports": "6", "balanced": "8", "quality": "10", "ultra": "12"},
     "Радиус, в котором живут мобы и растут растения. Считает процессор."),
    ("graphicsMode", "Графика",
     {"esports": "0", "balanced": "1", "quality": "1", "ultra": "2"},
     "0 — быстрая, 1 — детальная, 2 — превосходная. Превосходная включает "
     "полупрозрачную воду и стоит заметных кадров."),
    ("particles", "Частицы",
     {"esports": "2", "balanced": "1", "quality": "0", "ultra": "0"},
     "0 — все, 1 — меньше, 2 — минимум."),
    ("entityShadows", "Тени существ",
     {"esports": "false", "balanced": "true", "quality": "true", "ultra": "true"},
     "Мелочь, но на серверах с толпой мобов заметна."),
    ("mipmapLevels", "Сглаживание текстур вдали",
     {"esports": "0", "balanced": "4", "quality": "4", "ultra": "4"},
     "Убирает рябь на дальних блоках почти бесплатно."),
    ("enableVsync", "Вертикальная синхронизация",
     {"esports": "false", "balanced": "false", "quality": "true", "ultra": "true"},
     "Убирает разрывы, но добавляет задержку."),
]


def _mc_changes(text: str, preset: str, _machine: Machine) -> list[ConfigChange]:
    current = {}
    for line in text.splitlines():
        if ":" in line:
            name, _sep, value = line.partition(":")
            current[name.strip()] = value.strip()

    changes: list[ConfigChange] = []
    for key, title, values, why in _MC_KEYS:
        if key not in current:
            continue
        new = values[preset]
        if current[key] == new:
            continue
        changes.append(ConfigChange(title, key, current[key], new, why))
    return changes


def _mc_apply(text: str, changes: list[ConfigChange]) -> str:
    wanted = {c.key: c.new for c in changes}
    lines = []
    for line in text.splitlines():
        name, sep, _value = line.partition(":")
        if sep and name.strip() in wanted:
            lines.append(f"{name.strip()}:{wanted[name.strip()]}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- движки

_ENGINES = {
    "unreal": (_ue_changes, _ue_apply),
    "gtav": (_gta_changes, _gta_apply),
    "minecraft": (_mc_changes, _mc_apply),
}

ENGINE_NAMES = {
    "unreal": "Unreal Engine",
    "gtav": "RAGE (GTA V)",
    "minecraft": "Minecraft",
}


# ---------------------------------------------------------------- поиск конфигов


# Папки движка Unreal, которые играми не являются: лаунчеры и редакторы
_UE_SKIP = {"epicgameslauncher", "unrealengine", "unrealengineinstaller",
            "epicgameslauncherinstaller", "launcher"}
# Конфиги редактора и выделенного сервера трогать незачем
_UE_SKIP_CONFIG_DIRS = {"windowseditor", "windowsserver", "linuxserver"}


def _unreal_title(path: Path) -> str:
    """Имя игры вытаскиваем из пути: %LOCALAPPDATA%\\<Игра>\\Saved\\Config\\..."""
    parts = path.parts
    try:
        index = parts.index("Saved")
        return parts[index - 1]
    except (ValueError, IndexError):
        return path.parent.name


def detect_configs() -> list[GameConfig]:
    """Ищет файлы настроек игр, в которые умеем писать."""
    found: list[GameConfig] = []

    # Unreal Engine: у всех игр движка один и тот же путь внутри папки игры
    try:
        for config_dir in LOCAL.glob("*/Saved/Config/*"):
            ini = config_dir / "GameUserSettings.ini"
            if not ini.is_file() or config_dir.name.lower() in _UE_SKIP_CONFIG_DIRS:
                continue
            folder = _unreal_title(ini)
            if folder.lower() in _UE_SKIP:
                continue
            # «FortniteGame» — это папка, а игра называется Fortnite
            title = folder[:-4] if folder.lower().endswith("game") and len(folder) > 5                 else folder
            found.append(GameConfig(
                key=f"unreal_{folder.lower()}", title=title, engine="unreal", path=ini,
                detail="Настройки движка Unreal: качество по десяти параметрам, "
                       "синхронизация и предел кадров.",
                processes=[f"{folder.lower()}.exe", f"{title.lower()}.exe",
                           f"{folder.lower()}-win64-shipping.exe",
                           f"{title.lower()}client-win64-shipping.exe"]))
    except OSError:
        pass

    gta = DOCUMENTS / "Rockstar Games" / "GTA V" / "settings.xml"
    if gta.is_file():
        found.append(GameConfig(
            key="gtav", title="Grand Theft Auto V", engine="gtav", path=gta,
            detail="Пятнадцать графических параметров игры плюс синхронизация.",
            processes=["gta5.exe", "gtav.exe", "playgtav.exe", "gta5_enhanced.exe"]))

    gta_enhanced = DOCUMENTS / "Rockstar Games" / "GTA V Enhanced" / "settings.xml"
    if gta_enhanced.is_file():
        found.append(GameConfig(
            key="gtav_enhanced", title="Grand Theft Auto V Enhanced", engine="gtav",
            path=gta_enhanced,
            detail="Расширенное издание: те же параметры, отдельный файл настроек.",
            processes=["gta5_enhanced.exe", "gta5.exe"]))

    mc = ROAMING / ".minecraft" / "options.txt"
    if mc.is_file():
        found.append(GameConfig(
            key="minecraft", title="Minecraft", engine="minecraft", path=mc,
            detail="Дальность прорисовки, графика, частицы и синхронизация.",
            processes=["javaw.exe", "minecraft.exe"]))

    found.sort(key=lambda c: c.title.lower())
    return found


def is_running(config: GameConfig) -> str | None:
    """Имя процесса игры, если она сейчас запущена."""
    wanted = {name.lower() for name in config.processes}
    if not wanted:
        return None
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name in wanted:
            return name
    return None


# ---------------------------------------------------------------- применение


def plan(config: GameConfig, preset: str, machine: Machine) -> list[ConfigChange]:
    """Что именно изменится в файле. Ничего не пишет."""
    builder, _writer = _ENGINES[config.engine]
    try:
        text = config.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"Не удалось прочитать конфиг {config.path}: {exc}")
        return []
    return builder(text, preset, machine)


def _backup(config: GameConfig) -> Path | None:
    folder = config.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = folder / f"{stamp}_{config.path.name}"
    try:
        shutil.copy2(config.path, target)
        return target
    except OSError as exc:
        log(f"Не удалось сохранить копию конфига: {exc}")
        return None


def apply(config: GameConfig, changes: list[ConfigChange]) -> tuple[bool, str]:
    """Записывает изменения в файл игры. Перед записью делает копию."""
    if not changes:
        return False, "Менять нечего — настройки уже такие."

    running = is_running(config)
    if running:
        return False, (f"Игра запущена ({running}). Закрой её: при выходе игра "
                       "перезапишет файл настроек своими значениями.")

    if not _backup(config):
        return False, "Не удалось сделать резервную копию — запись отменена."

    try:
        text = config.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"Файл не читается: {exc}"

    _builder, writer = _ENGINES[config.engine]
    updated = writer(text, changes)
    if updated == text:
        return False, "Файл не изменился — ни один параметр не подошёл под формат."

    # снимаем «только чтение»: некоторые игры ставят его после первой настройки
    try:
        if not os.access(config.path, os.W_OK):
            config.path.chmod(0o666)
    except OSError:
        pass

    try:
        config.path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return False, f"Записать не удалось: {exc}"

    applied, failed = verify(config, changes)
    log(f"Настройки игры {config.title}: применено {applied} из {len(changes)}")
    if failed:
        return True, (f"Применено {applied} из {len(changes)}. "
                      f"Не легли: {', '.join(failed)}. Копия файла сохранена, "
                      "откат доступен кнопкой.")
    return True, (f"Готово: {applied} параметров записано в настройки игры. "
                  "Копия прежнего файла сохранена — откат в один клик.")


def verify(config: GameConfig, changes: list[ConfigChange]) -> tuple[int, list[str]]:
    """Перечитывает файл и сверяет, что значения действительно легли."""
    try:
        text = config.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, [c.title for c in changes]

    applied, failed = 0, []
    for change in changes:
        if config.engine == "unreal":
            section, key = change.key.split("/", 1)
            current = _ini_get(text, section, key)
        elif config.engine == "gtav":
            current = _xml_get(text, change.key)
        else:
            match = re.search(rf"^{re.escape(change.key)}:(.*)$", text, re.MULTILINE)
            current = match.group(1).strip() if match else None
        if current is not None and current.strip() == change.new:
            applied += 1
        else:
            failed.append(change.title)
    return applied, failed


def restore(config: GameConfig) -> tuple[bool, str]:
    """Возвращает самый первый сохранённый файл настроек."""
    folder = config.backup_dir
    if not folder.is_dir():
        return False, "Копий нет — приложение ничего в этой игре не меняло."
    copies = sorted(folder.glob("*"))
    if not copies:
        return False, "Копий нет."

    running = is_running(config)
    if running:
        return False, f"Сначала закрой игру ({running})."

    try:
        shutil.copy2(copies[0], config.path)
    except OSError as exc:
        return False, f"Не удалось вернуть файл: {exc}"
    log(f"Настройки игры {config.title} возвращены из копии {copies[0].name}")
    stamp = copies[0].name[:15].replace("_", " ")
    return True, f"Файл настроек возвращён к состоянию от {stamp}."
