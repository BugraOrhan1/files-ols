# -*- mode: python ; coding: utf-8 -*-
# Reproducible onedir build: one OLSTuner.exe plus its bundled runtime.
# py-ols (GPLv3) is deliberately NOT bundled; it is only needed for
# "Bibliotheek bijwerken" and is loaded from a user-supplied folder.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "packaging", "ols_tuner_entry.py")],
    pathex=[ROOT],
    hiddenimports=["ols_platform.reader", "ols_platform.tuning.__main__"],
    excludes=["ols_reader", "pytest", "numpy", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OLSTuner",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OLSTuner")
