@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Lotus LED - Windows setup
echo   %CD%
echo ============================================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not on PATH. Install Python 3.10+ from python.org
    echo         and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

for /f "delims=" %%i in ('python --version 2^>^&1') do echo [ok] %%i

if exist venv\ (
    echo [skip] venv already exists - delete the folder to start over
) else (
    echo Creating virtual environment...
    python -m venv venv || (echo [ERROR] could not create venv & pause & exit /b 1)
)

echo Installing...
venv\Scripts\python.exe -m pip install --upgrade pip -q
venv\Scripts\pip.exe install -e ".[full,dev]"
if errorlevel 1 (
    echo.
    echo [warn] Some optional packages failed. bleak is the only hard requirement;
    echo        the rest add audio, ambilight and system-monitor modes.
)

echo.
echo ============================================================
echo   Done. Try:
echo     run.bat scan
echo     run.bat color ff8800
echo     run.bat mode music
echo     run.bat modes
echo ============================================================
pause
