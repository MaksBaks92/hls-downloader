# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
is_windows = sys.platform.startswith("win")
yt_name = "yt-dlp.exe" if is_windows else "yt-dlp"
ff_name = "ffmpeg.exe" if is_windows else "ffmpeg"

datas = [("app.ico", ".")]
binaries = []
for name in (ff_name, yt_name):
    candidate = root / "bin" / name
    if candidate.exists():
        binaries.append((str(candidate), "bin"))

hiddenimports = []
for pkg in ("curl_cffi", "PIL", "cryptography"):
    collected = collect_all(pkg)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

a = Analysis(
    ["hls_downloader.py"],
    pathex=[],
    binaries=binaries,
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
    [],
    exclude_binaries=True,
    name="HLS Downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HLS Downloader",
)
