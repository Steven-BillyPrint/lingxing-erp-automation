@echo off
setlocal
echo start_shared_desktop.cmd is now the source local-test entry point.
call "%~dp0start_local_test.cmd" %*
exit /b %ERRORLEVEL%
