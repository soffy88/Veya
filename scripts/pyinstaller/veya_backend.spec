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
    # 浏览器抓取通道 (playwright + 惰性子模块)
    "playwright",
    "playwright.async_api",
    "playwright.sync_api",
    "playwright.driver",
]

a = Analysis(
    [os.path.join(SPECPATH, "backend_launcher.py")],
    pathex=PATHS,
    binaries=[],
    # veya.platform 按 <root>/platform/3O/<lib> 相对结构检查并注入 sys.path →
    # 把主库目录原样复制进产物 _internal/veya/platform/3O
    datas=[
        (os.path.join(ROOT, "platform", "3O"), "veya/platform/3O"),
        # Playwright 浏览器二进制: 打包前执行
        #   PLAYWRIGHT_BROWSERS_PATH=$PW_PACK_DIR python -m playwright install chromium
        (os.environ.get("PW_PACK_DIR", os.path.expanduser("~/.cache/ms-playwright")), "ms-playwright"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPECPATH, "rthook_playwright.py")],
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
