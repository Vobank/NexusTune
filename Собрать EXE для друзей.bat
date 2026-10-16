@echo off
chcp 65001 >nul
title КРЕМЕНЬ - сборка для друзей
cd /d "%~dp0"
echo Собираю самостоятельное приложение. Это займёт 2-3 минуты...
echo.
python -m pip install --quiet pyinstaller
python build_exe.py --zip
echo.
echo Готовый архив лежит в папке dist. Его можно отправить другу -
echo Python и другие программы ему устанавливать не нужно.
pause
