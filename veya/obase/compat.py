"""
veya/compat.py — compatibility shims for legacy `o*` modules

All legacy modules (oprim, omodul, obase, oskill, oservi, oservice)
have been removed from the repo. This module provides minimal shims
so that the coordinator, checkpoint, tools, and routes can import
without NameError/ImportError at runtime.

Shims are intentionally lightweight: they return sensible defaults
or raise NotImplementedError when the feature truly depends on a
missing external service (e.g. LSP, MCP).
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

# ===================================================================
# Checkpoint data structures (replace oprim._make_checkpoint)
# ===================================================================


@dataclass
class RunState:
    """Represents the execution state of a squad/task pipeline."""

    session_id: str
    step: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)


@dataclass
class CheckpointData:
    """Serializable checkpoint payload."""

    session_id: str
    timestamp: float
    version: int = 1
    payload: dict[str, Any] = field(default_factory=dict)


# ===================================================================
# Checkpoint helpers (replace oprim checkpoint functions)
# ===================================================================

_CHECKPOINT_DIR = pathlib.Path.home() / ".veya" / "checkpoints"


def make_checkpoint(run_state: RunState, *, session_id: str | None = None) -> CheckpointData:
    """Create a CheckpointData from a RunState."""
    import time

    sid = session_id or run_state.session_id
    return CheckpointData(
        session_id=sid,
        timestamp=time.time(),
        version=run_state.step,
        payload={
            "data": run_state.data,
            "completed_steps": run_state.completed_steps,
        },
    )


def restore_from_checkpoint(session_id: str) -> CheckpointData | None:
    """Load the latest checkpoint for a session from disk."""
    path = _CHECKPOINT_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return None
    lines = path.read_text().strip().splitlines()
    if not lines:
        return None
    latest = json.loads(lines[-1])
    return CheckpointData(
        session_id=latest["session_id"],
        timestamp=latest["timestamp"],
        version=latest.get("version", 1),
        payload=latest.get("payload", {}),
    )


def checkpoint_to_run_state(ckpt: CheckpointData) -> RunState:
    """Convert CheckpointData → RunState for resume (G13)."""
    payload = ckpt.payload or {}
    return RunState(
        session_id=ckpt.session_id,
        step=ckpt.version,
        data=payload.get("data", {}),
        completed_steps=list(payload.get("completed_steps", [])),
    )


def compute_diff(before: dict, after: dict) -> dict[str, Any]:
    """Simple dict diff — returns keys added/changed/removed."""
    added = {k: after[k] for k in set(after) - set(before)}
    removed = {k: before[k] for k in set(before) - set(after)}
    changed = {
        k: {"old": before[k], "new": after[k]}
        for k in set(before) & set(after)
        if before[k] != after[k]
    }
    return {"added": added, "removed": removed, "changed": changed}


def redact_share_secrets(data: dict) -> dict:
    """Redact common secret patterns from a dict (shim)."""
    _REDACT_KEYS = {"api_key", "secret", "token", "password", "api_token"}

    def _redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if k.lower() in _REDACT_KEYS else _redact(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_redact(item) for item in obj]
        return obj

    result = _redact(data)
    return result if isinstance(result, dict) else {"data": result}


# ===================================================================
# JSONL version store (replace obase.versionstore)
# ===================================================================


async def jsonl_append(*, path: pathlib.Path, entry: dict[str, Any]) -> None:
    """Append a JSON line to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def jsonl_latest(*, path: pathlib.Path, by_key: str = "session_id") -> dict | None:
    """Return the last JSON line from a file, or None."""
    if not path.exists():
        return None
    lines = path.read_text().strip().splitlines()
    if not lines:
        return None
    import json as _json

    entry = _json.loads(lines[-1])
    return entry if isinstance(entry, dict) else None


# ===================================================================
# LLM caller shim (replace oprim.llm / oprim.llm_call)
# ===================================================================


async def llm_call(messages: list[dict], **kwargs: Any) -> dict:
    """
    Chat completion via the canonical LLM layer (veya/llm.py).

    Delegates to the real provider (dashscope/anthropic/openai) when an API key
    is configured; otherwise falls back to a stub response so offline tests and
    unconfigured environments still work.
    """
    from veya.obase.llm import llm_call as _real_llm_call

    return await _real_llm_call(messages, **kwargs)


async def llm_stream(messages: list[dict], **kwargs: Any):
    """Streaming chat completion via the canonical LLM layer."""
    from veya.obase.llm import llm_stream as _real_llm_stream

    async for event in _real_llm_stream(messages, **kwargs):
        yield event


# ===================================================================
# Tool shims (replace oprim.fs, oprim.shell, oprim.git, etc.)
# ===================================================================


async def file_read(path: str, **kwargs: Any) -> dict:
    """Read a file from disk."""
    p = pathlib.Path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    return {"content": p.read_text(), "path": str(p)}


async def file_write(path: str, content: str, **kwargs: Any) -> dict:
    """Write content to a file."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"written": len(content), "path": str(p)}


async def file_read_range(path: str, start: int = 0, end: int | None = None, **kwargs: Any) -> dict:
    """Read a range of lines from a file."""
    result = await file_read(path)
    if "error" in result:
        return result
    lines = result["content"].splitlines()
    selected = lines[start:end] if end is not None else lines[start:]
    return {"content": "\n".join(selected), "path": path, "lines": (start, end or len(lines))}


def glob_match(pattern: str, root: str = ".") -> list[str]:
    """Find files matching a glob pattern."""
    import glob

    return sorted(glob.glob(str(pathlib.Path(root) / pattern), recursive=True))


async def bash_exec(command: str, **kwargs: Any) -> dict:
    """Execute a shell command (subprocess shim)."""
    import subprocess

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=kwargs.get("timeout", 30),
        )
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"error": str(e), "returncode": -1}


def git_status(path: str = ".") -> dict:
    """Get git status (subprocess shim)."""
    result = subprocess_run(f"git -C {path} status --porcelain")
    return {"output": result.get("stdout", ""), "returncode": result.get("returncode", -1)}


def git_diff(path: str = ".", target: str | None = None) -> dict:
    """Get git diff (subprocess shim)."""
    cmd = f"git -C {path} diff"
    if target:
        cmd += f" -- {target}"
    result = subprocess_run(cmd)
    return {"output": result.get("stdout", ""), "returncode": result.get("returncode", -1)}


# ===================================================================
# Network shims (replace oprim.network)
# ===================================================================


async def http_fetch(url: str, **kwargs: Any) -> dict:
    """Fetch a URL (shim — returns placeholder)."""
    return {"url": url, "status": "shim — no real HTTP in compat layer", "body": ""}


async def web_search(query: str, **kwargs: Any) -> dict:
    """Web search shim."""
    return {"query": query, "results": [], "note": "web_search shim — no real search backend"}


# ===================================================================
# LSP / MCP shims (replace oprim.lsp, oprim.mcp)
# ===================================================================


async def lsp_diagnostics(path: str, **kwargs: Any) -> dict:
    """LSP diagnostics shim."""
    return {"path": path, "diagnostics": [], "note": "LSP shim — no language server connected"}


async def mcp_connect(config: dict, **kwargs: Any) -> dict:
    """MCP connection shim."""
    return {"connected": False, "note": "MCP shim — no MCP server configured"}


async def mcp_call_tool(name: str, arguments: dict | None = None, **kwargs: Any) -> dict:
    """MCP tool call shim."""
    return {"error": f"MCP tool '{name}' unavailable — shim layer"}


# ===================================================================
# Subagent shim (replace omodul.run_subagent)
# ===================================================================


@dataclass
class SubagentConfig:
    name: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)


@dataclass
class SubagentInput:
    prompt: str = ""
    config: SubagentConfig | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubagentDefinition:
    name: str = ""
    role: str = ""
    permissions: list[str] = field(default_factory=list)


SubagentPermissions = SubagentDefinition  # alias for backward compat


async def run_subagent(input_data: SubagentInput | dict, **kwargs: Any) -> dict:
    """Run a subagent (shim — returns placeholder result)."""
    prompt = (
        input_data.prompt if isinstance(input_data, SubagentInput) else input_data.get("prompt", "")
    )
    return {
        "status": "success",
        "content": f"Subagent shim processed prompt: {prompt[:80]}...",
    }


# ===================================================================
# Permission shim (replace oskill.permission_evaluate)
# ===================================================================


def permission_evaluate(action: str, resource: str | None = None, **kwargs: Any) -> dict:
    """Evaluate permission（单源委托 obase.authz，§1.4）。"""
    from veya.obase.authz import evaluate_permission

    return evaluate_permission(
        action,
        resource=resource,
        persona=str(kwargs.get("persona", "build")),
        rules=kwargs.get("rules"),
    )


def match_permission_rule(permissions: list, action: str, resource: str | None = None) -> bool:
    """Match a permission rule（单源委托 obase.authz，§1.4）。

    返回 True 表示命中 allow/ask（ask 由调用方决定是否交互）；
    deny 命中返回 False。
    """
    from veya.obase.authz import match_permission_rule as _match

    return _match(list(permissions), action, resource) in ("allow", "ask")


def merge_config(base: dict, override: dict) -> dict:
    """Deep-merge two config dicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = merge_config(result[k], v)
        else:
            result[k] = v
    return result


def plan_to_todos(plan: str, **kwargs: Any) -> list[dict]:
    """Convert a plan string to a list of todos (shim)."""
    return [{"text": line.strip(), "done": False} for line in plan.splitlines() if line.strip()]


# ===================================================================
# Skill shim (replace oskill / oprim.hooks_image_skill)
# ===================================================================


def run_hook(hook_name: str, **kwargs: Any) -> dict:
    """Run a hook (shim)."""
    return {"hook": hook_name, "status": "shim — no real hook executed"}


def read_skill_frontmatter(path: str) -> dict:
    """Read skill frontmatter from a file (shim)."""
    return {"name": "shim_skill", "description": "No real skill frontmatter"}


def evaluate_hooks(hooks: list, context: dict) -> list[dict]:
    """Evaluate hooks (shim)."""
    return [{"hook": h, "status": "shim"} for h in hooks]


def resolve_memory_hierarchy(session_id: str) -> dict:
    """Resolve memory hierarchy (shim)."""
    return {"session_id": session_id, "levels": ["short_term", "long_term"], "note": "shim"}


# ===================================================================
# Misc shims
# ===================================================================


async def process_prompt(messages: list, **kwargs: Any) -> dict:
    """Process a prompt (shim)."""
    last = messages[-1].get("content", "") if messages else ""
    return {"messages": messages, "response": f"Shim response to: {last[:80]}..."}


async def execute_tool(tool_name: str, **kwargs: Any) -> dict:
    """Execute a tool (shim)."""
    return {"tool": tool_name, "result": "shim — tool not implemented"}


async def compact_session(messages: list, **kwargs: Any) -> dict:
    """Compact a conversation (shim)."""
    return {"messages": messages[:2] + messages[-1:], "compacted": True}


async def init_project(config: dict, **kwargs: Any) -> dict:
    """Initialize a project (shim)."""
    return {"status": "success", "note": "init_project shim — no real init"}


def retry_with_backoff(
    func,
    *args,
    max_attempts: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 30.0,
    retryable: tuple = (ConnectionError,),
    **kwargs: Any,
):
    """
    Retry wrapper for async/sync callables with *args/**kwargs.

    - Retries up to ``max_attempts`` times when the callable raises an
      exception that is a subclass of ``retryable``.
    - Non-retryable exceptions are raised immediately.
    - When attempts are exhausted, the last exception is re-raised.
    - Exponential backoff with jitter.
    """
    import asyncio
    import random
    import time

    async def _run():
        last_exc: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                result = func(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            except retryable as exc:
                last_exc = exc
                if attempt < max_attempts - 1:
                    delay = min(base_delay * (2**attempt), max_delay)
                    # Add jitter
                    jitter = random.uniform(0, delay * 0.1)
                    time.sleep(delay + jitter)
        assert last_exc is not None
        raise last_exc

    return _run()


def build_ripgrep_args(pattern: str, *, root: str, glob: str | None = None) -> list[str]:
    """Build ripgrep command arguments."""
    args = ["rg", "--json", "-n"]
    if glob:
        args += ["--glob", glob]
    return [*args, pattern, root]


def parse_ripgrep_output(stdout: str) -> list:
    """Parse ripgrep JSON output lines."""
    import json

    results = []
    for line in stdout.splitlines():
        if line.strip():
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    results.append(data)
            except json.JSONDecodeError:
                continue
    return results


def subprocess_run(command: str, timeout: int = 30) -> dict:
    """Thin subprocess wrapper used by git_status / git_diff."""
    import subprocess as _sub

    try:
        proc = _sub.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        return {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
    except Exception as e:
        return {"error": str(e), "returncode": -1}


# ===================================================================
# ServiceManifest + assemble shim (replace oservi)
# ===================================================================


class ServiceManifest:
    """Manifest describing an engine assembly (replaces oservi.ServiceManifest)."""

    def __init__(
        self,
        name: str,
        skeleton: str,
        inject: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.skeleton = skeleton
        self.inject = inject or {}
        self.trigger = trigger or {}
        self.config = config or {}


class Engine:
    """Minimal engine stub returned by assemble().

    G7: 增加真实 ``run_turn`` —— 按注入约定执行一轮
    ``turn_handler(messages, context) → llm_caller(messages, tools, config)``。
    """

    def __init__(self, manifest: ServiceManifest) -> None:
        self.name = manifest.name
        self.skeleton = manifest.skeleton
        self.inject = manifest.inject
        self.trigger = manifest.trigger
        self.config = manifest.config

    def __repr__(self) -> str:
        return f"Engine(name={self.name!r}, skeleton={self.skeleton!r})"

    async def run_turn(
        self,
        messages: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行一轮 agentic 循环:turn_handler 预备消息 → llm_caller 驱动。"""

        turn_handler = self.inject.get("turn_handler")
        llm_caller = self.inject.get("llm_caller")

        msgs = list(messages)
        if turn_handler is not None:
            prepared = await turn_handler(msgs, context=context)
            if isinstance(prepared, dict) and prepared.get("messages"):
                msgs = prepared["messages"]

        if llm_caller is None:
            return {"content": "", "tool_calls": [], "cost_usd": 0.0}

        result = await llm_caller(
            msgs,
            tools=self.inject.get("tools"),
            config=self.config,
        )
        result.setdefault("status", "completed")
        if not isinstance(result, dict):
            return {"content": str(result), "tool_calls": [], "cost_usd": 0.0}
        # 归一化输出键:coordinator 流式/传统分支分别读 turn_result / output
        result.setdefault("turn_result", result.get("content", ""))
        result.setdefault("output", result.get("content", ""))
        return result

    async def run_subagent(self, input_data: Any, **kwargs: Any) -> dict[str, Any]:
        """子 agent 运行入口(委托注入的 subagent_runner)。"""
        runner = self.inject.get("subagent_runner")
        if runner is None:
            return {"status": "failed", "content": "no subagent_runner injected", "cost_usd": 0.0}
        out = await runner(input_data, **kwargs)
        if not isinstance(out, dict):
            return {"status": "completed", "content": str(out), "cost_usd": 0.0}
        return out


def assemble(manifest: ServiceManifest) -> Engine:
    """Validate manifest cardinality and return an Engine (replace oservi.assemble)."""
    return Engine(manifest)


# ===================================================================
# code_search shim (replace oskill.code_search)
# ===================================================================


def code_search(query: str, *, root: str = ".", limit: int = 20, **kwargs: Any) -> dict:
    """Code search stub."""
    return {"query": query, "results": [], "status": "shim"}


# ===================================================================
# diff_session_state shim
# ===================================================================


def diff_session_state(session_a: dict, session_b: dict, **kwargs: Any) -> dict:
    """Diff two session states."""
    msgs_a = set(str(m) for m in session_a.get("messages", []))
    msgs_b = set(str(m) for m in session_b.get("messages", []))
    return {"added": len(msgs_b - msgs_a), "removed": len(msgs_a - msgs_b)}


# ===================================================================
# Provider registry adapter (single-source → obase.ProviderRegistry)
# ===================================================================


class ProviderRegistry:
    """Adapter over ``obase.ProviderRegistry`` (§1.4 single source).

    Veya's historical shim is replaced by a thin adapter so the canonical
    registry from the obase main library is the single implementation.
    The veya-facing API (``get()``/``get(name)``/``register``/``list``) is kept
    stable; lookups are routed to the main-library registry categories.
    """

    _delegate: Any = None
    _instance: ProviderRegistry | None = None

    @classmethod
    def _d(cls) -> Any:
        """Lazily resolve the obase singleton (never pay for it at import time)."""
        if cls._delegate is None:
            from veya.platform import load as _load

            cls._delegate = _load("obase").ProviderRegistry.get()
        return cls._delegate

    @classmethod
    def get(cls, name: str | None = None) -> ProviderRegistry | Any:
        """Get the singleton adapter instance, or a provider if ``name`` given."""
        if cls._instance is None:
            cls._instance = cls()
        if name is None:
            return cls._instance
        d = cls._d()
        for lookup in (d.llm, d.vlm, d.image_gen):
            try:
                found = lookup(name)
            except Exception:
                found = None
            if found is not None:
                return found
        try:
            return d.generic("generic", name)
        except Exception:
            return None

    def register(self, name: str, provider: Any) -> None:
        """Register under the generic category, overwriting any existing entry."""
        d = self._d()
        d.register_generic("generic", name, provider, replace=True)

    def list(self) -> list[str]:
        """List registered provider names (all categories)."""
        d = self._d()
        try:
            return [p for _, p in d.list_providers()]
        except Exception:
            return []


class LspManager:
    """Stub LSP manager."""

    pass


class EventBus:
    """Stub event bus."""

    pass


# ===================================================================
# Auth shim (replace obase.auth)
# ===================================================================


def auth(token: str | None = None, **kwargs: Any) -> dict:
    """Auth shim."""
    return {"authenticated": token is not None, "note": "auth shim"}


# ===================================================================
# Cost tracker (kept here for assembly.py convenience)
# ===================================================================


# ===================================================================
# CostTracker shim (single-source → veya.utils.CostTracker)
# ===================================================================

from veya.obase.utils import CostTracker as _UtilsCostTracker  # noqa: E402

CostTracker = _UtilsCostTracker


# ===================================================================
# Subagent runner shim (used by assembly.py)
# ===================================================================


# ===================================================================
# Subagent runner shim (used by assembly.py)
# ===================================================================


async def run_subagent_task(input_data: dict | SubagentInput, **kwargs: Any) -> dict:
    """
    Subagent task runner shim.
    Replaces the deleted omodul.run_subagent_task module.
    """
    prompt = ""
    if isinstance(input_data, dict):
        prompt = input_data.get("prompt", input_data.get("text", ""))
    elif isinstance(input_data, SubagentInput):
        prompt = input_data.prompt
    return {
        "status": "success",
        "content": f"Subagent shim: {prompt[:120]}...",
        "cost_usd": 0.0,
    }
