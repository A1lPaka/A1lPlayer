# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


block_cipher = None

hiddenimports = (
    collect_submodules("faster_whisper")
    + [
        "ctranslate2",
        "vlc",
        "av",
        "onnxruntime",
        "tokenizers",
        "huggingface_hub",
    ]
)

a = Analysis(
    ["MainWindow.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/4k.png", "assets"),
        ("assets/arrowdown.svg", "assets"),
        ("assets/arrowup.svg", "assets"),
        ("assets/fullscreen.svg", "assets"),
        ("assets/logo.ico", "assets"),
        ("assets/logo.svg", "assets"),
        ("assets/pause.svg", "assets"),
        ("assets/pipopen.svg", "assets"),
        ("assets/play.svg", "assets"),
        ("assets/restore.svg", "assets"),
        ("assets/rewindleft.svg", "assets"),
        ("assets/rewindright.svg", "assets"),
        ("assets/stop.svg", "assets"),
        ("assets/timepopup.svg", "assets"),
        ("assets/trackspeed.svg", "assets"),
        ("assets/volumemute.svg", "assets"),
        ("assets/volumenomute.svg", "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tests",
        "torch",
        "torchaudio",
        "torchvision",
        "tensorflow",
        "numba",
        "llvmlite",
        "PIL",
        "imageio",
        "imageio_ffmpeg",
        "matplotlib",
        "networkx",
        "sympy",
    ],
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
    name="A1lPlayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/logo.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="A1lPlayer",
)
