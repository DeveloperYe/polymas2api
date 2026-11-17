@echo off
REM Start or stop polymas2api. The real work lives in gateway.ps1.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway.ps1" -Action %1
endlocal
