@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call scripts\find_python.bat
if errorlevel 1 goto no_python
if exist ".venv\Scripts\python.exe" goto install
"%HUB_PYTHON%" -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import pypdf, PIL, pymupdf, pythoncom, win32com.client; print('依赖检查通过')"
if errorlevel 1 goto failed
echo 安装完成。WorkBuddy 请使用本目录 .venv\Scripts\python.exe。
echo 现在可双击“启动网页控制台.bat”，或在 WorkBuddy 中使用本技能。
pause
exit /b 0
:no_python
echo 未检测到 Python 3.9+。请先安装 Python 并启用 PATH，或安装带 Python 的 WorkBuddy。
pause
exit /b 1
:failed
echo 依赖安装失败，请检查上方错误和网络连接后重新运行本脚本。
pause
exit /b 1
