@echo off
:: Lotus LED Controller launcher
:: Usage: run.bat [command] [args...]
:: Examples:
::   run.bat              (interactive TUI)
::   run.bat scan
::   run.bat on / off
::   run.bat color 255 0 128
::   run.bat brightness 70
::   run.bat mode rainbow
::   run.bat mode audio
::   run.bat mode ambient
::   run.bat scene movie
::   run.bat status

if not exist venv\Scripts\python.exe (
    echo [ERROR] venv not found. Run install.bat first.
    exit /b 1
)

venv\Scripts\python.exe lotus_controller.py %*
