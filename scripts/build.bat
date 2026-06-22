@echo off
setlocal
REM Build from the project root (this script lives in scripts\).
cd /d "%~dp0.."
echo Building BulkDownloaderGUI for Windows...
pyinstaller --clean --noconfirm --distpath ..\dist --workpath ..\build\bulkdownloader BulkDownloaderGUI.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)
echo.
echo Done: dist\BulkDownloaderGUI.exe
