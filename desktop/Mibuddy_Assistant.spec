# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/D/work_place/desktop/packaging/../main.py'],
    pathex=[],
    binaries=[('C:/Users/admin/miniconda3/envs/mckj/Library/bin/ffi-7.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/ffi-8.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/ffi.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/sqlite3.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/zlib.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/libssl-3-x64.dll', '.'), ('C:/Users/admin/miniconda3/envs/mckj/Library/bin/libcrypto-3-x64.dll', '.')],
    datas=[('C:/D/work_place/desktop/packaging/../pca.json', '.'), ('C:/D/work_place/desktop/packaging/../ui', 'ui'), ('C:/D/work_place/desktop/packaging/../assets', 'assets')],
    hiddenimports=['qasync'],
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
    name='Mibuddy_Assistant',
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
    icon=['C:/D/work_place/desktop/packaging/../assets/mibuddy.ico'],
)
