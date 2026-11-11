@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call scripts\find_python.bat
if errorlevel 1 goto no_python
"%HUB_PYTHON%" -c "import pypdf, PIL, pymupdf, pythoncom, win32com.client" >nul 2>&1
if errorlevel 1 goto no_deps
echo 局域网访问：无登录鉴权，仅用于可信公司网络
echo 浏览器地址为启动日志中的 Local URL，关闭本窗口会停止服务。
"%HUB_PYTHON%" -u scripts\web_server.py --host 0.0.0.0
pause
exit /b 0
:no_python
echo 未检测到 Python 3.9+，请先安装 Python 或 WorkBuddy。
pause
exit /b 1
:no_deps
echo 请先双击“安装依赖.bat”，完成后再启动。
pause
exit /b 1
