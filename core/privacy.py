"""Следы работы за компьютером и по-настоящему безвозвратное удаление.

Две разные вещи, которые Windows не делает сама:

1. Следы. Куда ни нажми, система запоминает: последние открытые файлы,
   миниатюры удалённых фотографий, набранные в проводнике пути, историю
   действий за месяц. Это не мусор в смысле места — это история. Здесь её
   можно стереть по категориям, у каждой написано, что именно она хранит.

2. Затирание. Когда файл удаляют, Windows лишь помечает место как свободное —
   сами данные лежат на диске, пока их не перезапишут, и восстанавливаются
   любой бесплатной программой. `cipher /w` заполняет свободное место мусором,
   после чего восстановить уже нечего. На SSD это работает иначе (см. ниже),
   и приложение честно об этом предупреждает.
"""
from __future__ import annotations

import os
import shutil
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from .util import human_bytes, log, run

LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
ROAMING = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))

HKCU = winreg.HKEY_CURRENT_USER


@dataclass
class TraceItem:
    """Одна категория следов."""
    key: str
    title: str
    why: str
    paths: list = field(default_factory=list)      # папки, чьё содержимое чистим
    reg_keys: list = field(default_factory=list)   # (корень, путь) — очистить значения
    default: bool = True
    size_bytes: int = 0

    @property
    def size_text(self) -> str:
        return human_bytes(self.size_bytes) if self.size_bytes else "—"


def _p(*parts) -> Path:
    return Path(*parts)


def trace_items() -> list[TraceItem]:
    """Список следов, которые умеем находить и чистить."""
    return [
        TraceItem(
            "recent", "Недавние документы",
            "Список файлов, которые ты открывал: он виден в проводнике, в меню "
            "«Пуск» и в списках переходов на панели задач. Сами файлы остаются.",
            paths=[ROAMING / "Microsoft" / "Windows" / "Recent",
                   ROAMING / "Microsoft" / "Windows" / "Recent" / "AutomaticDestinations",
                   ROAMING / "Microsoft" / "Windows" / "Recent" / "CustomDestinations"]),
        TraceItem(
            "thumbs", "Кэш миниатюр",
            "Уменьшенные копии картинок и видео — в том числе уже удалённых. "
            "По ним видно, какие фотографии лежали в папке. Пересоздаётся сам.",
            paths=[LOCAL / "Microsoft" / "Windows" / "Explorer"]),
        TraceItem(
            "explorer_mru", "История адресной строки и поиска",
            "Пути, которые ты вручную вводил в проводнике, и запросы из поиска "
            "по файлам. Хранятся в реестре.",
            reg_keys=[
                (HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\TypedPaths"),
                (HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\WordWheelQuery"),
            ]),
        TraceItem(
            "run_mru", "История окна «Выполнить»",
            "Всё, что запускалось через Win+R. Мелочь, но хранится вечно.",
            reg_keys=[(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU")]),
        TraceItem(
            "activity", "Журнал действий Windows",
            "«Временная шкала»: чем ты занимался за последние 30 дней — какие "
            "программы и документы открывал, в какое время.",
            paths=[LOCAL / "ConnectedDevicesPlatform"]),
        TraceItem(
            "clipboard", "История буфера обмена",
            "Всё, что ты копировал, если включена история буфера (Win+V). Там "
            "оседают и пароли, скопированные из менеджера.",
            reg_keys=[(HKCU, r"Software\Microsoft\Clipboard")]),
        TraceItem(
            "diag", "Диагностические отчёты и очередь телеметрии",
            "Готовые к отправке в Microsoft отчёты о том, как ты пользуешься "
            "компьютером.",
            paths=[Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) /
                   "Microsoft" / "Diagnosis" / "ETLLogs"],
            default=False),
    ]


def _dir_size(path: Path, limit: int = 30000) -> int:
    total = count = 0
    if not path or not path.exists():
        return 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda e: None):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                count += 1
                if count >= limit:
                    return total
    except OSError:
        pass
    return total


def scan_traces() -> list[TraceItem]:
    """Считает объём каждой категории следов."""
    items = trace_items()
    for item in items:
        item.size_bytes = sum(_dir_size(p) for p in item.paths if p)
    return items


def _wipe_dir(path: Path) -> int:
    if not path or not path.exists():
        return 0
    freed = 0
    for entry in path.iterdir():
        # кэш миниатюр — единственный, что реально занят проводником;
        # занятые файлы просто пропускаем, пересоздадутся сами
        try:
            if entry.is_dir():
                size = _dir_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    freed += size
            elif entry.name.lower().startswith(("thumbcache", "iconcache")) or \
                    path.name != "Explorer":
                size = entry.stat().st_size
                entry.unlink()
                freed += size
        except OSError:
            continue
    return freed


def _clear_reg_values(root, subkey: str) -> None:
    """Удаляет все значения внутри ключа реестра, сам ключ оставляет."""
    try:
        handle = winreg.OpenKey(root, subkey, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    try:
        while True:
            name = winreg.EnumValue(handle, 0)[0]
            try:
                winreg.DeleteValue(handle, name)
            except OSError:
                break
    except OSError:
        pass
    finally:
        winreg.CloseKey(handle)


def clean_traces(keys: list[str]) -> tuple[int, int]:
    """Чистит выбранные следы. Возвращает (сколько категорий, освобождённые МБ)."""
    wanted = {i.key: i for i in trace_items()}
    freed = 0
    done = 0
    for key in keys:
        item = wanted.get(key)
        if not item:
            continue
        for path in item.paths:
            freed += _wipe_dir(path)
        for root, subkey in item.reg_keys:
            _clear_reg_values(root, subkey)
        done += 1
    mb = round(freed / (1024 ** 2))
    log(f"Следы: очищено категорий {done}, освобождено {mb} МБ")
    return done, mb


# ---------------------------------------------------------------- безвозвратное затирание


_MEDIA_CACHE: dict[str, bool | None] = {}

_MEDIA_SCRIPT = """
$out = @()
foreach ($p in (Get-Partition -ErrorAction SilentlyContinue |
                Where-Object { $_.DriveLetter })) {
    $disk = Get-PhysicalDisk -ErrorAction SilentlyContinue |
        Where-Object { $_.DeviceId -eq $p.DiskNumber }
    if ($disk) {
        $out += [pscustomobject]@{ Letter = [string]$p.DriveLetter
                                   Media  = [string]$disk.MediaType }
    }
}
$out
"""


def _read_media_types() -> dict[str, bool | None]:
    """Тип носителя по каждой букве диска: True — SSD, False — жёсткий диск.

    Раньше эта проверка спрашивала «есть ли в системе хоть один SSD» и
    отвечала «да» для любой буквы. На машине с SSD под систему и жёстким
    диском под игры это была прямая ложь: предупреждение про SSD показывалось
    для обычного диска и наоборот. Теперь буква сопоставляется с физическим
    носителем через разделы.
    """
    from .util import ps_json
    rows = ps_json(_MEDIA_SCRIPT, timeout=30)
    if isinstance(rows, dict):
        rows = [rows]
    result: dict[str, bool | None] = {}
    for row in rows or []:
        letter = str(row.get("Letter") or "").strip().upper()
        media = str(row.get("Media") or "").strip().lower()
        if not letter:
            continue
        if "ssd" in media:
            result[letter] = True
        elif "hdd" in media or "hard" in media:
            result[letter] = False
        else:
            result[letter] = None
    return result


def drive_is_ssd(letter: str) -> bool | None:
    """SSD ли конкретный диск. None — определить не удалось."""
    letter = (letter or "C").upper().lstrip()[:1]
    if not _MEDIA_CACHE:
        _MEDIA_CACHE.update(_read_media_types())
        if not _MEDIA_CACHE:
            _MEDIA_CACHE["?"] = None          # чтобы не спрашивать заново
    return _MEDIA_CACHE.get(letter)


@dataclass
class WipePlan:
    """Что произойдёт при затирании свободного места."""
    drive: str
    is_ssd: bool | None
    free_gb: float
    minutes_estimate: int
    warning: str


def plan_free_space_wipe(drive: str = "C") -> WipePlan:
    """Готовит план затирания и честно объясняет ограничения для SSD."""
    try:
        usage = shutil.disk_usage(f"{drive}:\\")
        free_gb = round(usage.free / (1024 ** 3), 1)
    except OSError:
        free_gb = 0.0
    ssd = drive_is_ssd(drive)

    # cipher пишет весь свободный объём: жёсткий диск ~30 МБ/с в один проход
    minutes = max(1, int(free_gb * 1024 / 30 / 60)) if free_gb else 1

    if ssd:
        warning = (
            "Это SSD. На нём затирание работает не так надёжно: контроллер сам "
            "решает, куда писать, и часть старых данных может остаться в резервных "
            "ячейках. Настоящий способ стереть SSD без следа — команда Secure Erase "
            "из фирменной утилиты производителя или шифрование всего диска BitLocker. "
            "Для SSD лучше просто включить BitLocker заранее — тогда любой удалённый "
            "файл уже зашифрован.")
    elif ssd is False:
        warning = (
            "Это обычный жёсткий диск — затирание сработает как надо: после него "
            "удалённые ранее файлы восстановить не получится. Займёт время: диск "
            "будет полностью записан один раз.")
    else:
        warning = ("Тип диска определить не удалось. Если это SSD, полной гарантии "
                   "затирание не даёт — см. пояснение для SSD.")
    return WipePlan(drive=drive, is_ssd=ssd, free_gb=free_gb,
                    minutes_estimate=minutes, warning=warning)


def wipe_free_space(drive: str = "C", timeout: int = 3600) -> tuple[bool, str]:
    """Затирает свободное место штатной командой Windows `cipher /w`.

    Команда встроена в Windows, ничего скачивать не нужно. Она пишет во
    временную папку, пока не кончится место, тремя проходами (нули, единицы,
    случайные данные), затем всё удаляет.
    """
    folder = f"{drive}:\\"
    if not Path(folder).exists():
        return False, "Такого диска нет."
    log(f"Затирание свободного места на {drive}: начато")
    code, out, err = run(["cipher", "/w:" + folder], timeout=timeout)
    if code == 0:
        log(f"Затирание свободного места на {drive}: завершено")
        return True, ("Свободное место затёрто. Ранее удалённые файлы на этом "
                      "диске восстановить больше нельзя.")
    text = (err or out or "").strip()
    if "timeout" in text:
        return False, ("Не хватило времени — диск большой. Затирание можно "
                       "запустить ещё раз, оно продолжит с чистого места.")
    return False, text or "Windows отклонила команду затирания."
