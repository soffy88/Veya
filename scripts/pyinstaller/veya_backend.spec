# -*- mode: python ; coding: utf-8 -*-
# veya 桌面后端 PyInstaller spec
# 构建: pyinstaller scripts/pyinstaller/veya_backend.spec --noconfirm
# 产物: dist/veya-backend/veya-backend (单目录, 供 Tauri resources/backend 打包)

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# 3O 主库路径 (veya.platform.load 运行时注入 sys.path, PyInstaller 需显式收集)
MAINLIBS = ["obase", "oprim", "oskill", "omodul", "oservi"]
# import veya / server 需要仓库根在 sys.path; import obase 等需要各主库目录
PATHS = [ROOT] + [os.path.join(ROOT, "platform", "3O", lib) for lib in MAINLIBS]

hiddenimports = [
    # 3O 主库 (顶层包)
    *MAINLIBS,
    # 主库内惰性导入的模块
    "oskill.recurring_scheduler",
    "obase.plugin_registry",
    "obase.knowledge_store",
    "omodul.cindy_mcp_server",
    "oskill.skills_dynamic_inject",
    "veya.mcp_server",
    # 根 app 运行时惰性导入
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "pgmpy",
    "yaml",
    # 3O 主库惰性导入的第三方 (PyInstaller 静态分析看不到)
    "cryptography",
    "cryptography.fernet",
    "rapidfuzz",
    "argon2",
    "asyncpg",
    "tree_sitter",
    "tree_sitter_python",
    "chardet",
    "anthropic",
    "openai",
    "mcp",
]

a = Analysis(
    [os.path.join(SPECPATH, "backend_launcher.py")],
    pathex=PATHS,
    binaries=[],
    # veya.platform 按 <root>/platform/3O/<lib> 相对结构检查并注入 sys.path →
    # 把主库目录原样复制进产物 _internal/veya/platform/3O
    datas=[(os.path.join(ROOT, "platform", "3O"), "veya/platform/3O")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib.tests", "pandas.tests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="veya-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="veya-backend",
)
