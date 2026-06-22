#!/usr/bin/env bash
cd "$(dirname "$0")"

# Resolve a Python 3 interpreter (python3 preferred, fall back to python).
if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' >/dev/null 2>&1; then
    PYTHON="python"
else
    echo "  ERROR: Python 3 not found. Install it from https://python.org"
    echo "         macOS:  brew install python3"
    echo "         Linux:  sudo apt-get install python3 python3-pip  (or dnf/pacman)"
    exit 1
fi

# The GUI needs Tkinter.
if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    echo "  ERROR: Python is missing Tkinter (required for the GUI)."
    echo "         macOS:  use python.org Python, or  brew install python-tk"
    echo "         Linux:  sudo apt-get install python3-tk"
    exit 1
fi

# Make sure dependencies are installed — run ./install.sh first if yt-dlp is missing.
if ! "$PYTHON" -c "import yt_dlp" >/dev/null 2>&1; then
    echo "yt-dlp is not installed."
    echo "Please run ./install.sh first to set up dependencies."
    exit 1
fi

# Launch the GUI directly.
exec "$PYTHON" src/bulkdownloader_gui.py
