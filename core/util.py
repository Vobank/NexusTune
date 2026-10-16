"""Низкоуровневые утилиты: запуск команд, права администратора, пути приложения."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Скрываем консольные окна дочерних процессов
CREATE_NO_WINDOW = 0x08000000

APP_NAME = "Kremen"
_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home()))
APP_DIR = _LOCAL / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
SNAPSHOT_DIR = APP_DIR / "snapshots"
LOG_FILE = APP_DIR / "kremen.log"


def _migrate_old_data() -> None:
    """Переносит данные из папки прежнего имени, чтобы не потерять историю.

    Снимки настроек, дневник игр и история потребления копились раньше в
    NexusTune. Терять их при переименовании нельзя: без снимков нечем откатывать
    изменения системы.
    """
    old = _LOCAL / "NexusTune"
    if APP_DIR.exists() or not old.is_dir():
        return
    try:
        old.rename(APP_DIR)
        old_log = APP_DIR / "nexustune.log"
        if old_log.exists() and not LOG_FILE.exists():
            old_log.rename(LOG_FILE)
    except OSError:
        return


_migrate_old_data()
for _d in (APP_DIR, SNAPSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    """Пишет строку в журнал приложения."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except UnicodeEncodeError:
        # консоль в cp1251 — не роняем приложение из-за кириллицы
        print(line.encode("ascii", "replace").decode("ascii"))


def run(args, timeout: int = 20, shell: bool = False) -> tuple[int, str, str]:
    """Запускает процесс без всплывающего окна. Возвращает (код, stdout, stderr)."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=shell,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as exc:
        return -1, "", str(exc)
    except OSError as exc:
        return -1, "", str(exc)


def run_streaming(args, on_line=None, timeout: int = 1800,
                  encoding: str = "utf-8") -> tuple[int, str]:
    """Запускает долгую команду и отдаёт вывод построчно по мере появления.

    Нужно для sfc и dism: они идут минутами и печатают проценты. Ждать их
    молча нельзя — человек решит, что программа зависла.

    Возвращает (код возврата, весь вывод одной строкой).
    """
    collected: list[str] = []
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding=encoding, errors="replace",
            creationflags=CREATE_NO_WINDOW, bufsize=1)
    except OSError as exc:
        return -1, str(exc)

    deadline = time.time() + timeout
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                collected.append(line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass
            if time.time() > deadline:
                proc.kill()
                return -1, "\n".join(collected) + "\nПревышено время ожидания."
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait(timeout=30)
    return proc.returncode, "\n".join(collected)


def run_ps(script: str, timeout: int = 30) -> tuple[int, str, str]:
    """Выполняет PowerShell-скрипт.

    PowerShell по умолчанию отдаёт вывод в кодировке консоли (cp866/cp1251),
    поэтому кириллица приходит крякозябрами. Принудительно переводим вывод в UTF-8.
    """
    prelude = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
               "$OutputEncoding = [System.Text.Encoding]::UTF8; ")
    return run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", prelude + script],
        timeout=timeout,
    )


def ps_json(script: str, timeout: int = 30, depth: int = 4):
    """Выполняет PowerShell и разбирает вывод как JSON. None при ошибке.

    Скрипт обязательно обрезается: если он заканчивается переводом строки,
    приписанный конвейер превращается в пустой элемент, и PowerShell
    отказывается разбирать команду целиком.
    """
    code, out, _ = run_ps(
        f"{script.strip()} | ConvertTo-Json -Depth {depth} -Compress", timeout)
    if code != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


_ADMIN_CACHE: bool | None = None


def _token_elevated() -> bool | None:
    """Повышен ли токен процесса. Золотой стандарт проверки прав.

    Важно задать restype/argtypes: без этого на 64-битной Windows ctypes
    обрезает хэндл процесса до 32 бит, вызов проваливается, и приложение
    решает, что прав нет, хотя они есть. Именно на этом спотыкались наивные
    реализации.
    """
    try:
        k32 = ctypes.windll.kernel32
        adv = ctypes.windll.advapi32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]

        TOKEN_QUERY = 0x0008
        TokenElevation = 20
        handle = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY,
                                    ctypes.byref(handle)):
            return None
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD()
            if adv.GetTokenInformation(handle, TokenElevation,
                                       ctypes.byref(elevation),
                                       ctypes.sizeof(elevation),
                                       ctypes.byref(size)):
                return bool(elevation.value)
            return None
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def is_admin() -> bool:
    """Запущено ли приложение с правами администратора.

    Спрашиваем два независимых источника: повышен ли токен процесса и
    состоит ли пользователь в группе администраторов. Если хоть один
    уверенно говорит «да» — значит права есть. Результат считается один раз
    и запоминается: он не меняется за время работы процесса.
    """
    global _ADMIN_CACHE
    if _ADMIN_CACHE is not None:
        return _ADMIN_CACHE

    token = _token_elevated()
    try:
        shell = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        shell = False

    result = bool(token) or shell
    _ADMIN_CACHE = result
    log(f"Проверка прав администратора: токен={token}, "
        f"IsUserAnAdmin={shell} -> {'ЕСТЬ' if result else 'НЕТ'}")
    return result


def relaunch_as_admin() -> bool:
    """Перезапускает приложение с правами администратора (UAC)."""
    if is_admin():
        return True
    try:
        script = str(Path(sys.argv[0]).resolve())
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" {params}', None, 1
        )
        return rc > 32
    except Exception as exc:
        log(f"Не удалось поднять права: {exc}")
        return False


def load_config() -> dict:
    """Читает конфиг приложения."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    """Сохраняет конфиг приложения."""
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить конфиг: {exc}")


def human_bytes(num: float) -> str:
    """Байты -> человекочитаемая строка."""
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(num) < 1024.0:
            return f"{num:.0f} {unit}" if unit in ("Б", "КБ") else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} ПБ"
