# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for DedupSuite 2.0.

Produces a single-file Windows executable (``DedupSuite.exe``) that:

* launches the CustomTkinter GUI when started with no arguments, and
* runs **headlessly** when a target directory / ``--headless`` flag is supplied
  (driven by :func:`dedup_suite.main`), for invocation by the Obsidian plugin.

Bundled, read-only assets (branding masters, ``app.ico``) are unpacked at
runtime to ``sys._MEIPASS`` and resolved via ``DedupApp._asset_base``. Writable
runtime state (``data_mine.db`` and exported ``logs/``) is created next to the
executable via ``DedupApp._runtime_base`` / ``DatabaseManager``.

Build:  pyinstaller dedup_suite.spec
Output: dist/DedupSuite.exe
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# CustomTkinter ships its theme JSON and bundled fonts as package data; without
# these the GUI cannot theme itself at runtime.
datas = collect_data_files("customtkinter")

# Project assets and the application icon. The first tuple element is the
# on-disk source; the second is the destination *inside* the bundle, matching
# the layout that DedupApp._asset_base() expects (``assets/...`` and ``app.ico``).
datas += [
    ("assets", "assets"),
    ("app.ico", "."),
]

# opentimestamps resolves some calendar/op modules dynamically; collect the
# whole package so notarisation works from the frozen build.
hiddenimports = collect_submodules("opentimestamps")

a = Analysis(
    ["dedup_suite.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DedupSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed build: a clean GUI with no console window. Headless CLI runs
    # still execute and stream stdout/stderr through pipes to a parent process
    # (e.g. the Obsidian ExecutionEngine's child_process.spawn).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["app.ico"],
)
