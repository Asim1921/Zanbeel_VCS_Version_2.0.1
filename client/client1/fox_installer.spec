# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for FoxNest Installer
# This bundles fox.exe inside the installer

import os

block_cipher = None

# Get the directory containing this spec file
spec_dir = os.path.dirname(os.path.abspath(SPEC))

# Look for fox.exe in dist folder or current directory
fox_exe_paths = [
    os.path.join(spec_dir, '..', 'dist', 'fox.exe'),
    os.path.join(spec_dir, 'dist', 'fox.exe'),
    os.path.join(spec_dir, 'fox.exe'),
]

fox_exe_path = None
for path in fox_exe_paths:
    if os.path.exists(path):
        fox_exe_path = path
        break

if fox_exe_path:
    print(f"Found fox.exe at: {fox_exe_path}")
    datas = [(fox_exe_path, '.')]
else:
    print("WARNING: fox.exe not found! Build fox.exe first using build.bat")
    datas = []

a = Analysis(
    ['fox_installer.py'],
    pathex=[spec_dir],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='FoxNest-Setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window - GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if you have one
    uac_admin=False,  # Don't require admin by default
)

