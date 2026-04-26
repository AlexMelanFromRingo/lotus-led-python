@echo off
setlocal enabledelayedexpansion

:: Always work from the directory this script lives in
cd /d "%~dp0"

echo ============================================================
echo   Lotus LED Controller - Windows Setup
echo   Working dir: %CD%
echo ============================================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

for /f "delims=" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo [OK] Found %PYVER%

:: Create venv
echo.
echo Creating virtual environment...
if exist venv\ (
    echo [SKIP] venv already exists. Delete it to reinstall.
) else (
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
    echo [OK] venv created at %CD%\venv
)

:: Upgrade pip
echo.
echo Upgrading pip...
venv\Scripts\python.exe -m pip install --upgrade pip -q

:: Install requirements
echo.
echo Installing packages...
venv\Scripts\pip.exe install -r requirements.txt

if errorlevel 1 (
    echo.
    echo [WARN] Some optional packages failed to install.
    echo        Core (bleak) required; others enable extra modes.
)

echo.
echo ============================================================
echo   Setup complete!
echo   Run:  run.bat --help
echo         run.bat scan
echo         run.bat on
echo         run.bat mode rainbow
echo         run.bat mode audio
echo         run.bat mode ambient
echo ============================================================
pause
