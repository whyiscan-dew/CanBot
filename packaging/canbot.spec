# -*- mode: python ; coding: utf-8 -*-
# CanBot.exe üretimi:  pyinstaller packaging/canbot.spec --noconfirm
# Tek dosyalık, konsolsuz (pencereli) Windows uygulaması üretir.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "bot", "web", "static", "icons", "canbot.ico")

datas = [(os.path.join(ROOT, "bot", "web", "static"), os.path.join("bot", "web", "static"))]
datas += collect_data_files("webview")

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("bot")
hiddenimports += collect_submodules("truststore")
hiddenimports += [
    "pystray._win32",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "qrcode.image.svg",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CanBot",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON,
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
)
