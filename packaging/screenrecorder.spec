# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec. Run from the repository root:
#     pyinstaller packaging/screenrecorder.spec --noconfirm --clean
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
native_ext = sorted((ROOT / "screenrecorder").glob("_srnative*.pyd"))
if not native_ext:
    raise SystemExit("screenrecorder/_srnative*.pyd not found: run build_native.ps1 first")

icon = next((str(p) for p in (ROOT / "packaging" / "screenrecorder.ico", ROOT / "packaging" / "screenrecorder.png") if p.exists()), None)

hiddenimports = ["screenrecorder._srnative"]
hiddenimports += collect_submodules("winrt")
hiddenimports += collect_submodules("dxcam")

excludes = [
    # Never ship media frameworks: encoding is Media Foundation only.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtPdf",
    "PySide6.Qt3DCore",
    "cv2",
    "av",
    "imageio",
    "imageio_ffmpeg",
    "moviepy",
    "tkinter",
    "matplotlib",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[(str(p), "screenrecorder") for p in native_ext],
    datas=[(str(ROOT / "LICENSE"), ".")],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScreenRecorder",
    console=False,
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ScreenRecorder", upx=False)
