@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

if not defined PRINT_HUB_CONVERTER_MODE set "PRINT_HUB_CONVERTER_MODE=gotenberg"
if not defined PRINT_HUB_GOTENBERG_URL set "PRINT_HUB_GOTENBERG_URL=http://127.0.0.1:3000"

set "HUB_PYTHON="
if exist ".venv\Scripts\python.exe" set "HUB_PYTHON=.venv\Scripts\python.exe"
if not defined HUB_PYTHON (
    where python >nul 2>&1 && set "HUB_PYTHON=python"
)
if not defined HUB_PYTHON (
    echo [错误] 未找到可用的 Python，请先在项目根目录运行 安装依赖.bat
    pause
    exit /b 1
)

echo 正在启动中心打印服务器 (模式: %PRINT_HUB_CONVERTER_MODE%, Gotenberg: %PRINT_HUB_GOTENBERG_URL%)...
"%HUB_PYTHON%" scripts\web_server.py --host 0.0.0.0
pause
