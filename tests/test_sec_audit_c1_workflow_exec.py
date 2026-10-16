"""SEC audit regression tests — C1: 未鉴权 exec() 任意代码执行。

审计 C1: ``veya/server/app.py`` 的 ``POST /api/v1/agent/workflow/run`` 把请求体
``events[].body`` 拼进 ``async def _inner`` 源码并 ``exec()`` 执行, 路由无鉴权。
修复: 直接删除该端点 (仓库内无任何调用方)。

回归断言: 该路径必须不再注册为路由; ``veya/server/app.py`` 源码中不得再有
``exec(`` 拼接请求数据的写法。

Run: .venv/bin/python -m pytest tests/test_sec_audit_c1_workflow_exec.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SRC = REPO_ROOT / "veya" / "server" / "app.py"


def test_workflow_run_source_removed() -> None:
    src = APP_SRC.read_text(encoding="utf-8")
    assert "/api/v1/agent/workflow/run" not in src, "C1: exec() 端点必须已删除"


def test_no_exec_of_request_data() -> None:
    """``exec(`` 不得出现在该文件 (C1 的利用原语)。"""
    src = APP_SRC.read_text(encoding="utf-8")
    assert not re.search(r"\bexec\s*\(", src), "C1: app.py 不得再 exec() 请求数据"


def test_route_not_registered() -> None:
    """对 app 实例断言: 未注册 workflow/run 路由。"""
    import server.app  # noqa: F401 — 确保根 app 可 import
    from veya.server.app import create_app

    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/agent/workflow/run" not in paths
