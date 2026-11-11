@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0scripts\bootstrap-windows.ps1" --install-only
if errorlevel 1 goto failed
echo Local console: see the URL below. Keep this window open.
".venv\Scripts\python.exe" -u scripts\web_server.py --host 127.0.0.1
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup or server startup failed. See the error above.
pause
exit /b 1
