@echo off
REM Stop polymas2api.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway.ps1" -Action stop
endlocal
