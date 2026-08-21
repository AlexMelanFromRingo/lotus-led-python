@echo off
:: Lotus LED launcher. Everything after run.bat is passed straight through.
::   run.bat scan
::   run.bat color 255 0 128
::   run.bat mode ambient
::   run.bat            (interactive)
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo [ERROR] No venv here. Run install.bat first.
    exit /b 1
)
venv\Scripts\python.exe -m lotus_led.cli %*
