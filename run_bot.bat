@echo off
setlocal
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

echo ============================
echo   Nyori bot launcher
echo ============================

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Install it from https://python.org ^(check "Add python.exe to PATH" during install^) and try again.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/3] First run: creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo [2/3] Checking dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo WARNING: full install failed. Trying the core packages only...
    echo ^(the porn filter needs nudenet and will be disabled until it installs^)
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-core.txt
    if errorlevel 1 (
        echo.
        echo ERROR: failed to install even the core dependencies.
        echo See the pip output above for the real reason ^(usually: no internet connection,
        echo a firewall/antivirus blocking Python, or an outdated pip/Python^).
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo ERROR: .env file not found. It must contain a line like TOKEN=your_bot_token
    pause
    exit /b 1
)

findstr /b /c:"PROXY_URL" ".env" >nul 2>nul
if not errorlevel 1 (
    echo PROXY_URL found in .env - installing PySocks for socks5 support...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check PySocks
)

echo [3/3] Starting the bot...
echo (To stop the bot, just close this window)
echo.
".venv\Scripts\python.exe" bot.py

echo.
echo The bot process has stopped. If that was unexpected, check the error text above or bot.log
pause
