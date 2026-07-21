# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


# CustomTkinter loads its themes and images at runtime, so include its complete
# package data. Codex is launched from the user's machine at runtime; its CLI,
# OAuth profile, keyring entries, and credentials must remain external.
datas, binaries, hiddenimports = collect_all("customtkinter")
excludes = [
    "codex",
    "openai",
    "authlib",
    "oauthlib",
]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TranscriptQuiz",
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
    name="TranscriptQuiz",
)
