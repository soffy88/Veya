"""server.skill_scan — 技能代码静态安全扫描 (加载期关口)。

SkillHub 执行 python 技能时会动态 ``exec_module`` 其 entrypoint —— 模块**顶层代码
一并执行**, 第三方/不可信技能等于任意代码执行入口。挂载前对源码做一次 AST 静态
扫描, 标出危险调用面 (命令执行 / 动态代码 / 动态 import / 网络外联 / 破坏性文件
操作 / 不安全反序列化), 供宿主按信任级别决定: 记录风险 / 告警 / 严格模式拒载。

纯函数, 无 I/O (源码由调用方读入)。检出是**保守启发式** —— 命中不代表恶意
(coding 技能合法用 subprocess 很常见), 但让风险从"不可见"变"可见 + 可拦"。
参照 K-Dense scientific-agent-skills 的 skill scanner 范式内化 (skill 可"指示
agent 跑任意代码", 装载期须过一道扫描)。
"""

from __future__ import annotations

import ast
from typing import Any

# 危险调用 (点分全名 → (严重度, 类别))。severity: high=直接 RCE 面, medium=需留意。
_DANGER_CALLS: dict[str, tuple[str, str]] = {
    "os.system": ("high", "command-exec"),
    "os.popen": ("high", "command-exec"),
    "os.execv": ("high", "command-exec"),
    "os.execve": ("high", "command-exec"),
    "os.spawnv": ("high", "command-exec"),
    "subprocess.run": ("high", "command-exec"),
    "subprocess.Popen": ("high", "command-exec"),
    "subprocess.call": ("high", "command-exec"),
    "subprocess.check_call": ("high", "command-exec"),
    "subprocess.check_output": ("high", "command-exec"),
    "subprocess.getoutput": ("high", "command-exec"),
    "pty.spawn": ("high", "command-exec"),
    "eval": ("high", "dynamic-code"),
    "exec": ("high", "dynamic-code"),
    "compile": ("medium", "dynamic-code"),
    "__import__": ("medium", "dynamic-import"),
    "importlib.import_module": ("medium", "dynamic-import"),
    "pickle.load": ("high", "unsafe-deserialize"),
    "pickle.loads": ("high", "unsafe-deserialize"),
    "marshal.load": ("high", "unsafe-deserialize"),
    "marshal.loads": ("high", "unsafe-deserialize"),
    "os.remove": ("medium", "fs-destructive"),
    "os.unlink": ("medium", "fs-destructive"),
    "os.rmdir": ("medium", "fs-destructive"),
    "shutil.rmtree": ("high", "fs-destructive"),
}

# 危险模块 (import 顶层名 → (严重度, 类别))。外联/远控面。
_DANGER_IMPORTS: dict[str, tuple[str, str]] = {
    "socket": ("medium", "network"),
    "urllib": ("medium", "network"),
    "http": ("medium", "network"),
    "requests": ("medium", "network"),
    "httpx": ("medium", "network"),
    "aiohttp": ("medium", "network"),
    "ftplib": ("medium", "network"),
    "smtplib": ("medium", "network"),
    "telnetlib": ("high", "network"),
}

_SEVERITY_RANK = {"high": 2, "medium": 1}


def _dotted_name(node: ast.AST) -> str:
    """从 Name / Attribute 链取点分全名 (os.system / subprocess.run); 取不到 → ''。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return ""  # 非静态可解析的调用目标 (如 foo().bar) — 跳过
    return ".".join(reversed(parts))


def _open_write_mode(call: ast.Call) -> bool:
    """open(path, 'w'/'a'/'x'...) 写模式? (位置第2参或 mode= 关键字含 w/a/x/+)。"""
    mode_node: ast.AST | None = None
    if len(call.args) >= 2:
        mode_node = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(c in mode_node.value for c in "wax+")
    return False


def scan_skill_source(code: str, *, filename: str = "<skill>") -> list[dict[str, Any]]:
    """AST 静态扫描技能源码, 返回 findings。

    Finding: ``{"severity", "category", "line", "detail"}``。空列表 = 未检出危险面。
    语法错误本身算 high (无法信任的代码不应被动态执行)。
    """
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError as exc:
        return [
            {
                "severity": "high",
                "category": "parse-error",
                "line": exc.lineno or 0,
                "detail": f"源码语法错误, 无法静态审查: {exc.msg}",
            }
        ]

    findings: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        # ── 危险调用 ──
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in _DANGER_CALLS:
                severity, category = _DANGER_CALLS[name]
                findings.append(
                    {
                        "severity": severity,
                        "category": category,
                        "line": getattr(node, "lineno", 0),
                        "detail": f"调用 {name}()",
                    }
                )
            elif name == "open" and _open_write_mode(node):
                findings.append(
                    {
                        "severity": "medium",
                        "category": "fs-destructive",
                        "line": getattr(node, "lineno", 0),
                        "detail": "open() 写模式 (可覆盖/写文件)",
                    }
                )
        # ── 危险 import ──
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _DANGER_IMPORTS:
                    severity, category = _DANGER_IMPORTS[top]
                    findings.append(
                        {
                            "severity": severity,
                            "category": category,
                            "line": getattr(node, "lineno", 0),
                            "detail": f"import {alias.name}",
                        }
                    )
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in _DANGER_IMPORTS:
                severity, category = _DANGER_IMPORTS[top]
                findings.append(
                    {
                        "severity": severity,
                        "category": category,
                        "line": getattr(node, "lineno", 0),
                        "detail": f"from {node.module} import ...",
                    }
                )

    findings.sort(key=lambda f: (-_SEVERITY_RANK.get(f["severity"], 0), f["line"]))
    return findings


def summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """把 findings 聚合成风险摘要 (供 skill 元数据/审计视图/拒载策略)。"""
    high = sum(1 for f in findings if f["severity"] == "high")
    medium = sum(1 for f in findings if f["severity"] == "medium")
    categories = sorted({f["category"] for f in findings})
    max_severity = "high" if high else ("medium" if medium else "none")
    return {
        "max_severity": max_severity,
        "high": high,
        "medium": medium,
        "categories": categories,
        "count": len(findings),
    }
