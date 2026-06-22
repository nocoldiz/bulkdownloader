@echo off
cd /d "%~dp0"

REM Make sure dependencies are installed - run install.bat first if yt-dlp is missing.
python -c "import yt_dlp" 2>nul
if errorlevel 1 (
    echo yt-dlp is not installed.
    echo Please run install.bat first to set up dependencies.
    echo.
    pause
    exit /b 1
)

REM Launch the GUI directly.
python bulkdownloader_gui.py
