#!/usr/bin/env python3
"""3O 附录 B: check_obase_no_reverse_dep.py

强制 §7.4 依赖方向：obase → 3O 任何层 ❌。

检查 veya/obase/ 内所有模块的 import：
- 允许：标准库、第三方（site-packages）、veya.errors、veya.compat、veya.obase 内部
- 禁止：veya.* 业务层（tools/tools 等）、server.*、agents.*、config.*、cli.*、commands.*
        （§7.4 落地指引：类型归 obase、算法归 oprim；obase 反向 import = 违规）

用法：python scripts/check_obase_no_reverse_dep.py [root]
退出码：0 = 通过；1 = 发现违规（列出明细）
"""

from __future__ import annotations

import ast
import pathlib
import sys


def _iter_py_files(root: pathlib.Path):
    return sorted(root.rglob("*.py"))


def check_file(path: pathlib.Path) -> list[str]:
    """返回该文件违反依赖方向的 import 描述列表（空 = 合规）。"""
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if _is_forbidden(name):
                    violations.append(f"{path}: import {name} (obase 不得反向依赖业务层)")
        elif isinstance(node, ast.ImportFrom) and node.module:
            name = node.module
            if _is_forbidden(name):
                violations.append(f"{path}: from {name} import ... (obase 不得反向依赖业务层)")
    return violations


# veya.* 业务层前缀（§7.4 禁止 obase → 3O/业务）
_FORBIDDEN_VEYA = (
    "veya.tools",
    "veya.agent_collaboration",
    "veya.autonomous_agent",
    "veya.collaboration",
    "veya.context",
    "veya.cross_language",
    "veya.integrations",
    "veya.intent",
    "veya.multimodal",
    "veya.sandbox",
    "veya.semantic_search",
    "veya.visualization",
    "veya.advanced_visualization",
    "veya.streaming",
    "veya.cache",
    "veya.performance",
    "veya.ast",
    "veya.code_review",
    "veya.logging",
    "veya.utils",
)
# 项目服务层 / 其他层
_FORBIDDEN_TOPS = (
    "server",
    "agents",
    "config",
    "cli",
    "commands",
    "session",
    "streaming",
    "registries",
    "hooks",
)


def _is_forbidden(import_name: str) -> bool:
    base = import_name.split(".")[0]
    if base in _FORBIDDEN_TOPS:
        return True
    if import_name == "veya" or import_name.startswith("veya."):
        if import_name in _FORBIDDEN_VEYA:
            return True
        # veya.platform 是唯一的 3O assembly choke point，不是业务层；允许
        # compat adapter 通过它惰性解析 canonical obase registry。
        allowed = ("veya.errors", "veya.compat", "veya.obase", "veya.llm", "veya.platform")
        if not any(import_name == a or import_name.startswith(a + ".") for a in allowed):
            return True
    return False


def main(root: str = ".") -> int:
    obase_dir = pathlib.Path(root) / "veya" / "obase"
    if not obase_dir.exists():
        print(f"[SKIP] {obase_dir} not found")
        return 0
    violations: list[str] = []
    for f in _iter_py_files(obase_dir):
        if "__pycache__" in str(f):
            continue
        violations.extend(check_file(f))
    if violations:
        print(f"[FAIL] {len(violations)} reverse-dependency violation(s):")
        for v in violations:
            print("  -", v)
        return 1
    print(f"[OK] obase dependency direction clean ({len(list(_iter_py_files(obase_dir)))} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
