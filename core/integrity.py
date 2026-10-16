"""Проверка целостности Windows: SFC, DISM и состояние диска.

Когда система начинает вести себя странно — вылетают программы, не ставятся
обновления, пропадают элементы интерфейса — виноваты обычно повреждённые
системные файлы. У Windows есть три встроенных инструмента, которые это чинят,
но живут они в командной строке, идут минутами без объяснений и печатают
результат так, что понять его без поиска в интернете нельзя.

Здесь они собраны вместе, с живым прогрессом и человеческой расшифровкой.

Порядок, в котором это делают правильно:
  1. DISM /ScanHealth — проверить хранилище компонентов (из него SFC берёт
     эталонные копии файлов). Если хранилище битое, SFC чинить нечем.
  2. DISM /RestoreHealth — починить хранилище, если нужно.
  3. SFC /scannow — проверить и заменить сами системные файлы.

Приложение объясняет этот порядок и не даёт запустить SFC до проверки
хранилища вслепую.

Вывод DISM запрашивается с ключом /English: тогда его можно разобрать
одинаково на любой локализации Windows. У SFC такого ключа нет, поэтому
его результат берётся из журнала CBS, где записи всегда на английском.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import is_admin, log, run, run_streaming

CBS_LOG = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Logs" / "CBS" / "CBS.log"

# Что печатает DISM в английском режиме
_DISM_CLEAN = "no component store corruption detected"
_DISM_REPAIRABLE = "the component store is repairable"
_DISM_REPAIRED = "the restore operation completed successfully"

_PERCENT = re.compile(r"(\d{1,3})[.,]\d%")


@dataclass
class CheckResult:
    """Итог одной проверки."""
    tool: str
    ok: bool = False
    healthy: bool = True
    title: str = ""
    detail: str = ""
    advice: list = field(default_factory=list)
    raw_tail: str = ""

    @property
    def tone(self) -> str:
        if not self.ok:
            return "warn"
        return "ok" if self.healthy else "danger"


def _percent_from(line: str) -> int | None:
    match = _PERCENT.search(line)
    if match:
        try:
            return max(0, min(100, int(match.group(1))))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- DISM


def dism_scan(on_progress=None, repair: bool = False) -> CheckResult:
    """Проверяет (или чинит) хранилище компонентов Windows."""
    action = "/RestoreHealth" if repair else "/ScanHealth"
    tool = "DISM /RestoreHealth" if repair else "DISM /ScanHealth"
    result = CheckResult(tool=tool)

    if not is_admin():
        result.title = "Нужны права администратора"
        result.detail = ("Проверка хранилища компонентов работает только "
                         "с правами администратора.")
        return result

    def handle(line: str):
        if on_progress:
            percent = _percent_from(line)
            on_progress(line, percent)

    log(f"{tool}: запуск")
    code, output = run_streaming(
        ["dism", "/Online", "/Cleanup-Image", action, "/English"],
        on_line=handle, timeout=3600)
    lowered = output.lower()
    result.raw_tail = "\n".join(
        [ln for ln in output.splitlines() if ln and "%" not in ln][-6:])

    if code != 0 and _DISM_CLEAN not in lowered:
        result.title = "Проверка не завершилась"
        result.detail = (f"DISM вернул код {code}. Часто это значит, что нет "
                         "интернета для докачки эталонных файлов или мешает "
                         "антивирус.")
        result.advice.append("Попробуй ещё раз, отключив VPN и антивирус.")
        return result

    result.ok = True
    if _DISM_CLEAN in lowered:
        result.healthy = True
        result.title = "Хранилище компонентов в порядке"
        result.detail = ("Эталонные копии системных файлов целы — значит SFC "
                         "будет чем чинить, если он что-то найдёт.")
    elif _DISM_REPAIRED in lowered and repair:
        result.healthy = True
        result.title = "Хранилище восстановлено"
        result.detail = ("Повреждения найдены и исправлены. Теперь имеет смысл "
                         "запустить проверку системных файлов SFC.")
        result.advice.append("Следующий шаг — SFC /scannow.")
    elif _DISM_REPAIRABLE in lowered:
        result.healthy = False
        result.title = "Хранилище повреждено, но чинится"
        result.detail = ("Windows нашла повреждения в хранилище компонентов. "
                         "Это исправимо: нажми «Починить хранилище».")
        result.advice.append("Запусти восстановление — оно займёт 5-20 минут "
                             "и потребует интернета.")
    else:
        result.healthy = False
        result.title = "Результат непонятен"
        result.detail = "DISM отработал, но не сказал ничего определённого."
    log(f"{tool}: {result.title}")
    return result


# ---------------------------------------------------------------- SFC


def cbs_size() -> int:
    """Размер журнала CBS прямо сейчас — метка начала проверки."""
    try:
        return CBS_LOG.stat().st_size if CBS_LOG.exists() else 0
    except OSError:
        return 0


def _read_cbs_repairs(since_bytes: int = 0,
                      limit_bytes: int = 6 * 1024 * 1024) -> tuple[int, int, list[str]]:
    """Что SFC починил и что не смог — по журналу CBS.

    since_bytes — размер журнала на момент запуска проверки. Читать нужно
    только то, что дописано после: иначе результаты прошлой проверки
    месячной давности выдадутся за сегодняшние. Это была бы прямая ложь.

    Журнал бывает на сотни мегабайт, поэтому в запасном случае (когда метки
    нет) читается только хвост.
    """
    if not CBS_LOG.exists():
        return 0, 0, []
    try:
        size = CBS_LOG.stat().st_size
        with CBS_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            if since_bytes and since_bytes < size:
                fh.seek(since_bytes)
                fh.readline()               # выбрасываем обрезанную строку
            elif size > limit_bytes:
                fh.seek(size - limit_bytes)
                fh.readline()
            lines = [ln for ln in fh if "[SR]" in ln]
    except OSError as exc:
        log(f"Журнал CBS не читается: {exc}")
        return 0, 0, []

    repaired = 0
    failed = 0
    names: list[str] = []
    for line in lines[-4000:]:
        low = line.lower()
        if "cannot repair member file" in low:
            failed += 1
            match = re.search(r"member file \[l:\d+\]'([^']+)'", line)
            if match and match.group(1) not in names:
                names.append(match.group(1))
        elif "repairing corrupted file" in low or "repaired file" in low:
            repaired += 1
    return repaired, failed, names[:8]


def sfc_scan(on_progress=None) -> CheckResult:
    """Проверяет и восстанавливает системные файлы Windows."""
    result = CheckResult(tool="SFC /scannow")
    if not is_admin():
        result.title = "Нужны права администратора"
        result.detail = "SFC работает только с правами администратора."
        return result

    def handle(line: str):
        if on_progress:
            on_progress(line, _percent_from(line))

    # запоминаем, где журнал заканчивался до запуска: разбирать будем
    # только то, что допишет именно эта проверка
    log_mark = cbs_size()
    log("SFC /scannow: запуск")
    # SFC печатает в кодировке консоли и без ключа английского языка
    code, output = run_streaming(["sfc", "/scannow"], on_line=handle,
                                 timeout=3600, encoding="cp866")
    result.raw_tail = "\n".join(
        [ln for ln in output.splitlines() if ln and "%" not in ln][-6:])

    repaired, failed, names = _read_cbs_repairs(since_bytes=log_mark)
    result.ok = code == 0 or repaired or failed

    if failed:
        result.healthy = False
        result.title = f"Найдены файлы, которые не удалось починить: {failed}"
        result.detail = ("SFC нашёл повреждённые системные файлы и не смог "
                         "заменить их — обычно потому, что повреждено само "
                         "хранилище эталонных копий.")
        if names:
            result.detail += " Например: " + ", ".join(names[:4]) + "."
        result.advice.append("Запусти «Починить хранилище» (DISM RestoreHealth), "
                             "а потом SFC ещё раз.")
    elif repaired:
        result.healthy = True
        result.title = f"Повреждения найдены и исправлены: {repaired}"
        result.detail = ("SFC заменил повреждённые системные файлы эталонными "
                         "копиями. Перезагрузи компьютер, чтобы изменения "
                         "вступили в силу везде.")
        result.advice.append("Перезагрузись и прогони проверку ещё раз — "
                             "она должна пройти чисто.")
    elif code == 0:
        result.healthy = True
        result.title = "Системные файлы целы"
        result.detail = ("Нарушений целостности не найдено. Если проблемы "
                         "остаются, дело не в системных файлах — смотри "
                         "драйверы и Хронику изменений.")
    else:
        result.healthy = False
        result.title = "Проверка завершилась с ошибкой"
        result.detail = (f"SFC вернул код {code}. Возможно, мешает антивирус "
                         "или служба обновлений занята.")
    log(f"SFC: {result.title}")
    return result


# ---------------------------------------------------------------- диск


def chkdsk_scan(drive: str = "C", on_progress=None) -> CheckResult:
    """Проверяет файловую систему без исправлений — только чтение."""
    result = CheckResult(tool=f"CHKDSK {drive}: (только чтение)")

    def handle(line: str):
        if on_progress:
            on_progress(line, _percent_from(line))

    log(f"CHKDSK {drive}: запуск в режиме только чтения")
    # без /F и /R — том не блокируется и перезагрузка не нужна
    code, output = run_streaming([f"chkdsk", f"{drive}:"], on_line=handle,
                                 timeout=3600, encoding="cp866")
    result.raw_tail = "\n".join(
        [ln for ln in output.splitlines() if ln and "%" not in ln][-8:])
    lowered = output.lower()

    result.ok = code in (0, 1, 2, 3)
    # Код 0 — ошибок нет; остальные означают найденные проблемы
    if code == 0:
        result.healthy = True
        result.title = "Файловая система в порядке"
        result.detail = "Ошибок структуры не найдено."
    elif not result.ok:
        result.title = "Проверка не запустилась"
        result.detail = ("Windows не дала проверить том. Обычно это значит, "
                         "что нужны права администратора.")
    else:
        result.healthy = False
        result.title = "Найдены ошибки файловой системы"
        result.detail = ("Проверка нашла проблемы в структуре тома. Исправить "
                         "их можно только при перезагрузке: Windows должна "
                         "получить том в монопольное владение.")
        result.advice.append(
            f"Выполни в командной строке администратора: chkdsk {drive}: /F "
            "и согласись на проверку при следующей загрузке.")
    log(f"CHKDSK {drive}: {result.title}")
    return result


# ---------------------------------------------------------------- порядок работ

STEPS = [
    ("dism_scan", "Проверить хранилище компонентов",
     "Быстрая проверка эталонных копий системных файлов. С неё нужно начинать: "
     "если хранилище повреждено, SFC нечем будет чинить. Обычно 1-3 минуты."),
    ("sfc", "Проверить системные файлы",
     "SFC сверяет каждый системный файл с эталоном и заменяет повреждённые. "
     "Занимает от 5 до 15 минут."),
    ("dism_repair", "Починить хранилище",
     "Скачивает и восстанавливает повреждённые эталонные копии. Нужен интернет. "
     "От 5 до 20 минут. Запускать, только если проверка нашла повреждения."),
    ("chkdsk", "Проверить файловую систему",
     "Читает структуру тома и ищет ошибки. Ничего не исправляет и не требует "
     "перезагрузки — только сообщает, есть ли проблема."),
]
