@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call scripts\find_python.bat
if errorlevel 1 (
    echo 未检测到 Python 3.9+。
    pause
    exit /b 1
)
"%HUB_PYTHON%" tools\build_skill_package.py
if errorlevel 1 (
    echo 发布包生成失败，请检查上方错误。
    pause
    exit /b 1
)
echo 发布包已生成到 dist。更新服务器时覆盖解压，保留现有 data 和 config.json。
pause
