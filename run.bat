@echo off
rem First run: installs dependencies, then starts DeskSearch with no console window.
cd /d "%~dp0"
python -c "import pynput, pystray, PIL, pypdf, mutagen, olefile, xlrd, watchdog" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Install failed. Make sure Python 3.10+ is installed and on PATH.
        pause
        exit /b 1
    )
)
start "" pythonw app.py
