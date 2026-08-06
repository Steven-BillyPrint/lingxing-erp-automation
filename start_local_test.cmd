@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local_test.ps1" -ConfirmLocalTestRun %*
exit /b %ERRORLEVEL%
