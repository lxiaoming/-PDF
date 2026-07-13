# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（macOS / Windows 通用）

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
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
    name='图片转PDF',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # 不显示终端窗口（GUI 应用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS .app 捆绑包（Windows 构建时会自动跳过）
import sys as _sys
if _sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='图片转PDF.app',
        icon=None,
        bundle_identifier=None,
    )
