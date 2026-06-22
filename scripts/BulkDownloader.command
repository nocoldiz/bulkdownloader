#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Double-clickable macOS launcher for the AphroArchive Download Manager.
#
# macOS has no standalone .app for this tool (PyInstaller can't cross-compile one
# from Windows/Linux). Instead, this runs the Python GUI directly from source.
#
# Finder runs .command files in Terminal when you double-click them. If a
# double-click does nothing (or opens a text editor), run this once in Terminal
# from the bulkdownloader folder:
#
#       chmod +x BulkDownloader.command
#
# …or right-click the file → Open the first time to clear Gatekeeper.
# ─────────────────────────────────────────────────────────────────────────────

# Run from the project root (this script lives in scripts/).
cd "$(dirname "$0")/.." || exit 1

say()   { printf '%s\n' "$*"; }
pause() { read -r -p "Press Enter to close this window…" _ ; }

# ── Resolve a Python 3 interpreter ──────────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' >/dev/null 2>&1; then
    PY=python
else
    say "Python 3 is required but was not found."
    say "  Install it from https://python.org  (or:  brew install python3)"
    pause; exit 1
fi

# ── The GUI needs Tkinter ───────────────────────────────────────────────────
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    say "This Python is missing Tkinter, which the GUI needs."
    say "  Fix: install Python from https://python.org (it bundles Tk),"
    say "       or with Homebrew:  brew install python-tk"
    pause; exit 1
fi

# ── One-time dependency install (yt-dlp etc.) ───────────────────────────────
if ! "$PY" -c 'import yt_dlp' >/dev/null 2>&1; then
    say "First run — installing dependencies (yt-dlp, curl_cffi)…"
    if [ -f scripts/install.sh ]; then
        bash scripts/install.sh || { say "Dependency install failed — see the messages above."; pause; exit 1; }
    else
        "$PY" -m pip install -U yt-dlp "curl_cffi>=0.7.0" requests || { pause; exit 1; }
    fi
fi

say "Launching AphroArchive Download Manager…"
exec "$PY" src/bulkdownloader_gui.py
