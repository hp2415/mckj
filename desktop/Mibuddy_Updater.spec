# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\D\\work_place\\desktop\\packaging\\..\\update_bootstrap.py'],
    pathex=[],
    binaries=[('D:\\miniconda\\envs\\ai_env\\Library\\bin\\ffi-7.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\ffi-8.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\ffi.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\sqlite3.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\zlib.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\libssl-3-x64.dll', '.'), ('D:\\miniconda\\envs\\ai_env\\Library\\bin\\libcrypto-3-x64.dll', '.')],
    datas=[],
    hiddenimports=[],
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
    name='Mibuddy_Updater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:\\D\\work_place\\desktop\\assets\\mibuddy.ico'],
)
