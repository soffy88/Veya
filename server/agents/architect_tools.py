"""Veya Genesis: ThreeOPhysicalTools — Genesis 的物理执行层。

在 3O 主库 (platform/3O) 上执行物理动作:
- forge_element: 锻造元素 (3O 纯度校验 + 写文件)
- run_in_sandbox: 3O 隔离沙箱验证 (网络封锁 / 内存 / 时间限制)
- search_library / list_layer / read_element: 摸清库内现状
- git_status / git_commit: 推库

3O 范式 (THE 3O PARADIGM):
- oprim/oskill: Pure Math/Logic ONLY (禁止 I/O / 网络 / 进程模块)
- obase: I/O and Resources
- omodul: Transaction Managers
- oservi: 引擎装配层

纯度校验只约束 oprim/oskill 新锻造元素;存量库不受影响。
若确实需要 I/O,必须显式传 allow_impure=True(由 Genesis 自证理由)。
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

from veya.sandbox import SandboxConfig, create_safe_executor

# 3O 主库层(对应 platform/3O 下的子模块)
LAYERS: tuple[str, ...] = ("oprim", "oskill", "omodul", "obase", "oservi")
# 纯数学层: 只允许 Pure Math/Logic
_MATH_LAYERS: frozenset[str] = frozenset({"oprim", "oskill"})
# 允许的第三方/科学计算库(即使不是纯 stdlib 数学,也是数值计算必需品)
_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "numpy",
        "pandas",
        "scipy",
        "sklearn",
        "statsmodels",
        "pydantic",
        "typing",
        "dataclasses",
        "enum",
        "math",
        "cmath",
        "statistics",
        "random",
        "decimal",
        "fractions",
        "numbers",
        "itertools",
        "functools",
        "collections",
        "re",
        "json",
        "datetime",
        "time",
        "warnings",
        "abc",
        "dataclass",
        "copy",
        "operator",
        "heapq",
        "bisect",
        "array",
        "struct",
        "types",
        "contextlib",
        "string",
        "unicodedata",
        "oskill",
        "oprim",
        "omodul",
        "obase",
    }
)
# 纯数学层禁止的 I/O / 网络 / 进程模块
_IMPURE_MODULE_PREFIXES: tuple[str, ...] = (
    "os",
    "sys",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "fnmatch",
    "importlib",
    "ctypes",
    "cffi",
    "multiprocessing",
    "threading",
    "asyncio",
    "aiohttp",
    "httpx",
    "http",
    "ftplib",
    "smtplib",
    "pickle",
    "shelve",
    "sqlite3",
    "signal",
    "fcntl",
    "pwd",
    "grp",
    "platform",
    "logging",
)


class ToolExecutionError(RuntimeError):
    """工具执行失败 → 触发 Genesis 的反思与经验记录回路。"""


def _is_impure_module(module: str) -> bool:
    """模块(或其祖先包)是否属于 I/O / 网络 / 进程家族。"""
    top = module.split(".")[0]
    return top in _IMPURE_MODULE_PREFIXES or any(
        module.startswith(f"{p}.") for p in _IMPURE_MODULE_PREFIXES
    )


def validate_3o_purity(layer: str, code: str) -> tuple[bool, str]:
    """3O 纯度校验: oprim/oskill 只允许 Pure Math/Logic。

    返回 (ok, reason)。校验失败只返回失败文本(不抛异常),
    让模型读到原因后自纠 —— 这正是它形成"经验教训"的素材。
    """
    if layer not in _MATH_LAYERS:
        return True, ""  # obase(IO)/omodul(事务)/oservi(装配) 不受纯度约束

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"代码语法错误,无法通过 3O 纯度校验: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_impure_module(alias.name):
                    return (
                        False,
                        f"3O 纯度违规: oprim/oskill 层禁止引入 '{alias.name}' "
                        f"(Pure Math/Logic ONLY)。如需 I/O 请改放 obase 层。",
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_impure_module(module):
                return (
                    False,
                    f"3O 纯度违规: oprim/oskill 层禁止引入 '{module}' "
                    f"(Pure Math/Logic ONLY)。如需 I/O 请改放 obase 层。",
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "open",
            "eval",
            "exec",
            "__import__",
        }:
            return (
                False,
                f"3O 纯度违规: oprim/oskill 层禁止调用 {node.func.id}() "
                f"(Pure Math/Logic ONLY)。",
            )
    return True, ""


def _tool_schema(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


class ThreeOPhysicalTools:
    """Genesis 的物理执行层: 全部动作限定在 3O 主库根目录内。"""

    def __init__(
        self,
        library_root: str | Path,
        *,
        git_enabled: bool = True,
        sandbox_timeout: float = 30.0,
        sandbox_memory_limit: int = 1024 * 1024 * 1024,
    ):
        self.library_root = Path(library_root).resolve()
        self.git_enabled = git_enabled
        self.sandbox_timeout = sandbox_timeout
        self.sandbox_memory_limit = sandbox_memory_limit
        # 单线程 BLAS: OpenBLAS 启动时预留大量虚拟内存, 与沙箱 RLIMIT_AS 冲突
        self.sandbox_env_extra = {
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        self._tool_registry: dict[str, Any] = {
            "forge_element": self.forge_element,
            "run_in_sandbox": self.run_in_sandbox,
            "search_library": self.search_library,
            "list_layer": self.list_layer,
            "read_element": self.read_element,
            "git_status": self.git_status,
            "git_commit": self.git_commit,
        }

    # ── 工具 schema ──────────────────────────────────────────────────
    def get_tool_schemas(self) -> list[dict]:
        return [
            _tool_schema(
                "forge_element",
                "Forge a new 3O element: write code into a layer (oprim/oskill must be Pure Math/Logic; obase for I/O; omodul for transactions). Returns 成功/失败 with reason.",
                {
                    "layer": {"type": "string", "description": "one of " + ", ".join(LAYERS)},
                    "element_name": {
                        "type": "string",
                        "description": "element path relative to the layer root, e.g. 'factor/dual_ma.py'",
                    },
                    "code": {"type": "string", "description": "full python source of the element"},
                    "allow_impure": {
                        "type": "boolean",
                        "description": "bypass purity check for oprim/oskill (must have a strong reason)",
                    },
                },
                ("layer", "element_name", "code"),
            ),
            _tool_schema(
                "run_in_sandbox",
                "Run python code inside the 3O isolated sandbox (network blocked, memory/time limited). Non-zero exit is reported back as a failure.",
                {"code": {"type": "string", "description": "python source to execute"}},
                ("code",),
            ),
            _tool_schema(
                "search_library",
                "Search the 3O master library for existing implementations before forging anything new.",
                {
                    "query": {"type": "string", "description": "regex or keyword"},
                    "layer": {"type": "string", "description": "restrict to a layer (optional)"},
                },
                ("query",),
            ),
            _tool_schema(
                "list_layer",
                "List python files in a 3O layer.",
                {"layer": {"type": "string", "description": "one of " + ", ".join(LAYERS)}},
                ("layer",),
            ),
            _tool_schema(
                "read_element",
                "Read an existing element from a 3O layer.",
                {
                    "layer": {"type": "string"},
                    "element_name": {"type": "string", "description": "path relative to layer root"},
                },
                ("layer", "element_name"),
            ),
            _tool_schema(
                "git_status", "Show git status of the 3O master library.", {}
            ),
            _tool_schema(
                "git_commit",
                "Commit all pending changes in the 3O master library with a message.",
                {"message": {"type": "string", "description": "commit message"}},
                ("message",),
            ),
        ]

    def execute(self, tool_name: str, **tool_args: Any) -> str:
        """动态派发(Genesis Agent 调用入口)。"""
        fn = self._tool_registry.get(tool_name)
        if fn is None:
            raise ToolExecutionError(f"unknown 3O tool '{tool_name}'")
        return fn(**tool_args)

    # ── 路径安全 ─────────────────────────────────────────────────────
    def _resolve_element(self, layer: str, element_name: str) -> Path:
        if layer not in LAYERS:
            raise ToolExecutionError(f"unknown 3O layer '{layer}'. Available: {', '.join(LAYERS)}")
        layer_root = (self.library_root / layer).resolve()
        target = (layer_root / element_name).resolve()
        if target != layer_root and layer_root not in target.parents:
            raise ToolExecutionError(
                f"path '{layer}/{element_name}' escapes layer root '{layer_root}' — 拒绝越界"
            )
        return target

    def _run_git(self, *args: str) -> str:
        if not self.git_enabled:
            raise ToolExecutionError("git operations disabled")
        proc = subprocess.run(
            ["git", "-C", str(self.library_root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise ToolExecutionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # ── 工具实现 ─────────────────────────────────────────────────────
    def forge_element(self, layer: str, element_name: str, code: str, allow_impure: bool = False) -> str:
        """锻造元素: 纯度校验 → 写文件 → 返回状态(成功/失败均带原因)。"""
        target = self._resolve_element(layer, element_name)
        if layer in _MATH_LAYERS and not allow_impure:
            ok, reason = validate_3o_purity(layer, code)
            if not ok:
                return f"失败：{reason}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"forge_element: 写入失败 {target}: {exc}") from exc
        return f"成功：已锻造元素 {layer}/{element_name} ({len(code)} chars, 纯度校验通过)"

    async def run_in_sandbox(self, code: str) -> str:
        """安全验证: 3O 隔离沙箱(网络封锁 + 内存/时间限制)。"""
        config = SandboxConfig(
            time_limit=self.sandbox_timeout,
            memory_limit=self.sandbox_memory_limit,
            network_blocked=True,
            audit_enabled=True,
            env_extra=self.sandbox_env_extra,
        )
        executor = create_safe_executor(config)
        async with executor:
            result = await executor.run_script(code)
        if result.get("exit_code") != 0:
            raise ToolExecutionError(
                f"exit_code={result.get('exit_code')} ({result.get('duration', 0.0):.2f}s)\n"
                f"stdout:\n{result.get('stdout', '')}\n"
                f"stderr:\n{result.get('stderr', '')}"
            )
        return f"exit_code=0 ({result.get('duration', 0.0):.2f}s)\n{result.get('stdout', '')}"

    def search_library(self, query: str, layer: str | None = None) -> str:
        """在 3O 主库中搜索已有实现(防止重复造轮子)。"""
        import re

        root = self.library_root if layer is None else self.library_root / layer
        if not root.exists():
            return f"layer 不存在: {layer}"
        pattern = re.compile(query, re.IGNORECASE)
        hits = []
        for p in sorted(root.rglob("*.py")):
            if any(part in {"__pycache__", ".git", "tests", "docs"} for part in p.parts):
                continue
            try:
                for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        rel = p.relative_to(self.library_root)
                        hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
                        break
            except OSError:
                continue
            if len(hits) >= 30:
                break
        return "\n".join(hits) if hits else f"库内未找到与 '{query}' 匹配的实现"

    def list_layer(self, layer: str) -> str:
        if layer not in LAYERS:
            raise ToolExecutionError(f"unknown 3O layer '{layer}'. Available: {', '.join(LAYERS)}")
        root = self.library_root / layer
        if not root.exists():
            return f"(layer {layer} 不存在)"
        files = []
        for p in sorted(root.rglob("*.py")):
            if any(part in {"__pycache__", ".git", "tests", "docs"} for part in p.parts):
                continue
            try:
                rel = p.relative_to(self.library_root)
                files.append(f"{rel} ({p.stat().st_size}b)")
            except OSError:
                continue
        return "\n".join(files) if files else f"(layer {layer} 为空)"

    def read_element(self, layer: str, element_name: str) -> str:
        target = self._resolve_element(layer, element_name)
        if not target.exists():
            raise ToolExecutionError(f"元素不存在: {layer}/{element_name}")
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > 12000:
            content = content[:12000] + "\n... [truncated]"
        return f"# {layer}/{element_name}\n{content}"

    def git_status(self) -> str:
        return self._run_git("status", "--short")

    def git_commit(self, message: str) -> str:
        self._run_git("add", "-A")
        out = self._run_git("commit", "-m", message)
        return out or "(nothing to commit)"

    def to_dict(self) -> dict:
        return {"library_root": str(self.library_root), "tools": sorted(self._tool_registry)}


def _schema_json() -> str:
    """调试辅助: 打印全部工具 schema。"""
    return json.dumps(ThreeOPhysicalTools(".").get_tool_schemas(), ensure_ascii=False, indent=2)
