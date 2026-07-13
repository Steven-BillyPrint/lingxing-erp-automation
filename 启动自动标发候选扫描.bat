@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "INTERVAL_SECONDS=10800"

:bootstrap
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Please install Python 3 and run this file again.
    goto setup_wait
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating Python virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo Failed to create virtual environment. Please check Python installation.
        goto setup_wait
    )
)

echo Installing or checking dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies. Please check network or Python environment.
    goto setup_wait
)

echo Installing browser runtime if needed...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo Browser runtime installation failed. The script will still try system Chrome.
)
goto main_menu

:setup_wait
echo.
echo Setup failed. This window will retry in 3 hours.
echo Press Ctrl+C to stop this window.
timeout /t %INTERVAL_SECONDS% /nobreak
goto bootstrap

:main_menu
echo.
echo ============================================================
echo 自动标发主菜单
echo 1. 启动自动标发巡检
echo 2. 管理阻止和待处理的队列订单
echo 0. 退出
echo ============================================================
set "MENU_CHOICE="
set /p MENU_CHOICE=请输入 1、2 或 0：
if "%MENU_CHOICE%"=="1" goto run_loop
if "%MENU_CHOICE%"=="2" goto queue_manage
if "%MENU_CHOICE%"=="0" goto end
echo 输入无效，请重新选择。
goto main_menu

:queue_manage
".venv\Scripts\python.exe" -m shipment_automation.cli queue manage
goto main_menu

:run_loop
echo ============================================================
echo Auto shipment inspection will run three steps.
echo Step 3 will operate Lingxing ERP for real after you confirm each order.
echo Please review the ERP logistics form carefully and enter y only when it is correct.
echo This window will stay open and run again every 3 hours. Press Ctrl+C to stop.
echo ============================================================
echo.
echo Step 1/3: scanning Lingxing ERP candidates...
".venv\Scripts\python.exe" -m shipment_automation.cli scan --dry-run
set "SCAN_EXIT_CODE=%ERRORLEVEL%"
if "%SCAN_EXIT_CODE%"=="3" (
    echo Shipment candidate scan was incomplete. Existing queued orders will continue; missing rows will be retried next round.
) else (
    if not "%SCAN_EXIT_CODE%"=="0" (
        echo Shipment candidate scan failed. Later steps will not start.
        goto wait_next_run
    )
)

echo.
echo Step 2/3: checking Alibaba logistics details from queue and updating local SQLite...
".venv\Scripts\python.exe" -m shipment_automation.cli logistics --from-queue --limit 20 --update-queue
if errorlevel 1 (
    echo Alibaba logistics lookup failed.
    goto wait_next_run
)

echo.
echo Step 3/3: marking ready orders in Lingxing ERP...
".venv\Scripts\python.exe" -m shipment_automation.cli erp-mark --execute --limit 20
if errorlevel 1 (
    echo ERP mark shipment batch completed with technical errors.
    goto wait_next_run
)

:wait_next_run
echo.
echo Next auto shipment inspection will start in 3 hours.
echo Press Ctrl+C to stop this window.
timeout /t %INTERVAL_SECONDS% /nobreak
goto run_loop

:end
endlocal
