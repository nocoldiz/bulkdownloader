# -*- mode: python ; coding: utf-8 -*-
import sys
import shutil

# IMPORTANT: PyInstaller does NOT cross-compile. A macOS .app is only produced
# when this spec is built ON macOS (run ./build.sh there). Building on Windows
# yields BulkDownloaderGUI.exe; on Linux, a Linux ELF binary.

# UPX corrupts/breaks binaries on macOS (dylib loading + codesigning) and is
# usually absent on macOS/Linux, which would abort the build. Only enable it on
# non-Darwin platforms when the upx executable is actually available.
IS_MAC = sys.platform == 'darwin'
USE_UPX = not IS_MAC and shutil.which('upx') is not None

a = Analysis(
    ['src/bulkdownloader_gui.py'],
    pathex=['src'],
    binaries=[],
    datas=[('src/bulkdownloader.py', '.'), ('src/site_search.py', '.'),
           ('src/websites.json', '.'), ('src/categories.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if IS_MAC:
    # onedir + COLLECT + BUNDLE is the robust recipe for a real, launchable .app
    # (onefile-in-.app extracts to /tmp on every launch and trips Gatekeeper more
    # often). COLLECT produces dist/BulkDownloaderGUI/, BUNDLE wraps it as the .app.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='BulkDownloaderGUI',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='BulkDownloaderGUI',
    )
    app = BUNDLE(
        coll,
        name='BulkDownloaderGUI.app',
        bundle_identifier='com.aphroarchive.bulkdownloader',
        info_plist={
            'CFBundleName': 'BulkDownloaderGUI',
            'CFBundleDisplayName': 'AphroArchive Downloader',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'LSMinimumSystemVersion': '10.13.0',
        },
    )
else:
    # Windows / Linux: single self-contained executable.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='BulkDownloaderGUI',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=USE_UPX,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
