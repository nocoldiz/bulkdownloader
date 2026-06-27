# -*- mode: python ; coding: utf-8 -*-
import sys

block_cipher = None

# Define your main entry point script here
app_name = 'BulkDownloaderGUI'
script_path = 'main.py'  # REPLACE with your actual main python filename

a = Analysis(
    [script_path],
    pathex=[],
    binaries=[],
    datas=[], # Add your assets (icons, images) here as tuples: ('source', 'destination')
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False, # Set to True if you want to see terminal output during debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=app_name,
)

# This block is CRITICAL for macOS .app generation
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='BulkDownloaderGUI.app',
        icon=None, # Path to your .icns file
        bundle_identifier='com.yourname.bulkdownloader',
    )