"""Профили одной кнопки: нажал — и весь компьютер перестроился под задачу.

Профиль описывает целевое состояние системы: какие твики включить, какой план
питания поставить, какие службы и задачи усыпить, что закрыть и как подсветить.
Применение всегда начинается со снимка, поэтому любой профиль обратим.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import cleanup, memory, optimizer, overclock
from .context import AppContext
from .util import log


@dataclass
class Profile:
    """Целевое состояние системы под конкретную задачу."""
    key: str
    title: str
    subtitle: str
    icon: str
    color: str
    description: str

    tweaks: dict[str, bool] = field(default_factory=dict)
    power_plan: str = "ultimate"         # ultimate / balanced / saver
    cpu_mode: str = "performance"        # performance / balanced / quiet
    disable_tasks: bool = False          # усыпить мусорные задачи планировщика
    trim_startup: bool = False           # отключить тяжёлую автозагрузку
    close_apps: list[str] = field(default_factory=list)
    clean_memory: bool = True
    clean_junk: list[str] = field(default_factory=list)
    gpu_power_percent: int | None = None
    rgb_effect: str = ""
    rgb_color: tuple = (0, 200, 255)
    highlights: list[str] = field(default_factory=list)


# Идентификаторы твиков, которые встречаются в наборах ниже
_MAX_PERF = {
    "power_plan": True, "hags": True, "game_mode": True, "gpu_priority": True,
    "cpu_priority_games": True, "sys_responsiveness": True, "prio_separation": True,
    "net_throttle": True, "game_dvr": True, "xbox_gamebar": True, "bg_apps": True,
    "telemetry": True, "svc_diagtrack": True, "svc_sysmain": True,
    "visual_fx": True, "transparency": True, "power_throttling": True,
    "win_tips": True, "web_search": True, "widgets": True, "qos_reserve": True,
    "nagle": True, "edge_preload": True, "mmcss_audio": True,
}

_OFFICE = {
    "power_plan": True, "hags": True, "game_mode": False, "gpu_priority": False,
    "cpu_priority_games": False, "sys_responsiveness": False, "prio_separation": False,
    "net_throttle": False, "game_dvr": True, "xbox_gamebar": True, "bg_apps": False,
    "telemetry": True, "svc_diagtrack": True, "svc_sysmain": False,
    "svc_wsearch": False, "visual_fx": False, "transparency": True,
    "power_throttling": False, "win_tips": True, "web_search": True,
    "widgets": True, "qos_reserve": False, "nagle": False,
    "edge_preload": False, "mmcss_audio": False,
}

_STOCK = {key: False for key in set(_MAX_PERF) | set(_OFFICE) | {"svc_wsearch", "fso",
                                                                "mouse_accel", "fast_startup"}}
_STOCK["power_plan"] = False


PROFILES: list[Profile] = [
    Profile(
        key="gaming", title="Игры", subtitle="Максимум кадров",
        icon="🎮", color="#00d0ff",
        description=(
            "Всё, что можно, отдаётся игре. Windows перестаёт придерживать ресурсы "
            "под фон, отключается запись игр и лишняя красота интерфейса, "
            "усыпляются мусорные задачи планировщика."),
        tweaks=dict(_MAX_PERF), power_plan="ultimate", cpu_mode="performance",
        disable_tasks=True, trim_startup=False, clean_memory=True,
        clean_junk=["temp", "shader_cache", "dumps"],
        rgb_effect="static", rgb_color=(255, 40, 0),
        highlights=[
            "Игровой режим и приоритет игр в планировщике Windows",
            "Запись игр и панель Xbox выключены — это 3-8% кадров",
            "Мусорные задачи планировщика усыплены",
            "Кэш шейдеров очищен — уходят рывки после обновления драйвера",
        ]),

    Profile(
        key="esports", title="Киберспорт", subtitle="Минимальная задержка",
        icon="⚡", color="#ff5f5f",
        description=(
            "Профиль для соревновательных шутеров. Всё как в игровом, плюс "
            "отключается ускорение мыши, сетевой троттлинг и полноэкранная "
            "оптимизация. Фоновые программы закрываются жёстче."),
        tweaks={**_MAX_PERF, "mouse_accel": True, "fso": True, "svc_wsearch": True},
        power_plan="ultimate", cpu_mode="performance",
        disable_tasks=True, trim_startup=True, clean_memory=True,
        clean_junk=["temp", "shader_cache", "dumps"],
        close_apps=["chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
                    "browser.exe", "Spotify.exe", "Telegram.exe", "OneDrive.exe",
                    "TIDAL.exe", "WallpaperEngine.exe", "wallpaper64.exe"],
        rgb_effect="static", rgb_color=(255, 0, 0),
        highlights=[
            "Ускорение мыши выключено — прицел становится предсказуемым",
            "Сетевой троттлинг снят, пинг стабильнее",
            "Индексация поиска усыплена — диск не просыпается в бою",
            "Тяжёлый фон закрыт",
        ]),

    Profile(
        key="streaming", title="Стриминг", subtitle="Игра плюс запись",
        icon="🎥", color="#7b5cff",
        description=(
            "Компромисс: игре отдаётся приоритет, но процессор не душится под "
            "фоном — кодировщику OBS нужны ядра. Discord и голосовые программы "
            "не трогаем."),
        tweaks={**_MAX_PERF, "sys_responsiveness": False, "prio_separation": False,
                "bg_apps": False, "mmcss_audio": True},
        power_plan="ultimate", cpu_mode="performance",
        disable_tasks=True, trim_startup=False, clean_memory=True,
        clean_junk=["temp"],
        close_apps=["OneDrive.exe", "SearchApp.exe"],
        rgb_effect="breathing", rgb_color=(140, 0, 255),
        highlights=[
            "Резерв системы под фон оставлен — кодировщику нужны ядра",
            "Приоритет звука повышен, голос не заикается",
            "Discord и голосовые программы не закрываются",
        ]),

    Profile(
        key="work", title="Работа и учёба", subtitle="Отзывчивость и порядок",
        icon="💼", color="#3fd07f",
        description=(
            "Система быстрая, но ничего игрового не навязывается. Поиск и "
            "индексация работают, эффекты интерфейса на месте, реклама и "
            "телеметрия выключены."),
        tweaks=dict(_OFFICE), power_plan="ultimate", cpu_mode="balanced",
        disable_tasks=True, trim_startup=True, clean_memory=True,
        clean_junk=["temp", "dumps", "update_cache"],
        rgb_effect="static", rgb_color=(0, 200, 255),
        highlights=[
            "Поиск Windows и индексация продолжают работать",
            "Реклама, советы и виджеты выключены",
            "Тяжёлая автозагрузка отключена — вход в систему быстрее",
        ]),

    Profile(
        key="silent", title="Тихий", subtitle="Тишина и холод",
        icon="🔇", color="#8b98ad",
        description=(
            "Для ночной работы и жарких дней. Видеокарта ограничивается по "
            "мощности, процессор переходит в мягкий режим. Кадров чуть меньше, "
            "шума заметно меньше."),
        tweaks={**_OFFICE, "visual_fx": False},
        power_plan="balanced", cpu_mode="quiet",
        disable_tasks=True, trim_startup=False, clean_memory=True,
        gpu_power_percent=80,
        rgb_effect="breathing", rgb_color=(0, 80, 160),
        highlights=[
            "Предел мощности видеокарты снижен до 80% — минус 8-12 °C",
            "Процессор в мягком режиме, вентиляторы тише",
            "Потеря кадров обычно 3-7%, на слух разница огромная",
        ]),

    Profile(
        key="eco", title="Экономия", subtitle="Меньше ватт и счёта",
        icon="🔋", color="#ffb648",
        description=(
            "Максимальная экономия энергии. Полезно, если компьютер работает "
            "круглосуточно или электричество дорогое."),
        tweaks=dict(_OFFICE),
        power_plan="saver", cpu_mode="quiet",
        disable_tasks=True, trim_startup=True, clean_memory=True,
        gpu_power_percent=70,
        rgb_effect="off",
        highlights=[
            "План энергосбережения и мягкий режим процессора",
            "Видеокарта ограничена 70% мощности",
            "Подсветка выключена — это ещё несколько ватт",
        ]),

    Profile(
        key="stock", title="Как с завода", subtitle="Вернуть всё обратно",
        icon="↺", color="#e6edf3",
        description=(
            "Полный откат: все твики выключаются, службы и задачи возвращаются "
            "в работу, автозагрузка восстанавливается, план питания становится "
            "сбалансированным."),
        tweaks=dict(_STOCK), power_plan="balanced", cpu_mode="balanced",
        disable_tasks=False, trim_startup=False, clean_memory=False,
        gpu_power_percent=100,
        rgb_effect="rainbow",
        highlights=[
            "Все изменения приложения отменяются",
            "Службы и задачи планировщика включаются обратно",
            "Автозагрузка восстанавливается",
        ]),
]


def by_key(key: str) -> Profile | None:
    for profile in PROFILES:
        if profile.key == key:
            return profile
    return None


SNAPSHOT_PROFILE = "before_profile"


def apply_profile(ctx: AppContext, profile: Profile, progress=None) -> list[str]:
    """Применяет профиль целиком. Возвращает список выполненных шагов."""
    steps: list[str] = []

    def report(text: str):
        steps.append(text)
        log(f"[{profile.key}] {text}")
        if progress:
            progress(text)

    # 1. Снимок текущего состояния — без него ничего не делаем
    optimizer.save_snapshot(ctx.tweaks, SNAPSHOT_PROFILE)
    report("Снимок текущих настроек сохранён — откат доступен в любой момент.")

    # 2. Твики Windows
    changed = 0
    failed = 0
    for tweak_id, want in profile.tweaks.items():
        tweak = ctx.tweak(tweak_id)
        if not tweak:
            continue
        if tweak.state() == want:
            continue
        if tweak.apply(want):
            changed += 1
        else:
            failed += 1
    text = f"Настроек Windows изменено: {changed}."
    if failed:
        text += f" Не удалось применить: {failed} (нужны права администратора)."
    report(text)

    # 3. Электропитание
    plan_guid = {
        "ultimate": optimizer.ensure_ultimate_plan(),
        "balanced": optimizer.BALANCED_GUID,
        "saver": optimizer.SAVER_GUID,
    }.get(profile.power_plan, optimizer.BALANCED_GUID)
    if optimizer.set_power_plan(plan_guid):
        names = {"ultimate": "максимальная производительность",
                 "balanced": "сбалансированный", "saver": "энергосбережение"}
        report(f"План электропитания: {names.get(profile.power_plan, profile.power_plan)}.")

    ok, msg = overclock.apply_cpu_profile(profile.cpu_mode)
    report(msg if not ok else f"Режим процессора: {profile.cpu_mode}.")

    # 4. Задачи планировщика
    if profile.disable_tasks:
        done, total = cleanup.set_junk_tasks(enabled=False)
        report(f"Мусорных задач планировщика усыплено: {done} из {total}.")
    elif profile.key == "stock":
        done, total = cleanup.set_junk_tasks(enabled=True)
        report(f"Задачи планировщика возвращены в работу: {done} из {total}.")

    # 5. Автозагрузка
    if profile.trim_startup:
        done = cleanup.trim_startup()
        report(f"Отключено программ в автозагрузке: {done}.")
    elif profile.key == "stock":
        done = cleanup.restore_startup()
        report(f"Автозагрузка восстановлена: {done} программ.")

    # 6. Фоновые программы
    if profile.close_apps:
        from .boost import close_processes
        closed, freed = close_processes(profile.close_apps)
        if closed:
            report(f"Закрыто фоновых программ: {closed}, освобождено {freed} МБ.")

    # 7. Уборка мусора
    if profile.clean_junk:
        freed_mb = cleanup.clean_categories(profile.clean_junk)
        if freed_mb:
            report(f"Удалено мусора: {freed_mb} МБ.")

    # 8. Память
    if profile.clean_memory:
        result = memory.clean_memory()
        report(f"Память очищена: освобождено {result['freed_mb']} МБ.")

    # 9. Предел мощности видеокарты
    if profile.gpu_power_percent is not None:
        caps = overclock.read_gpu_caps()
        if caps.available and caps.power_default:
            watts = int(caps.power_default * profile.gpu_power_percent / 100)
            watts = max(int(caps.power_min), min(watts, int(caps.power_max)))
            ok, msg = overclock.apply_power_limit(watts)
            report(msg)

    # 10. Подсветка
    if profile.rgb_effect and ctx.rgb_client and ctx.rgb_client.connected:
        ctx.effects.effect = profile.rgb_effect
        ctx.effects.color = profile.rgb_color
        if profile.rgb_effect in ("breathing", "rainbow", "wave"):
            ctx.effects.start()
        else:
            ctx.effects.stop()
            ctx.effects.apply_once()
        report(f"Подсветка переключена на сцену «{profile.title}».")

    ctx.config["active_profile"] = profile.key
    ctx.save()
    report(f"Профиль «{profile.title}» применён.")
    return steps
