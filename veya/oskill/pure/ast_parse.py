"""3O-PURE — ast_parse: 代码结构合法性检查。

纯函数包装 ``ast``（标准库，无副作用）：
- ``syntax_check``: 语法合法性 → (ok, 错误信息)；
- ``find_definitions``: 函数/类/导入定义清单（name/kind/行号）；
- ``structure_summary``: 结构统计（函数数/类数/导入数/顶层定义）。
用于 omodul_evidence_refine：模型生成复杂代码时先静态检查再执行。
"""

from __future__ import annotations

import ast


def syntax_check(code: str) -> tuple[bool, str]:
    """语法合法性检查。返回 (ok, error)；ok=True 时 error 为空串。"""
    if not isinstance(code, str):
        return False, "code 必须是字符串"
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as exc:
        line = exc.lineno or 0
        offset = exc.offset or 0
        return (
            False,
            f"语法错误 L{line}:{offset} — {exc.msg} ({exc.text and exc.text.strip() or ''})",
        )


def find_definitions(code: str) -> list[dict]:
    """提取顶层定义清单: [{name, kind, lineno, end_lineno}]。kind ∈
    function/async_function/class/import/import_from/constant/其他。"""
    if not syntax_check(code)[0]:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = (
                "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
                if isinstance(node, ast.FunctionDef)
                else "class"
            )
            out.append(
                {
                    "name": node.name,
                    "kind": kind,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", node.lineno),
                }
            )
        elif isinstance(node, ast.Import):
            out.append(
                {
                    "name": ", ".join(a.name for a in node.names),
                    "kind": "import",
                    "lineno": node.lineno,
                    "end_lineno": node.lineno,
                }
            )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out.append(
                {
                    "name": f"from {mod} import ...",
                    "kind": "import_from",
                    "lineno": node.lineno,
                    "end_lineno": node.lineno,
                }
            )
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.append(
                        {
                            "name": tgt.id,
                            "kind": "assign",
                            "lineno": node.lineno,
                            "end_lineno": node.lineno,
                        }
                    )
    return out


def structure_summary(code: str) -> dict:
    """结构统计: {functions, classes, imports, top_level_assigns, has_syntax_error}。"""
    if not syntax_check(code)[0]:
        return {
            "functions": 0,
            "classes": 0,
            "imports": 0,
            "top_level_assigns": 0,
            "has_syntax_error": True,
        }
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "functions": 0,
            "classes": 0,
            "imports": 0,
            "top_level_assigns": 0,
            "has_syntax_error": True,
        }
    functions = classes = imports = assigns = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions += 1
        elif isinstance(node, ast.ClassDef):
            classes += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports += 1
        elif isinstance(node, ast.Assign):
            assigns += 1
    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "top_level_assigns": assigns,
        "has_syntax_error": False,
    }


def forbidden_imports(code: str, forbidden: tuple[str, ...]) -> list[str]:
    """检查代码是否 import 了禁止模块（返回命中的模块名列表，空 = 干净）。"""
    if not syntax_check(code)[0]:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in forbidden and top not in hits:
                    hits.append(top)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top in forbidden and top not in hits:
                hits.append(top)
    return hits


__all__ = [
    "find_definitions",
    "forbidden_imports",
    "structure_summary",
    "syntax_check",
]
