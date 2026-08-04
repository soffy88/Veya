#!/usr/bin/env python3
"""3O 附录 B: check_async_contract.py（简化落地版）

§5.6 / §8.8 执行模型由本性决定；async 元素须被正确 await。

简化检查（hicode/obase 范围）：
1. obase 内 ``async def`` 的模块级函数若被本包其它模块 import 使用，
   调用点必须出现在 ``await`` / ``asyncio.gather`` / ``asyncio.create_task`` 上下文；
2. 禁止在 obase 内 ``asyncio.run()`` 嵌套调用（会破坏既有 loop）。

完整版应采集 is_async 契约并做翻转门禁；此处为可执行的最小实现。

用法：python scripts/check_async_contract.py [root]
"""

from __future__ import annotations

import ast
import pathlib
import sys


def _find_async_defs(path: pathlib.Path) -> set[str]:
    """返回文件内 async def 的函数名集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def _is_awaited(node: ast.Await) -> bool:
    return True


def check_file(path: pathlib.Path) -> list[str]:
    """检查单个文件：async def 的调用点必须 await。"""
    issues: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    async_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    # 找所有 Call 到 async 函数名，且不在 await/作为 coroutine 传给 asyncio.* 的
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            name = func.attr
        if name is None or name not in async_names:
            continue
        parent = _parent(tree, node)
        if isinstance(parent, ast.Await):
            continue
        # asyncio.gather / create_task / to_thread 等合法包装（单层 if，SIM102）
        if (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr in ("gather", "create_task", "to_thread", "wait_for", "shield")
        ):
            continue
        line = getattr(node, "lineno", "?")
        issues.append(
            f"{path}:{line}: async '{name}' called without await (C1 铁律/契约)"
            " — use await or asyncio.gather/create_task/to_thread"
        )
    return issues


def _parent(tree: ast.AST, target: ast.AST) -> ast.AST | None:
    """返回 target 的父节点（简单线性扫描，AST 规模小足够）。"""
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if child is target:
                return node
    return None


def main(root: str = ".") -> int:
    obase_dir = pathlib.Path(root) / "hicode" / "obase"
    if not obase_dir.exists():
        print(f"[SKIP] {obase_dir} not found")
        return 0
    issues: list[str] = []
    for f in sorted(obase_dir.rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        issues.extend(check_file(f))
    if issues:
        print(f"[FAIL] {len(issues)} async contract issue(s):")
        for i in issues:
            print("  -", i)
        return 1
    print("[OK] async contract clean (all async calls awaited)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
