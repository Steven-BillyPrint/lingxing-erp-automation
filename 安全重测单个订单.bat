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

set "PROMPT_TMP=%TEMP%\lingxing_safe_retry_%RANDOM%_%RANDOM%.txt"
".venv\Scripts\python.exe" -c "import os; from pathlib import Path; Path(os.environ['PROMPT_TMP']).write_text(input('\u8bf7\u8f93\u5165\u8981\u5b89\u5168\u91cd\u6d4b\u7684\u5e73\u53f0\u5355\u53f7\uff1a'), encoding='utf-8')"
if errorlevel 1 (
    echo Failed to read platform order number.
    pause
    exit /b 1
)
set /p "RETRY_ORDER=" < "%PROMPT_TMP%"
del "%PROMPT_TMP%" >nul 2>nul
if "%RETRY_ORDER%"=="" (
    ".venv\Scripts\python.exe" -c "print('\u672a\u8f93\u5165\u5e73\u53f0\u5355\u53f7\uff0c\u5df2\u53d6\u6d88\u3002')"
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import os; print('\u5f00\u59cb\u5b89\u5168\u91cd\u6d4b\uff1a' + os.environ.get('RETRY_ORDER', ''))"
".venv\Scripts\python.exe" -c "print('\u5b89\u5168\u91cd\u6d4b\u6a21\u5f0f\uff1a')"
".venv\Scripts\python.exe" -c "print('- \u53ef\u80fd\u4f1a\u5199\u56de ERP \u8054\u7cfb\u65b9\u5f0f\u5b57\u6bb5\u3002')"
".venv\Scripts\python.exe" -c "print('- \u4e0d\u5199\u5165\u6b63\u5f0f\u67e5\u91cd JSON\u3002')"
".venv\Scripts\python.exe" -c "print('- \u4e0d\u521b\u5efa Z \u76d8\u8ba2\u5355\u6587\u4ef6\u5939\uff0c\u4e0d\u590d\u5236\u5b9a\u5236 zip\u3002')"
set "ALLOW_SKU_ARG="
set "PROMPT_TMP=%TEMP%\lingxing_safe_retry_sku_%RANDOM%_%RANDOM%.txt"
".venv\Scripts\python.exe" -c "import os; from pathlib import Path; Path(os.environ['PROMPT_TMP']).write_text(input('\u662f\u5426\u5141\u8bb8\u672c\u6b21\u771f\u5b9e\u8c03\u6574\u5e10\u7bf7 SKU\uff1f\u8f93\u5165 y \u5141\u8bb8\uff0c\u76f4\u63a5\u56de\u8f66\u5219\u53ea\u751f\u6210\u8ba1\u5212\uff1a'), encoding='utf-8')"
if errorlevel 1 (
    echo Failed to read SKU adjustment choice.
    pause
    exit /b 1
)
set /p "ALLOW_SKU=" < "%PROMPT_TMP%"
del "%PROMPT_TMP%" >nul 2>nul
if /I "%ALLOW_SKU%"=="Y" set "ALLOW_SKU_ARG=--allow-sku-adjustment"
if /I "%ALLOW_SKU%"=="YES" set "ALLOW_SKU_ARG=--allow-sku-adjustment"

".venv\Scripts\python.exe" lingxing_web_sync.py --retry-order "%RETRY_ORDER%" --apply --no-dedupe-write --no-create-folder --keep-browser-open %ALLOW_SKU_ARG%
pause
