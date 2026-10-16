"""Свой профиль под каждую игру.

Один профиль на все игры — грубо. В соревновательном шутере нужен максимум
кадров и минимум задержки; в одиночной игре с красивой картинкой важнее ровный
кадр и тишина; во время стрима нельзя отдавать игре все ядра. Здесь для каждой
игры запоминается своя связка:

  * профиль системы, который включится при её запуске;
  * профиль настроек графики, который можно записать прямо в игру;
  * приоритет процесса и привязка к ядрам.

Правило то же, что и везде: если для игры ничего не задано, автопилот работает
по общему профилю из настроек — никакой магии за спиной.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from .util import APP_DIR, log

RULES_FILE = APP_DIR / "game_profiles.json"

# Что можно назначить игре
SYSTEM_PROFILES = ["gaming", "esports", "streaming", "silent", "eco", "work"]
GRAPHICS_PRESETS = ["esports", "balanced", "quality", "ultra"]

PRIORITIES = {
    "": "не менять",
    "above": "выше обычного",
    "high": "высокий",
}

AFFINITY = {
    "": "все ядра",
    "performance": "только производительные",
    "physical": "производительные без гиперпоточности",
}


@dataclass
class GameRule:
    """Правило для одной игры."""
    game: str                    # человеческое название
    process: str = ""            # имя exe, если известно
    system_profile: str = ""     # профиль системы: gaming / esports / ...
    graphics_preset: str = ""    # профиль графики для записи в конфиг игры
    priority: str = ""           # приоритет процесса
    affinity: str = ""           # привязка к ядрам
    auto_graphics: bool = False  # записывать настройки в игру автоматически
    note: str = ""

    def describe(self) -> str:
        """Короткое человеческое описание правила."""
        parts = []
        if self.system_profile:
            titles = {"gaming": "профиль «Игры»", "esports": "профиль «Киберспорт»",
                      "streaming": "профиль «Стриминг»", "silent": "профиль «Тихий»",
                      "eco": "профиль «Экономия»", "work": "профиль «Работа»"}
            parts.append(titles.get(self.system_profile, self.system_profile))
        if self.graphics_preset:
            titles = {"esports": "графика «Киберспорт»", "balanced": "графика «Баланс»",
                      "quality": "графика «Качество»", "ultra": "графика «Ультра»"}
            text = titles.get(self.graphics_preset, self.graphics_preset)
            if self.auto_graphics:
                text += " (записывать в игру)"
            parts.append(text)
        if self.priority:
            parts.append(f"приоритет {PRIORITIES.get(self.priority, self.priority)}")
        if self.affinity:
            parts.append(AFFINITY.get(self.affinity, self.affinity))
        return ", ".join(parts) if parts else "ничего не задано"

    @property
    def is_empty(self) -> bool:
        return not any((self.system_profile, self.graphics_preset,
                        self.priority, self.affinity))


def _load_raw() -> dict:
    if not RULES_FILE.exists():
        return {}
    try:
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_rules() -> dict[str, GameRule]:
    """Все правила: ключ — название игры в нижнем регистре."""
    rules: dict[str, GameRule] = {}
    for key, data in _load_raw().items():
        try:
            rules[key] = GameRule(**data)
        except TypeError:
            continue
    return rules


def save_rules(rules: dict[str, GameRule]) -> None:
    try:
        RULES_FILE.write_text(
            json.dumps({k: asdict(v) for k, v in rules.items()},
                       indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить правила игр: {exc}")


def get_rule(game: str) -> GameRule | None:
    """Правило для игры по её названию."""
    if not game:
        return None
    return load_rules().get(game.strip().lower())


def set_rule(rule: GameRule) -> None:
    rules = load_rules()
    key = rule.game.strip().lower()
    if rule.is_empty:
        rules.pop(key, None)
        log(f"Правило для игры «{rule.game}» удалено")
    else:
        rules[key] = rule
        log(f"Правило для игры «{rule.game}»: {rule.describe()}")
    save_rules(rules)


def remove_rule(game: str) -> None:
    rules = load_rules()
    if rules.pop(game.strip().lower(), None) is not None:
        save_rules(rules)
        log(f"Правило для игры «{game}» удалено")


def match_rule(game: str, process: str = "") -> GameRule | None:
    """Ищет правило по названию игры или по имени процесса."""
    rules = load_rules()
    key = (game or "").strip().lower()
    if key in rules:
        return rules[key]
    if process:
        process = process.lower()
        for rule in rules.values():
            if rule.process and rule.process.lower() == process:
                return rule
    # мягкое совпадение: «GTA V» и «Grand Theft Auto V»
    for stored_key, rule in rules.items():
        if stored_key and key and (stored_key in key or key in stored_key):
            return rule
    return None


def apply_rule(ctx, rule: GameRule, process_name: str = "") -> list[str]:
    """Применяет правило игры. Возвращает список выполненных шагов."""
    steps: list[str] = []

    if rule.system_profile:
        from . import profiles
        profile = profiles.by_key(rule.system_profile)
        if profile:
            try:
                profiles.apply_profile(ctx, profile)
                steps.append(f"Включён профиль «{profile.title}».")
            except Exception as exc:
                log(f"Правило игры: профиль не применился — {exc}")

    if rule.auto_graphics and rule.graphics_preset:
        steps.extend(_apply_graphics(ctx, rule))

    if (rule.priority or rule.affinity) and process_name:
        steps.extend(_apply_process(ctx, rule, process_name))
    return steps


def _apply_graphics(ctx, rule: GameRule) -> list[str]:
    """Пишет настройки графики в конфиг игры, если умеем."""
    from . import gameconfig
    steps: list[str] = []
    try:
        configs = gameconfig.detect_configs()
    except Exception:
        return steps

    target = None
    name = rule.game.lower()
    for config in configs:
        title = config.title.lower()
        if name in title or title in name:
            target = config
            break
    if target is None:
        return steps

    # Игра уже запущена — писать нельзя, она перезапишет файл при выходе
    if gameconfig.is_running(target):
        steps.append(f"Настройки графики для «{rule.game}» не записаны: "
                     "игра уже запущена.")
        return steps

    try:
        changes = gameconfig.plan(target, rule.graphics_preset, ctx.machine)
        if changes:
            ok, message = gameconfig.apply(target, changes)
            steps.append(message if ok else f"Графика не записана: {message}")
    except Exception as exc:
        log(f"Правило игры: настройки графики не записались — {exc}")
    return steps


def _apply_process(ctx, rule: GameRule, process_name: str) -> list[str]:
    """Ставит приоритет и привязку к ядрам процессу игры."""
    import psutil

    from .cpuinfo import read_topology
    steps: list[str] = []
    targets = [p for p in psutil.process_iter(["name", "pid"])
               if (p.info.get("name") or "").lower() == process_name.lower()]
    if not targets:
        return steps

    topology = read_topology()
    mask = 0
    if rule.affinity == "performance":
        mask = topology.performance_mask
    elif rule.affinity == "physical":
        best = max((c.efficiency for c in topology.cores), default=0)
        for core in topology.cores:
            if core.efficiency == best and core.threads:
                mask |= 1 << core.threads[0]

    priority_map = {
        "above": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
        "high": psutil.HIGH_PRIORITY_CLASS,
    }

    for proc in targets:
        try:
            if rule.priority in priority_map:
                proc.nice(priority_map[rule.priority])
            if mask:
                proc.cpu_affinity([i for i in range(topology.logical_count)
                                   if mask & (1 << i)])
        except (psutil.Error, OSError) as exc:
            log(f"Правило игры: процесс не настроен — {exc}")
            continue

    if rule.priority:
        steps.append(f"Приоритет игры: {PRIORITIES.get(rule.priority, rule.priority)}.")
    if mask:
        steps.append(f"Ядра для игры: {AFFINITY.get(rule.affinity, rule.affinity)}.")
    return steps
