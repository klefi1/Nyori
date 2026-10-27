@echo off
setlocal
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
    echo Not found: .venv
    echo Run run_bot.bat first at least once - it creates the virtual environment.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" console.py

pause
