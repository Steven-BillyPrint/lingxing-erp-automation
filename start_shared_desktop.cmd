@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_shared_desktop.ps1" %*
exit /b %ERRORLEVEL%
