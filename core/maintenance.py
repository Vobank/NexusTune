"""Обслуживание по расписанию через планировщик заданий Windows.

Смысл: уборка и очистка памяти полезны, но их никто не делает регулярно.
Здесь приложение заводит себе настоящую задачу в планировщике Windows, которая
запускает его в тихом режиме и выполняет выбранные работы без окон и вопросов.

Почему через планировщик, а не «висеть в памяти и следить за часами»: задача
планировщика выполнится, даже если приложение не запущено, переживёт
перезагрузку и не будет занимать память круглые сутки.

Задача создаётся только для текущего пользователя и запускается с его правами.
Работы, требующие администратора, в тихом режиме честно пропускаются — иначе
Windows показывала бы запрос UAC в пустоту.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .util import is_admin, log, run, run_ps

TASK_NAME = "KremenMaintenance"

# Что умеет делать тихий режим. Ключ -> (название, пояснение, нужен ли админ)
JOBS = {
    "junk": ("Убрать мусорные файлы",
             "Временные файлы, кэш шейдеров, отчёты о сбоях, кэш обновлений.",
             False),
    "traces": ("Стереть следы работы",
               "Недавние документы, кэш миниатюр, история адресной строки.",
               False),
    "memory": ("Освободить память",
               "Рабочие наборы простаивающих программ и список ожидания.",
               True),
    "recycle": ("Очистить корзину",
                "Файлы в корзине занимают место так же, как обычные.",
                False),
    "chronicle": ("Снять слепок системы",
                  "Чтобы Хроника видела, что менялось между запусками.",
                  False),
}

SCHEDULES = {
    "daily": ("Каждый день", "DAILY", "03:00"),
    "weekly": ("Раз в неделю", "WEEKLY", "03:00"),
    "logon": ("При входе в Windows", "ONLOGON", ""),
}


@dataclass
class TaskState:
    """Что сейчас заведено в планировщике."""
    exists: bool = False
    schedule: str = ""
    next_run: str = ""
    last_run: str = ""
    last_result: str = ""


def _command() -> str:
    """Команда, которую будет запускать планировщик."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --maintenance'
    script = Path(__file__).resolve().parent.parent / "main.py"
    runner = Path(sys.executable).with_name("pythonw.exe")
    if not runner.exists():
        runner = Path(sys.executable)
    return f'"{runner}" "{script}" --maintenance'


def read_state() -> TaskState:
    """Есть ли задача и когда она сработает."""
    state = TaskState()
    code, out, _err = run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
                          timeout=20)
    if code != 0 or not out:
        return state
    state.exists = True
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        # подписи зависят от языка Windows, поэтому ищем по корню слова
        if "next run" in key or "следующ" in key:
            state.next_run = value
        elif "last run" in key or "последн" in key and "результат" not in key:
            state.last_run = value
        elif "last result" in key or "результат" in key:
            state.last_result = value
        elif "schedule type" in key or "тип расписан" in key:
            state.schedule = value
    return state


def create_task(schedule: str, jobs: list[str], hour: str = "03:00") -> tuple[bool, str]:
    """Заводит задачу в планировщике Windows."""
    if schedule not in SCHEDULES:
        return False, "Неизвестное расписание."
    if not jobs:
        return False, "Не выбрано ни одной работы."

    _title, kind, default_time = SCHEDULES[schedule]
    command = _command() + " " + ",".join(jobs)

    args = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", command,
            "/sc", kind, "/f"]
    if kind != "ONLOGON":
        args += ["/st", hour or default_time]
    if kind == "WEEKLY":
        args += ["/d", "SUN"]

    code, out, err = run(args, timeout=30)
    if code == 0:
        log(f"Обслуживание по расписанию: {schedule}, работы: {', '.join(jobs)}")
        when = SCHEDULES[schedule][0].lower()
        return True, (f"Задача создана: {when}"
                      + (f" в {hour}" if kind != "ONLOGON" else "")
                      + ". Приложение будет запускаться тихо, без окон.")
    text = (err or out or "").strip()
    return False, text or "Планировщик отклонил создание задачи."


def remove_task() -> tuple[bool, str]:
    code, _out, err = run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], timeout=20)
    if code == 0:
        log("Обслуживание по расписанию отключено")
        return True, "Задача удалена — обслуживание по расписанию отключено."
    return False, err or "Не удалось удалить задачу."


def run_now() -> tuple[bool, str]:
    """Запускает заведённую задачу немедленно."""
    code, _out, err = run(["schtasks", "/run", "/tn", TASK_NAME], timeout=20)
    if code == 0:
        return True, "Задача запущена — обслуживание идёт в фоне."
    return False, err or "Не удалось запустить задачу."


# ---------------------------------------------------------------- тихий режим


def run_jobs(jobs: list[str]) -> list[str]:
    """Выполняет работы без интерфейса. Возвращает список строк отчёта."""
    report: list[str] = []
    admin = is_admin()

    for key in jobs:
        entry = JOBS.get(key)
        if entry is None:
            continue
        title, _why, needs_admin = entry
        if needs_admin and not admin:
            report.append(f"{title}: пропущено, нужны права администратора.")
            continue
        try:
            report.append(_run_one(key, title))
        except Exception as exc:
            report.append(f"{title}: не получилось — {exc}")

    for line in report:
        log(f"Обслуживание: {line}")
    return report


def _run_one(key: str, title: str) -> str:
    if key == "junk":
        from .cleanup import clean_categories, junk_categories
        keys = [c.key for c in junk_categories() if c.default]
        freed = clean_categories(keys)
        return f"{title}: освобождено {freed} МБ."

    if key == "traces":
        from .privacy import clean_traces, trace_items
        keys = [item.key for item in trace_items() if item.default]
        done, mb = clean_traces(keys)
        return f"{title}: очищено категорий {done}, освобождено {mb} МБ."

    if key == "memory":
        from .memory import clean_memory
        result = clean_memory()
        return f"{title}: вернулось {result.get('freed_mb', 0)} МБ."

    if key == "recycle":
        from .cleanup import empty_recycle_bin
        ok, message = empty_recycle_bin()
        return f"{title}: {message}"

    if key == "chronicle":
        from .chronicle import capture, save
        from .context import AppContext
        snapshot = capture(AppContext.create())
        save(snapshot)
        return f"{title}: записано {len(snapshot.programs)} программ."

    return f"{title}: нечего делать."


def parse_arguments(argv: list[str]) -> list[str] | None:
    """Разбирает аргументы командной строки тихого режима.

    Формат: main.py --maintenance junk,memory
    Возвращает None, если тихий режим не запрашивали.
    """
    if "--maintenance" not in argv:
        return None
    index = argv.index("--maintenance")
    if index + 1 < len(argv):
        jobs = [j.strip() for j in argv[index + 1].split(",") if j.strip()]
        if jobs:
            return jobs
    # без списка выполняем безопасный набор по умолчанию
    return ["junk", "recycle", "chronicle"]
