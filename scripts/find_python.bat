@echo off
set "HUB_PYTHON="
if exist "%~dp0..\.venv\Scripts\python.exe" set "HUB_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if defined HUB_PYTHON goto validate
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "HUB_PYTHON=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if defined HUB_PYTHON goto validate
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "HUB_PYTHON=%%P"
if defined HUB_PYTHON goto validate
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "HUB_PYTHON=%%P"
if not defined HUB_PYTHON exit /b 1
:validate
"%HUB_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
