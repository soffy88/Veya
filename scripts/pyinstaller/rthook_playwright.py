"""PyInstaller runtime hook — 指向打包内的 Playwright 浏览器二进制。

打包产物布局: _internal/ms-playwright/<browser-version>/...
浏览器查找: playwright 优先读 PLAYWRIGHT_BROWSERS_PATH 环境变量。
"""

import os
import sys

_MEIPASS = getattr(sys, "_MEIPASS", None)
if _MEIPASS:
    _browsers = os.path.join(_MEIPASS, "ms-playwright")
    if os.path.isdir(_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _browsers
    # 容器/无 user namespace 环境: Chromium 自身 sandbox 关闭
    os.environ.setdefault("VEYA_BROWSER_NO_SANDBOX", "1")
