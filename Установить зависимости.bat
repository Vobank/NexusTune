@echo off
chcp 65001 >nul
title КРЕМЕНЬ - установка зависимостей
cd /d "%~dp0"
echo Ставлю библиотеки, нужные приложению...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Готово. Теперь запускай "Запустить КРЕМЕНЬ.bat".
pause
