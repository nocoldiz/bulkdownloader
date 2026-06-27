#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# ── Parse Arguments ─────────────────────────────────────────────────────
TARGET="auto"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --macos) TARGET="macOS"; shift ;;
        --linux) TARGET="Linux"; shift ;;
        --windows) TARGET="Windows"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
done

# ── Determine Current OS ────────────────────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
    CURRENT_OS="macOS"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
    CURRENT_OS="Windows"
else
    CURRENT_OS="Linux"
fi

if [ "$TARGET" == "auto" ]; then
    TARGET=$CURRENT_OS
fi

# ── Cross-Compilation Reality Check ─────────────────────────────────────
if [ "$TARGET" != "$CURRENT_OS" ]; then
    echo "ERROR: You are on $CURRENT_OS, but requested a build for $TARGET."
    echo "PyInstaller cannot cross-compile. You must run this script natively on a $TARGET machine, VM, or CI/CD runner."
    exit 1
fi

# ── Resolve a Python 3 interpreter ──────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -q "Python 3"; then
    PY=python
else
    echo "ERROR: Python 3 not found. Install it first." >&2
    echo "       macOS:  brew install python3" >&2
    echo "       Linux:  sudo apt-get install python3 python3-pip" >&2
    exit 1
fi

# ── Ensure PyInstaller is available ─────────────────────────────────────
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "Installing PyInstaller..."
    "$PY" -m pip install -U pyinstaller
fi

# ── Build Application ───────────────────────────────────────────────────
echo "Building BulkDownloaderGUI for $TARGET..."
"$PY" -m PyInstaller --clean --noconfirm \
    --distpath ./dist \
    --workpath ./build/bulkdownloader \
    BulkDownloaderGUI.spec

echo ""

# ── Platform-Specific Packaging ─────────────────────────────────────────
if [[ "$TARGET" == "macOS" ]]; then
    chmod +x ./BulkDownloader.command 2>/dev/null || true
    
    if [ -d ./dist/BulkDownloaderGUI.app ]; then
        echo "Creating macOS installable DMG..."
        cd ./dist
        
        # Remove old DMG if it exists
        rm -f BulkDownloaderGUI-mac.dmg
        
        # Create a Disk Image (.dmg) containing the .app bundle
        hdiutil create -volname "BulkDownloader" -srcfolder BulkDownloaderGUI.app -ov -format UDZO BulkDownloaderGUI-mac.dmg
        
        # Also zip it just in case
        rm -f BulkDownloaderGUI-mac.zip
        zip -qr BulkDownloaderGUI-mac.zip BulkDownloaderGUI.app
        
        echo "Done: dist/BulkDownloaderGUI.app"
        echo "Release ready: dist/BulkDownloaderGUI-mac.dmg (Installable)"
    else
        echo "WARNING: no .app bundle was produced by PyInstaller."
        echo "Check your .spec file. You must have an 'app = BUNDLE(...)' block to generate a macOS .app."
    fi

elif [[ "$TARGET" == "Windows" ]]; then
    echo "Done: Check the dist/ folder for your Windows executable (.exe)."

else
    # Linux build
    chmod +x ./dist/BulkDownloaderGUI 2>/dev/null || true
    echo "Done: dist/BulkDownloaderGUI (Linux binary)"
fi