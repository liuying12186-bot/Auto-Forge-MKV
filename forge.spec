# -*- mode: python ; coding: utf-8 -*-
"""Auto-Forge-MKV / forge.spec

PyInstaller 打包配置：把 cli/main.py 打成单文件 exe。
    python -m PyInstaller forge.spec --noconfirm

要点：
1. cli/main.py 靠运行时 sys.path.insert 找 core 包，静态分析看不到，
   所以必须用 pathex 把项目根目录喂给 PyInstaller。
2. 不做 UPX 压缩：UPX 压过的 exe 常被杀软误报，追番工具没必要冒这个险。
3. 不 --windowed：这是命令行工具，需要控制台。
"""

block_cipher = None

a = Analysis(
    ['cli/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['core', 'core.util', 'core.probe', 'core.cut',
                   'core.audio', 'core.mux'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'test', 'distutils',
              'lib2to3', 'pip', 'setuptools'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AutoForgeMKV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
