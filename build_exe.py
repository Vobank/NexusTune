"""Сборка КРЕМЕНЬ в самостоятельное приложение для Windows.

Результат — папка `dist/Kremen`, которую можно заархивировать и отдать другу.
Python на его компьютере не нужен: интерпретатор и Qt лежат внутри.

Запуск:  python build_exe.py            — обычная сборка (папка)
         python build_exe.py --onefile  — один файл, стартует медленнее
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "kremen.ico"
APP_NAME = "Kremen"

# Модули Qt, которые приложению не нужны. Без них сборка легче на сотни мегабайт.
EXCLUDED = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSerialPort",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvgWidgets",
    "tkinter", "unittest", "pydoc", "doctest", "test", "lib2to3",
    "matplotlib", "numpy", "PIL", "setuptools", "pip",
]


def make_icon() -> Path | None:
    """Рисует фирменный знак и сохраняет его в .ico."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 не установлен — иконка будет стандартной.")
        return None

    sys.path.insert(0, str(ROOT))
    from ui.brand import mark_pixmap

    QApplication.instance() or QApplication([])
    # Qt сохраняет .ico из одного изображения — берём самое крупное
    if mark_pixmap(256).save(str(ICON_PATH), "ICO"):
        print(f"Иконка готова: {ICON_PATH.name}")
        return ICON_PATH
    print("Иконку сохранить не удалось, продолжаю без неё.")
    return None


def build(onefile: bool = False) -> int:
    """Собирает приложение через PyInstaller."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller не установлен. Выполни: python -m pip install pyinstaller")
        return 1

    icon = make_icon()

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",                       # без чёрного окна консоли
        "--uac-admin",                      # сразу просит права администратора
        "--onefile" if onefile else "--onedir",
    ]
    if icon:
        args += ["--icon", str(icon)]
    for module in EXCLUDED:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "main.py"))

    print("Собираю, это займёт пару минут…\n")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("\nСборка не удалась.")
        return result.returncode

    target = ROOT / "dist" / (f"{APP_NAME}.exe" if onefile else APP_NAME)
    print(f"\nГотово: {target}")

    if not onefile and target.is_dir():
        # кладём рядом короткую инструкцию для того, кому отдадим папку
        readme = target / "ПРОЧТИ МЕНЯ.txt"
        readme.write_text(
            "КРЕМЕНЬ — центр настройки игрового ПК\n"
            "=========================================\n\n"
            "Запуск: Kremen.exe\n\n"
            "Windows спросит права администратора — это нужно, чтобы менять\n"
            "настройки системы, файл подкачки и предел мощности видеокарты.\n"
            "Без прав приложение тоже работает, но часть функций будет недоступна.\n\n"
            "Python и другие программы устанавливать не нужно — всё внутри.\n\n"
            "Windows SmartScreen может предупредить о неизвестном издателе:\n"
            "это нормально для программ без платной цифровой подписи.\n"
            "Нажми «Подробнее» и «Выполнить в любом случае».\n\n"
            "Управление подсветкой требует установленного OpenRGB (openrgb.org)\n"
            "со включённым SDK-сервером.\n\n"
            "Все изменения обратимы: профиль «Как с завода» возвращает систему\n"
            "в исходное состояние.\n",
            encoding="utf-8")
        size_mb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) / (1024 ** 2)
        print(f"Размер папки: {size_mb:.0f} МБ")
        print("Заархивируй папку dist/Kremen и отдавай друзьям.")
    return 0


def make_zip() -> None:
    """Пакует готовую сборку в архив."""
    folder = ROOT / "dist" / APP_NAME
    if not folder.is_dir():
        return
    archive = ROOT / "dist" / f"{APP_NAME}"
    shutil.make_archive(str(archive), "zip", root_dir=folder.parent, base_dir=APP_NAME)
    size_mb = Path(f"{archive}.zip").stat().st_size / (1024 ** 2)
    print(f"Архив готов: {archive}.zip ({size_mb:.0f} МБ)")


if __name__ == "__main__":
    onefile = "--onefile" in sys.argv
    code = build(onefile)
    if code == 0 and not onefile and "--zip" in sys.argv:
        make_zip()
    sys.exit(code)
