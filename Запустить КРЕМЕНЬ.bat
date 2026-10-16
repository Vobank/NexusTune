@echo off
chcp 65001 >nul
title КРЕМЕНЬ
cd /d "%~dp0"

rem --- проверяем права администратора, при необходимости перезапускаемся через UAC
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Запрашиваю права администратора...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem --- ищем интерпретатор
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0main.py"
    exit /b
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0main.py"
    exit /b
)

echo Python не найден. Установи Python 3.10 или новее с python.org
echo и не забудь галочку "Add python.exe to PATH".
pause
