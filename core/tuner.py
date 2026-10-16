"""Движок подбора игровых настроек под конкретное железо.

Модель простая и прозрачная: у каждой игры есть «вес» нагрузки на GPU и CPU,
у железа — индекс мощности. Из них считается ожидаемый FPS для четырёх
профилей, а набор настроек собирается по правилам, а не наугад.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .hwinfo import Machine

# ---------------------------------------------------------------- профили


PRESETS = ["esports", "balanced", "quality", "ultra"]

PRESET_INFO = {
    "esports": {
        "title": "Киберспорт",
        "subtitle": "Максимум FPS и минимум задержки",
        "icon": "⚡",
        "factor": 2.60,
        "vram": 0.50,
    },
    "balanced": {
        "title": "Баланс",
        "subtitle": "Высокий FPS при достойной картинке",
        "icon": "⚖",
        "factor": 1.65,
        "vram": 0.68,
    },
    "quality": {
        "title": "Качество",
        "subtitle": "Красиво и всё ещё плавно",
        "icon": "✦",
        "factor": 1.25,
        "vram": 0.85,
    },
    "ultra": {
        "title": "Ультра",
        "subtitle": "Максимальная картинка",
        "icon": "◆",
        "factor": 1.00,
        "vram": 1.0,
    },
}


@dataclass
class GameProfile:
    """Характеристика требовательности игры."""
    pattern: str
    weight: float = 5.5           # нагрузка на видеокарту, 1..10
    cpu_weight: float = 2.5       # нагрузка на процессор, 1..6
    category: str = "Одиночная"
    vram_gb: float = 6.0          # видеопамять для ультра в 1080p
    dlss: bool = False
    fsr: bool = False
    rt: bool = False
    note: str = ""


# База популярных игр. Порядок важен: первое совпадение выигрывает.
GAME_DB: list[GameProfile] = [
    GameProfile(r"counter-?strike|cs2|cs:?go", 2.0, 1.2, "Соревновательный шутер", 4.0,
                note="Движок любит частоту ЦП. Держи низкие тени — они дают ложные силуэты."),
    GameProfile(r"valorant", 1.6, 1.3, "Соревновательный шутер", 3.0,
                note="Ограничь FPS чуть ниже герц монитора — ввод станет ровнее."),
    GameProfile(r"dota\s*2", 2.4, 2.0, "MOBA", 4.0,
                note="Vulkan обычно даёт более ровный кадр, чем DirectX."),
    GameProfile(r"league of legends", 1.4, 1.4, "MOBA", 2.5),
    GameProfile(r"overwatch", 2.6, 1.8, "Соревновательный шутер", 4.0, fsr=True),
    GameProfile(r"rainbow\s*six|siege", 2.8, 2.0, "Соревновательный шутер", 5.0,
                note="Vulkan + низкие тени — стандарт для соревновательной игры."),
    GameProfile(r"apex\s*legends", 3.4, 2.6, "Королевская битва", 6.0,
                note="Ограничение FPS в игре снижает микрофризы."),
    GameProfile(r"fortnite", 3.6, 2.8, "Королевская битва", 6.0, dlss=True, fsr=True, rt=True,
                note="Режим Performance даёт огромный FPS ценой картинки."),
    GameProfile(r"pubg|playerunknown", 3.8, 3.0, "Королевская битва", 6.0),
    GameProfile(r"warzone|call of duty|modern warfare|black ops", 6.5, 3.4, "Шутер", 9.0,
                dlss=True, fsr=True, rt=True,
                note="Игра съедает много видеопамяти — следи за потоковой передачей текстур."),
    GameProfile(r"battlefield", 5.5, 3.2, "Шутер", 8.0, dlss=True, fsr=True),
    GameProfile(r"escape from tarkov", 6.0, 4.2, "Выживание", 8.0,
                note="Очень зависит от ЦП и ОЗУ. 32 ГБ здесь реально помогают."),
    GameProfile(r"cyberpunk", 9.0, 3.5, "Открытый мир", 8.5, dlss=True, fsr=True, rt=True,
                note="Эталон тяжести. Без апскейлинга трассировка лучей почти нереальна."),
    GameProfile(r"red dead redemption|rdr\s*2", 8.0, 3.2, "Открытый мир", 8.0, dlss=True,
                note="Vulkan работает стабильнее DirectX 12 на большинстве систем."),
    GameProfile(r"gta\s*v|grand theft auto v", 4.0, 2.8, "Открытый мир", 5.0,
                note="Сглаживание MSAA здесь дорогое — лучше FXAA."),
    GameProfile(r"gta\s*(vi|6)", 9.5, 4.0, "Открытый мир", 12.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"witcher\s*3", 5.5, 2.6, "RPG", 7.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"elden ring", 5.8, 3.0, "RPG", 8.0,
                note="Игра жёстко ограничена 60 FPS — целься в стабильные 60."),
    GameProfile(r"baldur.?s gate\s*3", 6.0, 4.0, "RPG", 8.0, dlss=True, fsr=True,
                note="Третий акт упирается в процессор, а не в видеокарту."),
    GameProfile(r"starfield", 8.5, 4.0, "RPG", 8.5, dlss=True, fsr=True),
    GameProfile(r"hogwarts legacy", 8.0, 3.6, "Открытый мир", 9.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"alan wake\s*2", 9.5, 3.0, "Экшн", 9.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"black myth|wukong", 9.0, 3.2, "Экшн", 9.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"stalker\s*2", 9.0, 4.0, "Открытый мир", 9.5, dlss=True, fsr=True),
    GameProfile(r"the last of us", 7.5, 3.6, "Экшн", 9.0, dlss=True, fsr=True),
    GameProfile(r"god of war", 6.0, 2.8, "Экшн", 8.0, dlss=True, fsr=True),
    GameProfile(r"spider-?man", 6.5, 3.8, "Экшн", 9.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"horizon (zero|forbidden)", 7.0, 3.0, "Открытый мир", 9.0, dlss=True, fsr=True),
    GameProfile(r"assassin.?s creed", 7.5, 3.4, "Открытый мир", 9.0, dlss=True, fsr=True),
    GameProfile(r"far cry", 5.5, 3.0, "Шутер", 7.0, dlss=True),
    GameProfile(r"resident evil", 5.5, 2.6, "Хоррор", 8.0, dlss=True, fsr=True, rt=True,
                note="Ползунок текстур завышает требования к видеопамяти — не ставь максимум."),
    GameProfile(r"forza|need for speed", 5.0, 2.6, "Гонки", 8.0, dlss=True, fsr=True, rt=True),
    GameProfile(r"microsoft flight simulator", 8.5, 5.0, "Симулятор", 9.0, dlss=True, fsr=True,
                note="Упирается в процессор и интернет-канал сильнее, чем в видеокарту."),
    GameProfile(r"minecraft", 2.0, 2.0, "Песочница", 3.0,
                note="Дальность прорисовки и Java-память решают всё."),
    GameProfile(r"roblox", 1.5, 1.5, "Песочница", 2.0),
    GameProfile(r"rust", 6.5, 4.0, "Выживание", 9.0,
                note="Очень прожорлив к ОЗУ и видеопамяти."),
    GameProfile(r"ark:?", 7.0, 3.6, "Выживание", 9.0),
    GameProfile(r"valheim|palworld|enshrouded", 4.5, 3.0, "Выживание", 6.0, dlss=True, fsr=True),
    GameProfile(r"war\s*thunder|world of tanks|world of warships", 3.5, 2.6, "Симулятор", 5.0),
    GameProfile(r"destiny\s*2", 4.5, 3.0, "Шутер", 6.0, dlss=True, fsr=True),
    GameProfile(r"genshin|honkai|zenless", 3.0, 2.4, "RPG", 4.0),
    GameProfile(r"path of exile|diablo", 4.0, 3.2, "RPG", 6.0, dlss=True, fsr=True),
    GameProfile(r"the finals|delta force", 5.0, 3.4, "Шутер", 7.0, dlss=True, fsr=True),
    GameProfile(r"helldivers", 5.5, 3.4, "Шутер", 7.0, dlss=True, fsr=True),
    GameProfile(r"marvel rivals", 5.0, 3.0, "Шутер", 7.0, dlss=True, fsr=True),
    GameProfile(r"delta|squad|arma", 6.5, 4.5, "Симулятор", 8.0),
    GameProfile(r"cities.?skylines|anno|civilization|total war", 4.5, 5.0, "Стратегия", 6.0,
                note="Поздние стадии партии упираются в процессор — графика тут ни при чём."),
    GameProfile(r"factorio|satisfactory|dyson", 3.5, 4.5, "Стратегия", 5.0),
    GameProfile(r"terraria|stardew|hades|celeste|vampire survivors", 1.2, 1.2, "Инди", 2.0),
]

_INDIE_HINTS = ("simulator 20", "tycoon", "pixel", "roguelike", "card")


def profile_for(name: str) -> GameProfile:
    """Подбирает профиль требовательности по названию игры."""
    low = name.lower()
    for prof in GAME_DB:
        if re.search(prof.pattern, low):
            return prof
    if any(h in low for h in _INDIE_HINTS):
        return GameProfile(pattern="", weight=2.5, cpu_weight=2.0, category="Инди", vram_gb=3.0,
                           note="Игры нет в базе — оценка усреднённая для лёгких проектов.")
    return GameProfile(pattern="", weight=5.5, cpu_weight=2.8, category="Неизвестно", vram_gb=6.0,
                       dlss=True, fsr=True,
                       note="Игры нет в базе — оценка усреднённая. Проверь FPS в бою, не в меню.")


# ---------------------------------------------------------------- модель производительности

_CPU_INDEX = {1: 1.6, 2: 2.6, 3: 3.6, 4: 5.0, 5: 6.5}
# Прирост FPS от режимов апскейлинга
_UPSCALE_GAIN = {"Выкл": 1.0, "Качество": 1.35, "Баланс": 1.55, "Производительность": 1.85}


def _pixel_load(machine: Machine) -> float:
    """Во сколько раз разрешение тяжелее, чем 1080p."""
    return (machine.megapixels / 2.0736) ** 0.9


def estimate_fps(machine: Machine, prof: GameProfile, preset: str,
                 upscale: str = "Выкл", rt_on: bool = False) -> tuple[int, str]:
    """Оценка среднего FPS. Возвращает (FPS, что именно ограничивает)."""
    info = PRESET_INFO[preset]
    gpu_fps = (3.0 * machine.gpu.power * info["factor"] * _UPSCALE_GAIN.get(upscale, 1.0)
               / (prof.weight * _pixel_load(machine)))
    if rt_on:
        gpu_fps *= 0.55                                   # трассировка лучей режет FPS вдвое
    cpu_fps = _CPU_INDEX.get(machine.cpu_tier, 3.0) * 100.0 / max(prof.cpu_weight, 0.5)

    # нехватка видеопамяти бьёт по кадру сильнее любых настроек
    need_vram = prof.vram_gb * info["vram"] * (machine.megapixels / 2.0736) ** 0.35 * 1024
    have_vram = machine.gpu.vram_mb or 8192
    if need_vram > have_vram * 1.05:
        gpu_fps *= max(0.45, have_vram / need_vram)
        limiter = "видеопамять"
    elif gpu_fps <= cpu_fps:
        limiter = "видеокарта"
    else:
        limiter = "процессор"

    fps = min(gpu_fps, cpu_fps)
    if need_vram > have_vram * 1.05:
        fps = min(fps, cpu_fps)
    return max(int(round(fps)), 5), limiter


# ---------------------------------------------------------------- сборка настроек


@dataclass
class Recommendation:
    """Готовая рекомендация для одного профиля."""
    preset: str
    title: str
    subtitle: str
    icon: str
    fps: int
    limiter: str
    settings: list[tuple[str, str, str]] = field(default_factory=list)  # (параметр, значение, зачем)
    warnings: list[str] = field(default_factory=list)
    upscale: str = "Выкл"
    rt: bool = False


def _upscale_tech(machine: Machine, prof: GameProfile) -> str | None:
    """Какая технология апскейлинга доступна на этом железе."""
    name = machine.gpu.name.lower()
    if prof.dlss and machine.gpu.vendor == "nvidia" and re.search(r"rtx", name):
        return "DLSS"
    if prof.fsr or prof.dlss:
        return "FSR"
    return None


def build_recommendation(machine: Machine, game_name: str, preset: str) -> Recommendation:
    """Собирает полный набор настроек для игры и профиля."""
    prof = profile_for(game_name)
    info = PRESET_INFO[preset]
    tech = _upscale_tech(machine, prof)
    warnings: list[str] = []

    # --- решаем, нужен ли апскейлинг и в каком режиме
    upscale = "Выкл"
    if tech:
        raw_fps, _ = estimate_fps(machine, prof, preset)
        target = machine.refresh if preset in ("esports", "balanced") else 60
        if preset == "esports" and raw_fps < target:
            upscale = "Производительность"
        elif raw_fps < target * 0.75:
            upscale = "Производительность" if raw_fps < target * 0.5 else "Баланс"
        elif raw_fps < target * 1.1 or preset in ("quality", "ultra"):
            upscale = "Качество"

    # --- трассировка лучей только там, где на неё хватает мощности
    rt_on = False
    if prof.rt and preset == "ultra" and machine.gpu.tier >= 4:
        test_fps, _ = estimate_fps(machine, prof, preset, upscale, rt_on=True)
        rt_on = test_fps >= 45
        if not rt_on:
            warnings.append("Трассировка лучей отключена: с ней кадр падает ниже играбельного.")

    fps, limiter = estimate_fps(machine, prof, preset, upscale, rt_on)

    # --- разрешение
    res = machine.resolution
    res_why = "Родное разрешение монитора — картинка без замыливания."
    if preset == "esports" and fps < 100 and machine.megapixels > 2.5:
        res = "1920x1080"
        res_why = "Понижено: на этом железе родное разрешение не даёт нужный FPS в динамике."

    # --- ограничение кадров
    if preset == "esports":
        cap = max(machine.refresh, 60)
        cap_why = f"Потолок по монитору ({machine.refresh} Гц). Ровный кадр важнее рекордных цифр."
    else:
        cap = machine.refresh - 3
        cap_why = "На 3 кадра ниже герцовки — так G-Sync/FreeSync не срывается в разрывы."
    cap = min(cap, max(fps + 15, 30))

    def pick(esports, balanced, quality, ultra):
        return {"esports": esports, "balanced": balanced,
                "quality": quality, "ultra": ultra}[preset]

    settings: list[tuple[str, str, str]] = [
        ("Разрешение", res, res_why),
        ("Режим экрана", "Полноэкранный (эксклюзивный)",
         "Даёт самый низкий отклик и приоритет над рабочим столом."),
        ("Вертикальная синхронизация", pick("Выкл", "Выкл", "Выкл", "Вкл"),
         "Выключена ради отклика; включай только если ловишь разрывы без G-Sync."),
        ("Ограничение кадров", str(cap), cap_why),
        ("Текстуры", pick("Высокие", "Высокие", "Высокие", "Ультра"),
         "Текстуры почти не влияют на FPS, пока хватает видеопамяти — экономить тут бессмысленно."),
        ("Тени", pick("Низкие", "Средние", "Высокие", "Ультра"),
         "Самый дорогой параметр после трассировки. Первое, что стоит снижать."),
        ("Дальность прорисовки", pick("Высокая", "Высокая", "Высокая", "Ультра"),
         "В соревновательных играх её не режут: важно видеть противника раньше."),
        ("Эффекты и частицы", pick("Низкие", "Средние", "Высокие", "Ультра"),
         "Низкие эффекты убирают визуальный мусор в перестрелках."),
        ("Постобработка", pick("Низкая", "Средняя", "Высокая", "Ультра"),
         "Дешёвая красота: даёт много картинки за небольшой кадр."),
        ("Сглаживание", pick("FXAA / Выкл", "TAA", "TAA", "TAA / DLAA"),
         "MSAA дороже всех вариантов; TAA — лучший компромисс."),
        ("Отражения", pick("Низкие", "Средние", "Высокие", "Ультра"),
         "Экранные отражения дороже, чем кажется."),
        ("Объёмный свет и туман", pick("Низкий", "Средний", "Высокий", "Ультра"),
         "Тяжёлый эффект, который почти не замечаешь в бою."),
        ("Окклюзия окружения", pick("Выкл", "SSAO", "HBAO+", "HBAO+ / RTAO"),
         "Добавляет объём теням, стоит умеренно."),
        ("Размытие в движении", "Выкл",
         "Смазывает картинку и добавляет ощущение задержки. Выключать всегда."),
        ("Глубина резкости", pick("Выкл", "Выкл", "Вкл", "Вкл"),
         "Красиво в катсценах, мешает в геймплее."),
        ("Хроматическая аберрация / зерно", "Выкл",
         "Чистая потеря чёткости без выигрыша в производительности."),
    ]

    if tech:
        settings.insert(4, (f"Апскейлинг ({tech})", upscale,
                            "Рендер в меньшем разрешении с умным увеличением — "
                            "самый дешёвый способ получить кадры."
                            if upscale != "Выкл" else
                            "Мощности хватает без апскейлинга — рендерим нативно."))
        if tech == "DLSS" and preset in ("quality", "ultra"):
            settings.insert(5, ("Генерация кадров", "Только на RTX 40+",
                                "На RTX 30 функция недоступна; на RTX 40+ включай при FPS выше 50."))
    if prof.rt:
        settings.insert(6, ("Трассировка лучей", "Вкл" if rt_on else "Выкл",
                            "Включена: запас мощности позволяет." if rt_on else
                            "Отключена: слишком дорого для этой связки железа и разрешения."))

    # --- предупреждения
    info_vram = prof.vram_gb * info["vram"] * (machine.megapixels / 2.0736) ** 0.35
    have_gb = (machine.gpu.vram_mb or 8192) / 1024
    if info_vram > have_gb:
        warnings.append(
            f"Игре нужно около {info_vram:.1f} ГБ видеопамяти, а у карты {have_gb:.0f} ГБ. "
            "Снижай текстуры на ступень, иначе будут рывки при подгрузке.")
    if limiter == "процессор":
        warnings.append("Упор в процессор: снижение графики почти не поднимет FPS. "
                        "Помогут разгон ОЗУ и закрытие фоновых программ.")
    if fps < 45:
        warnings.append("Расчётный FPS низкий даже на этом профиле. "
                        "Попробуй разрешение ниже или более агрессивный апскейлинг.")
    if prof.note:
        warnings.append(prof.note)

    return Recommendation(
        preset=preset, title=info["title"], subtitle=info["subtitle"], icon=info["icon"],
        fps=fps, limiter=limiter, settings=settings, warnings=warnings,
        upscale=upscale, rt=rt_on,
    )


def build_all(machine: Machine, game_name: str) -> dict[str, Recommendation]:
    """Все четыре профиля для одной игры."""
    return {p: build_recommendation(machine, game_name, p) for p in PRESETS}


def system_score(machine: Machine) -> tuple[int, str]:
    """Общая оценка системы 0..100 и её словесный класс."""
    gpu = min(machine.gpu.power / 2.6, 100)
    cpu = min(_CPU_INDEX.get(machine.cpu_tier, 3.0) / 6.5 * 100, 100)
    ram = min(machine.ram_gb / 32 * 100, 100)
    if machine.ram_sticks == 1:
        ram *= 0.75                                       # одноканал режет реальную скорость
    disk = 100 if machine.system_disk_ssd else 45
    score = int(gpu * 0.45 + cpu * 0.3 + ram * 0.15 + disk * 0.1)
    if score >= 85:
        label = "Топовая сборка"
    elif score >= 68:
        label = "Сильная игровая"
    elif score >= 50:
        label = "Крепкий средний класс"
    elif score >= 32:
        label = "Начальный игровой"
    else:
        label = "Офисный уровень"
    return score, label
