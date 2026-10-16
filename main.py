"""КРЕМЕНЬ — центр настройки и оптимизации игрового ПК.

Запуск:  python main.py
Лучше запускать от имени администратора — часть настроек Windows,
управление файлом подкачки и пределом мощности видеокарты требуют прав.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QRectF
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont

from core.context import AppContext
from core.util import load_config, log
from ui import theme
from ui.brand import app_icon, draw_wordmark
from ui.window import MainWindow


def make_splash() -> QSplashScreen:
    """Заставка на время определения железа."""
    pix = QPixmap(480, 210)
    pix.fill(QColor(theme.BG))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(theme.BORDER_LIGHT))
    p.drawRoundedRect(1, 1, 477, 207, 16, 16)

    draw_wordmark(p, QRectF(46, 34, 400, 104), 96, theme.TEXT, theme.TEXT_DIM)

    p.setPen(QColor(theme.ACCENT))
    p.setFont(QFont("Segoe UI", 10, QFont.Bold))
    p.drawText(QRectF(0, 152, 480, 30), Qt.AlignCenter, "Изучаю железо компьютера…")
    p.end()
    return QSplashScreen(pix)


def install_error_handler() -> None:
    """Показывает окно с ошибкой вместо тихого падения при запуске через pythonw."""
    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log("Необработанная ошибка:\n" + text)
        try:
            QMessageBox.critical(
                None, "КРЕМЕНЬ — ошибка",
                f"Что-то пошло не так:\n\n{exc_value}\n\n"
                "Подробности записаны в журнал:\n"
                "%LOCALAPPDATA%\\Kremen\\kremen.log")
        except Exception:
            pass

    sys.excepthook = hook


def run_maintenance(jobs: list) -> int:
    """Тихий режим для планировщика: без окон, только работа и запись в журнал."""
    from core import maintenance
    log("Тихое обслуживание: " + ", ".join(jobs))
    for line in maintenance.run_jobs(jobs):
        log("  " + line)
    return 0


def main() -> int:
    # Планировщик Windows запускает приложение с ключом --maintenance:
    # тогда интерфейс не нужен вовсе.
    from core import maintenance
    jobs = maintenance.parse_arguments(sys.argv)
    if jobs is not None:
        return run_maintenance(jobs)

    install_error_handler()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("КРЕМЕНЬ")
    # палитру читаем до создания виджетов, иначе окно моргнёт чужими цветами
    saved = load_config().get("palette", theme.DEFAULT_PALETTE)
    app.setStyleSheet(theme.apply_palette(saved))
    app.setWindowIcon(app_icon())

    splash = make_splash()
    splash.show()
    app.processEvents()

    # если в прошлый раз забыли выключить «Ультиматум» и время вышло —
    # возвращаем защиту молча ещё до создания окна
    try:
        from core import ultimatum
        ultimatum.recover_if_expired()
    except Exception:
        pass

    try:
        ctx = AppContext.create()
    except Exception as exc:
        splash.close()
        log("Не удалось определить железо:\n" + traceback.format_exc())
        QMessageBox.critical(None, "КРЕМЕНЬ",
                             f"Не удалось собрать данные о компьютере:\n{exc}")
        return 1

    window = MainWindow(ctx)
    splash.finish(window)
    if ctx.config.get("start_minimized") and window.tray.isVisible():
        window.hide()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
