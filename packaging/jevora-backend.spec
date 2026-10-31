# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/thaku/hackathon-projects/Jevora-IDE/Jevora-IDE/packaging/backend_main.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/thaku/hackathon-projects/Jevora-IDE/Jevora-IDE/backend/agents/system_prompt.md', 'backend/agents'), ('C:/Users/thaku/hackathon-projects/Jevora-IDE/Jevora-IDE/backend/db/migrations', 'backend/db/migrations')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.wsproto_impl', 'uvicorn.lifespan.on', 'aiosqlite', 'anyio._backends._asyncio'],
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
    name='jevora-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    name='jevora-backend',
)
