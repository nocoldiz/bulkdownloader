#!/usr/bin/env bash
cd "$(dirname "$0")"

echo "BulkDownloader — Dependency Installer"
echo "======================================"

# Resolve a Python 3 interpreter (python3 preferred, fall back to python).
if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -q "Python 3"; then
    PYTHON="python"
else
    echo "  ERROR: Python 3 not found. Install it from https://python.org"
    echo "         macOS:  brew install python3"
    echo "         Linux:  sudo apt-get install python3 python3-pip  (or dnf/pacman)"
    exit 1
fi

# Core deps (always install)
echo "[1/4] Installing yt-dlp, curl_cffi, requests..."
if "$PYTHON" -m pip install -U yt-dlp "curl_cffi>=0.7.0" requests --quiet; then
    echo "      OK"
else
    echo "      WARNING: pip install failed for core deps. Check your Python installation."
fi

# Playwright (optional — headless browser fallback for JS-heavy sites)
if "$PYTHON" -c "import playwright" >/dev/null 2>&1; then
    echo "[2/4] Playwright already installed — skipping."
    echo "[3/4] Skipped."
else
    echo "[2/4] Installing Playwright (headless browser for JS-rendered sites)..."
    if "$PYTHON" -m pip install playwright --quiet; then
        echo "[3/4] Installing Chromium browser for Playwright..."
        if "$PYTHON" -m playwright install chromium --with-deps; then
            echo "      OK"
        else
            echo "      WARNING: Chromium install failed (optional — skipping)"
        fi
    else
        echo "      WARNING: Playwright install failed (optional — skipping)"
    fi
fi

# Verify curl_cffi impersonation actually works
echo "[4/4] Verifying curl_cffi impersonation..."
if "$PYTHON" -c "from curl_cffi import requests as r; r.get('https://example.com', impersonate='chrome', timeout=5)" >/dev/null 2>&1; then
    echo "      OK"
else
    echo "      WARNING: curl_cffi impersonation test failed."
    echo "      Try: pip install -U \"curl_cffi>=0.7.0\" --force-reinstall"
fi

# Make the launchers executable (so BulkDownloader.command double-clicks on macOS).
chmod +x ./launch.sh ./build.sh ./BulkDownloader.command 2>/dev/null || true

echo ""
echo "======================================"
echo "Done."
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "macOS: double-click  BulkDownloader.command  to launch the app,"
    echo "       or run  ./launch.sh  from Terminal."
else
    echo "Run  ./launch.sh  to start BulkDownloader."
fi
echo "======================================"
