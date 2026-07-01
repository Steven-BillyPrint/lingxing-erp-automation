@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Please install Python 3 and run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating Python virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment. Please check Python installation.
        pause
        exit /b 1
    )
)

echo Installing or checking dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies. Please check network or Python environment.
    pause
    exit /b 1
)

echo Installing browser runtime if needed...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo Browser runtime installation failed. The script will still try system Chrome.
)

echo Starting Lingxing batch patrol. The script will switch to order view, skip processed platform order numbers, and repeat every 5 minutes.
".venv\Scripts\python.exe" lingxing_web_sync.py --batch --loop --batch-interval-minutes 5 --keep-browser-open
pause
