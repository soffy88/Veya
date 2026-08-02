"""
layer4.tools — Tool Registry & Adapters
=========================================
把 oprim/oskill/omodul 包成带 JSON schema 的 LLM 可调工具。

InjectionKind=layer4（§8.3 SPEC v2.1）：
  这一层是 oservice 引擎的注入点实现，不入 3O 主库。
  每个 Adapter 包装一个 oprim/oskill，持有 callable + JSON schema。
"""
from __future__ import annotations

import json
import sys
import os
from dataclasses import dataclass
from pathlib import Path
import asyncio
import inspect
from typing import Any, Callable

# 路径注入（layer4 依赖 oprim/oskill/omodul）
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
for _pkg in ["oprim", "oskill", "omodul"]:
    _p = os.path.join(_BASE, _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# ToolAdapter — 单个工具适配器
# ---------------------------------------------------------------------------

@dataclass
class ToolAdapter:
    """单个 LLM 可调工具的完整描述。

    readonly=True 的工具在 plan 模式下可用。
    callable 接受 dict 返回 Any（可 async）。
    """
    name: str
    description: str
    input_schema: dict
    callable: Callable
    readonly: bool = False
    category: str = "general"   # "file" | "shell" | "git" | "web" | "lsp" | "mcp" | "agent"

    def to_llm_schema(self) -> dict:
        """转成 LLM tools 参数格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """工具注册表：注册/查找/按模式过滤。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolAdapter] = {}

    def register(self, adapter: ToolAdapter) -> None:
        self._tools[adapter.name] = adapter

    def get(self, name: str) -> ToolAdapter | None:
        return self._tools.get(name)

    def all(self) -> list[ToolAdapter]:
        return list(self._tools.values())

    def readonly_only(self) -> list[ToolAdapter]:
        return [t for t in self._tools.values() if t.readonly]

    def by_category(self, category: str) -> list[ToolAdapter]:
        return [t for t in self._tools.values() if t.category == category]

    def filter(self, *, allowed: list[str] | None = None,
               denied: list[str] | None = None) -> list[ToolAdapter]:
        import fnmatch
        result = []
        for t in self._tools.values():
            if denied and any(fnmatch.fnmatch(t.name, p) for p in denied):
                continue
            if allowed and not any(fnmatch.fnmatch(t.name, p) for p in allowed):
                continue
            result.append(t)
        return result

    def schemas(self, tools: list[ToolAdapter] | None = None) -> list[dict]:
        src = tools or list(self._tools.values())
        return [t.to_llm_schema() for t in src]

    def __len__(self) -> int:
        return len(self._tools)


def build_tool_schemas(tools: list[ToolAdapter]) -> list[dict]:
    """把 ToolAdapter 列表转成 LLM tools 参数格式（纯计算）。"""
    return [t.to_llm_schema() for t in tools]


# ---------------------------------------------------------------------------
# 内部工具调用工厂
# ---------------------------------------------------------------------------

async def _call(adapter: ToolAdapter, inp: dict) -> Any:
    """统一调用 adapter.callable，支持 sync/async。"""
    if inspect.iscoroutinefunction(adapter.callable):  # pragma: no cover
        return await adapter.callable(inp)  # pragma: no cover
    return await asyncio.to_thread(adapter.callable, inp)  # pragma: no cover


# ---------------------------------------------------------------------------
# File Adapters
# ---------------------------------------------------------------------------

def _make_file_read() -> ToolAdapter:
    try:
        from oprim.fs import file_read
        from oprim._exceptions import FileOprimError
    except ImportError:  # pragma: no cover
        def file_read(p, **kw): return Path(p).read_text(errors="replace")  # type: ignore  # pragma: no cover
        class FileOprimError(Exception):  # type: ignore  # pragma: no cover
            pass  # pragma: no cover

    async def fn(inp: dict) -> dict:
        try:
            content = file_read(
                inp["path"],
                start=inp.get("start_line"),
                end=inp.get("end_line"),
            )
            return {"content": content, "path": inp["path"]}
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="file_read",
        description="Read file contents, optionally specifying line range.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "start_line": {"type": "integer", "description": "Start line (0-based, inclusive)"},
                "end_line": {"type": "integer", "description": "End line (0-based, exclusive)"},
            },
            "required": ["path"],
        },
        callable=fn,
        readonly=True,
        category="file",
    )


def _make_file_write() -> ToolAdapter:
    try:
        from oprim.fs import file_write
    except ImportError:  # pragma: no cover
        def file_write(p, *, content, **kw):  # type: ignore  # pragma: no cover
            Path(p).write_text(content)  # pragma: no cover
            return Path(p)  # pragma: no cover

    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            path = file_write(inp["path"], content=inp["content"])  # pragma: no cover
            return {"written": str(path), "bytes": len(inp["content"])}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="file_write",
        description="Write content to a file (creates parent dirs automatically).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        callable=fn,
        readonly=False,
        category="file",
    )


def _make_glob_match() -> ToolAdapter:
    try:
        from oprim.fs import glob_match
    except ImportError:  # pragma: no cover
        def glob_match(pat, *, root, **kw): return list(Path(root).glob(pat))  # type: ignore  # pragma: no cover

    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            matches = glob_match(  # pragma: no cover
                inp["pattern"],
                root=inp.get("root", "."),
                respect_gitignore=inp.get("respect_gitignore", True),
            )
            return {"matches": [str(p) for p in matches[:200]]}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="glob_match",
        description="Find files matching a glob pattern.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                "root": {"type": "string", "description": "Root directory (default: cwd)"},
                "respect_gitignore": {"type": "boolean", "default": True},
            },
            "required": ["pattern"],
        },
        callable=fn,
        readonly=True,
        category="file",
    )


def _make_bash_exec(*, permission_gate=None) -> ToolAdapter:
    try:
        from oprim.shell import bash_exec
    except ImportError:  # pragma: no cover
        class bash_exec:  # type: ignore  # pragma: no cover
            def __init__(self, cmd, **kw):  # pragma: no cover
                self.stdout = ""  # pragma: no cover
                self.stderr = "bash not available"  # pragma: no cover
                self.code = 1  # pragma: no cover
                self.ok = False  # pragma: no cover

    async def fn(inp: dict) -> dict:
        cmd = inp.get("command", "")  # pragma: no cover
        if permission_gate:  # pragma: no cover
            decision = permission_gate({"name": "bash_exec", "input": inp})  # pragma: no cover
            if decision == "deny":  # pragma: no cover
                return {"error": "bash_exec denied by permission policy"}  # pragma: no cover
        try:  # pragma: no cover
            result = bash_exec(  # pragma: no cover
                cmd,
                cwd=inp.get("cwd"),
                timeout=inp.get("timeout", 120),
            )
            return {  # pragma: no cover
                "stdout": result.stdout[:8000],
                "stderr": result.stderr[:2000],
                "exit_code": result.code,
                "ok": result.ok,
            }
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="bash_exec",
        description="Execute a shell command and return stdout/stderr/exit_code.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
            },
            "required": ["command"],
        },
        callable=fn,
        readonly=False,
        category="shell",
    )


def _make_ripgrep() -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            from oprim.shell import bash_exec  # pragma: no cover
            pattern = inp["pattern"]  # pragma: no cover
            root = inp.get("root", ".")  # pragma: no cover
            glob = inp.get("glob", "")  # pragma: no cover
            glob_flag = f"--glob '{glob}'" if glob else ""  # pragma: no cover
            cmd = f"rg --json {glob_flag} '{pattern}' '{root}' 2>/dev/null | head -200"  # pragma: no cover
            result = bash_exec(cmd)  # pragma: no cover
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]  # pragma: no cover
            matches = []  # pragma: no cover
            for line in lines[:100]:  # pragma: no cover
                try:  # pragma: no cover
                    obj = json.loads(line)  # pragma: no cover
                    if obj.get("type") == "match":  # pragma: no cover
                        d = obj["data"]  # pragma: no cover
                        matches.append({  # pragma: no cover
                            "path": d["path"]["text"],
                            "line": d["line_number"],
                            "text": d["lines"]["text"].rstrip(),
                        })
                except Exception:  # pragma: no cover
                    pass  # pragma: no cover
            return {"matches": matches, "count": len(matches)}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="ripgrep_search",
        description="Search for a pattern across files using ripgrep.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex)"},
                "root": {"type": "string", "description": "Root directory"},
                "glob": {"type": "string", "description": "File glob filter, e.g. '*.py'"},
            },
            "required": ["pattern"],
        },
        callable=fn,
        readonly=True,
        category="file",
    )


# ---------------------------------------------------------------------------
# Git Adapters
# ---------------------------------------------------------------------------

def _make_git_status() -> ToolAdapter:
    try:
        from oprim.git import git_status
    except ImportError:  # pragma: no cover
        def git_status(*, repo): return []  # type: ignore  # pragma: no cover

    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            statuses = git_status(repo=inp.get("repo", "."))  # pragma: no cover
            return {"files": [{"path": s.path, "index": s.index, "worktree": s.worktree}  # pragma: no cover
                               for s in statuses]}
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="git_status",
        description="Get the current git working tree status.",
        input_schema={
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "Repo root (default: cwd)"}},
        },
        callable=fn, readonly=True, category="git",
    )


def _make_git_diff() -> ToolAdapter:
    try:
        from oprim.git import git_diff
    except ImportError:  # pragma: no cover
        def git_diff(*, repo, **kw): return ""  # type: ignore  # pragma: no cover

    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            diff = git_diff(  # pragma: no cover
                repo=inp.get("repo", "."),
                staged=inp.get("staged", False),
                paths=inp.get("paths"),
            )
            return {"diff": diff[:16000], "truncated": len(diff) > 16000}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="git_diff",
        description="Get git diff (unstaged by default, or --staged).",
        input_schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "staged": {"type": "boolean", "default": False},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
        },
        callable=fn, readonly=True, category="git",
    )


# ---------------------------------------------------------------------------
# Web Adapters
# ---------------------------------------------------------------------------

def _make_web_fetch() -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            from oprim.network import http_fetch  # pragma: no cover
            resp = await http_fetch(  # pragma: no cover
                inp["url"],
                method=inp.get("method", "GET"),
                timeout=inp.get("timeout", 30),
            )
            return {"status_code": resp.status_code, "text": resp.text[:16000], "ok": resp.ok}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="web_fetch",
        description="Fetch a URL and return the response text.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "method": {"type": "string", "default": "GET"},
                "timeout": {"type": "number", "default": 30},
            },
            "required": ["url"],
        },
        callable=fn, readonly=True, category="web",
    )


def _make_web_search() -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            # web_search: fallback to bash curl/lynx
            from oprim.shell import bash_exec  # pragma: no cover
            q = inp["query"].replace("'", "")  # pragma: no cover
            r = bash_exec(f"curl -s 'https://api.duckduckgo.com/?q={q}&format=json' 2>/dev/null")  # pragma: no cover
            if r.ok and r.stdout:  # pragma: no cover
                data = json.loads(r.stdout)  # pragma: no cover
                results = [{"title": t.get("Text",""), "url": t.get("FirstURL",""), "snippet": ""}  # pragma: no cover
                           for t in data.get("RelatedTopics", [])[:5] if isinstance(t, dict)]
                return {"results": results}  # pragma: no cover
            return {"results": [], "note": "web search unavailable"}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e), "results": []}  # pragma: no cover

    return ToolAdapter(
        name="web_search",
        description="Search the web and return top results.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        callable=fn, readonly=True, category="web",
    )


# ---------------------------------------------------------------------------
# LSP Adapter (dynamic — requires live server handle)
# ---------------------------------------------------------------------------

def _make_lsp_diagnostics(server_factory=None) -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        if server_factory is None:  # pragma: no cover
            return {"diagnostics": [], "note": "LSP server not configured"}  # pragma: no cover
        try:  # pragma: no cover
            from oprim.lsp import lsp_diagnostics  # pragma: no cover
            server = server_factory(inp.get("path", ""))  # pragma: no cover
            diags = await lsp_diagnostics(inp["path"], server=server)  # pragma: no cover
            return {"diagnostics": [  # pragma: no cover
                {"line": d.line, "severity": d.severity_name,
                 "message": d.message, "source": d.source}
                for d in diags
            ]}
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="lsp_diagnostics",
        description="Get LSP diagnostics (errors/warnings) for a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
        callable=fn, readonly=True, category="lsp",
    )


# ---------------------------------------------------------------------------
# MCP Adapter (dynamic — wraps mcp_call_tool for each discovered tool)
# ---------------------------------------------------------------------------

def make_mcp_tool_adapter(tool_spec: dict, mcp_client: Any) -> ToolAdapter:
    """为单个 MCP 工具动态创建 ToolAdapter。"""
    tool_name = tool_spec.get("name", "mcp_tool")

    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            from oprim.mcp import mcp_call_tool  # pragma: no cover
            result = await mcp_call_tool(tool_name, arguments=inp, client=mcp_client)  # pragma: no cover
            return result  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name=f"mcp_{tool_name}",
        description=f"[MCP] {tool_spec.get('description', tool_name)}",
        input_schema=tool_spec.get("inputSchema", {"type": "object", "properties": {}}),
        callable=fn,
        readonly=False,
        category="mcp",
    )


# ---------------------------------------------------------------------------
# TodoWrite Adapter
# ---------------------------------------------------------------------------

def _make_todo_write(todo_tracker=None) -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        try:  # pragma: no cover
            from oskill.tooling import plan_to_todos  # pragma: no cover
            todos = plan_to_todos(inp.get("todos", []))  # pragma: no cover
            if todo_tracker:  # pragma: no cover
                todo_tracker(todos)  # pragma: no cover
            return {"todos": [{"id": t.id, "content": t.content,  # pragma: no cover
                                "status": t.status, "priority": t.priority}
                               for t in todos]}
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="todo_write",
        description="Write/update the task todo list.",
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "status": {"type": "string"},
                            "priority": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                }
            },
            "required": ["todos"],
        },
        callable=fn, readonly=False, category="general",
    )


# ---------------------------------------------------------------------------
# SubagentAdapter
# ---------------------------------------------------------------------------

def _make_subagent(subagent_loader=None) -> ToolAdapter:
    async def fn(inp: dict) -> dict:
        name = inp.get("name", "")  # pragma: no cover
        task = inp.get("task", "")  # pragma: no cover
        if not subagent_loader:  # pragma: no cover
            return {"error": "subagent_loader not configured"}  # pragma: no cover
        try:  # pragma: no cover
            from omodul.run_subagent import (  # pragma: no cover
                SubagentConfig, SubagentInput, run_subagent,
            )
            import tempfile  # pragma: no cover
            defn = subagent_loader(name)  # pragma: no cover
            if defn is None:  # pragma: no cover
                return {"error": f"subagent '{name}' not found"}  # pragma: no cover
            cfg = SubagentConfig()  # pragma: no cover
            si = SubagentInput(task=task, subagent_def=defn,  # pragma: no cover
                               caller=inp.get("_caller"))
            result = await run_subagent(cfg, si, Path(tempfile.mkdtemp()))  # pragma: no cover
            return {"summary": result.get("summary", ""), "status": result["status"]}  # pragma: no cover
        except Exception as e:  # pragma: no cover
            return {"error": str(e)}  # pragma: no cover

    return ToolAdapter(
        name="dispatch_subagent",
        description="Dispatch a task to a named subagent.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Subagent name"},
                "task": {"type": "string", "description": "Task description for the subagent"},
            },
            "required": ["name", "task"],
        },
        callable=fn, readonly=False, category="agent",
    )


# ---------------------------------------------------------------------------
# Registry factory — 默认工具集
# ---------------------------------------------------------------------------

def build_default_registry(
    *,
    permission_gate=None,
    server_factory=None,
    todo_tracker=None,
    subagent_loader=None,
) -> ToolRegistry:
    """构建包含所有标准工具的默认 ToolRegistry。"""
    reg = ToolRegistry()
    for adapter in [
        _make_file_read(),
        _make_file_write(),
        _make_glob_match(),
        _make_bash_exec(permission_gate=permission_gate),
        _make_ripgrep(),
        _make_git_status(),
        _make_git_diff(),
        _make_web_fetch(),
        _make_web_search(),
        _make_lsp_diagnostics(server_factory),
        _make_todo_write(todo_tracker),
        _make_subagent(subagent_loader),
    ]:
        reg.register(adapter)
    return reg
