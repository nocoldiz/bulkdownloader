#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# NOTE: PyInstaller cannot cross-compile. To get a standalone macOS .app you must
# run THIS script on a Mac. Running it on Linux produces a Linux binary; the
# Windows .exe comes from build.bat on Windows.
#
# Don't need a packaged app on macOS? You don't have to build at all — just
# double-click  BulkDownloader.command  to run the GUI straight from source.

# ── Resolve a Python 3 interpreter ──────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -q "Python 3"; then
    PY=python
else
    echo "ERROR: Python 3 not found. Install it first (see launch.sh)." >&2
    echo "       macOS:  brew install python3" >&2
    echo "       Linux:  sudo apt-get install python3 python3-pip  (or dnf/pacman)" >&2
    exit 1
fi

# ── Ensure PyInstaller is available ─────────────────────────────────────
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "Installing PyInstaller..."
    "$PY" -m pip install -U pyinstaller
fi

if [[ "$OSTYPE" == "darwin"* ]]; then
    PLATFORM="macOS"
else
    PLATFORM="Linux"
fi

echo "Building BulkDownloaderGUI for $PLATFORM..."
"$PY" -m PyInstaller --clean --noconfirm \
    --distpath ../dist \
    --workpath ../build/bulkdownloader \
    BulkDownloaderGUI.spec

echo ""
if [[ "$PLATFORM" == "macOS" ]]; then
    chmod +x ./BulkDownloader.command 2>/dev/null || true
    if [ -d ../dist/BulkDownloaderGUI.app ]; then
        # Zip the .app bundle into a distributable release archive.
        ( cd ../dist && rm -f BulkDownloaderGUI-mac.zip \
            && zip -qr BulkDownloaderGUI-mac.zip BulkDownloaderGUI.app )
        echo "Done: dist/BulkDownloaderGUI.app  (release: dist/BulkDownloaderGUI-mac.zip)"
    else
        echo "WARNING: no .app bundle was produced by PyInstaller."
        echo "         Run the GUI from source instead — double-click  BulkDownloader.command"
    fi
else
    chmod +x ../dist/BulkDownloaderGUI 2>/dev/null || true
    echo "Done: dist/BulkDownloaderGUI"
fi
