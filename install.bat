@echo off
cd /d "%~dp0"

echo BulkDownloader - Dependency Installer
echo ======================================

REM Core deps (always install)
echo [1/4] Installing yt-dlp, curl_cffi, requests...
python -m pip install -U yt-dlp "curl_cffi>=0.7.0" requests --quiet
if errorlevel 1 (
    echo      WARNING: pip install failed for core deps. Check your Python installation.
) else (
    echo      OK
)

REM Playwright (optional - headless browser fallback for JS-heavy sites)
python -c "import playwright" 2>nul
if errorlevel 1 (
    echo [2/4] Installing Playwright ^(headless browser for JS-rendered sites^)...
    python -m pip install playwright --quiet
    if errorlevel 1 (
        echo      WARNING: Playwright install failed ^(optional - skipping^)
    ) else (
        echo [3/4] Installing Chromium browser for Playwright...
        python -m playwright install chromium --with-deps
        if errorlevel 1 (
            echo      WARNING: Chromium install failed ^(optional - skipping^)
        ) else (
            echo      OK
        )
    )
) else (
    echo [2/4] Playwright already installed - skipping.
    echo [3/4] Skipped.
)

REM Verify curl_cffi impersonation actually works
echo [4/4] Verifying curl_cffi impersonation...
python -c "from curl_cffi import requests as r; r.get('https://example.com', impersonate='chrome', timeout=5)" 2>nul
if errorlevel 1 (
    echo      WARNING: curl_cffi impersonation test failed.
    echo      Try: pip install -U "curl_cffi>=0.7.0" --force-reinstall
) else (
    echo      OK
)

echo.
echo ======================================
echo Done. Run launch.bat to start BulkDownloader.
echo ======================================
pause
