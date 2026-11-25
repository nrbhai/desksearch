@echo off
rem Builds dist\DeskSearch.exe (no Python needed to run it afterwards)
rem and keeps a copy per version in releases\DeskSearch-<version>.exe.
rem Before building a new version: change VERSION in version.py and add a line to CHANGELOG.md.
cd /d "%~dp0"

tasklist /fi "imagename eq DeskSearch.exe" | find /i "DeskSearch.exe" >nul
if not errorlevel 1 (
    echo DeskSearch is running. Quit it from the tray icon first, then run build.bat again.
    pause
    exit /b 1
)

for /f %%v in ('python version.py') do set VER=%%v
echo Building DeskSearch v%VER% ...

python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 goto :fail
if not exist build mkdir build
python version.py --rcfile build\version_info.txt
if errorlevel 1 goto :fail
python -m PyInstaller --noconfirm --onefile --noconsole --name DeskSearch ^
    --version-file build\version_info.txt ^
    --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
    --hidden-import pystray._win32 --hidden-import watchdog.observers.read_directory_changes app.py
if errorlevel 1 goto :fail

if not exist releases mkdir releases
copy /y dist\DeskSearch.exe releases\DeskSearch-%VER%.exe >nul
echo.
echo Done: DeskSearch v%VER%
echo   Run this one : %~dp0dist\DeskSearch.exe
echo   Archived copy: %~dp0releases\DeskSearch-%VER%.exe
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
