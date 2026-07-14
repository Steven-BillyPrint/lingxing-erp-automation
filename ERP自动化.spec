# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).resolve()
playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
hidden_imports = [
    *collect_submodules("erp_automation"),
    *collect_submodules("lingxing_automation"),
    *collect_submodules("shipment_automation"),
    *playwright_hidden,
]

a = Analysis(
    [str(project_root / "desktop_main.py")],
    pathex=[str(project_root)],
    binaries=playwright_binaries,
    datas=[
        *playwright_datas,
        (str(project_root / "data" / "china_workdays.json"), "data"),
        (str(project_root / "rules" / "sku_rules.example.json"), "rules"),
        (str(project_root / "rules" / "split_rules.example.json"), "rules"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ERP自动化",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ERP自动化",
)
