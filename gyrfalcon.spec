# PyInstaller spec for building the `gyrfalcon` CLI as a standalone executable.
# Build with: uv run pyinstaller gyrfalcon.spec --noconfirm
#
# Bundles the pre-built web dashboard (web/dist) so `gyrfalcon dashboard` works
# from the frozen binary. Run `cd web && npm run build` first if it's stale.

from PyInstaller.utils.hooks import collect_submodules

datas = []
web_dist = "web/dist"
import os
if os.path.isdir(web_dist):
    datas.append((web_dist, "web/dist"))

# Stdlib modules that nothing in gyrfalcon imports statically, so PyInstaller's
# analysis never sees them — but user-authored files loaded at *runtime* from
# ~/.gyrfalcon/flows/ and ~/.gyrfalcon/skills/scripts/ commonly do. Without
# these, such a file dies on ModuleNotFoundError inside the frozen binary and
# `discover_flows()` silently skips it: the flow just never appears in the
# dashboard, while the same file works fine when run from source.
user_code_stdlib = [
    "imaplib",
    "poplib",
    "smtplib",
    "email",
    "mailbox",
    "csv",
    "sqlite3",
    "xml.etree.ElementTree",
    "zipfile",
    "tarfile",
    "hashlib",
    "hmac",
    "uuid",
    "textwrap",
    "difflib",
    "statistics",
    "decimal",
    "ipaddress",
]

hiddenimports = (
    collect_submodules("gyrfalcon.plugins")
    + collect_submodules("gyrfalcon.providers")
    + collect_submodules("gyrfalcon.tools")
    + collect_submodules("gyrfalcon.gateway")
    + collect_submodules("gyrfalcon.flow")
    + collect_submodules("uvicorn")
    + user_code_stdlib
)

a = Analysis(
    ["gyrfalcon_cli/main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gyrfalcon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
