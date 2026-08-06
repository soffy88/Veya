"""server/runtimes/base.py — AgentRuntime 统一协议 + 注册助手。

三框架适配器 (prime-agent / pi / agentscope) 实现同一协议,
上层 (编排/CLI/MCP) 零感知差异; 治理由 veya hooks 统一包裹。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from obase.agent_registry import AgentRegistry, RegistryConflictError


@runtime_checkable
class AgentRuntime(Protocol):
    """统一运行时协议 (PRD: docs/prd/AGENT_RUNTIMES_PRD.md §3)。"""

    name: str
    kind: str = "runtime"

    async def init(self, config: dict | None = None) -> dict: ...
    async def dispatch(self, task: str, **kwargs: Any) -> dict: ...
    async def invoke(self, prompt: str, **kwargs: Any) -> dict: ...
    async def lifecycle(self, action: str) -> dict: ...
    async def health(self) -> dict: ...


def register_runtime(adapter: AgentRuntime,
                     registry: AgentRegistry | None = None) -> dict[str, Any]:
    """注册适配器到 agent_registry (runtime 类型, 幂等)。"""
    reg = registry or AgentRegistry()
    try:
        reg.register("runtime", adapter.name, adapter,
                     desc=getattr(adapter, "description", adapter.__class__.__doc__ or ""))
        return {"registered": adapter.name}
    except RegistryConflictError:
        return {"skipped": adapter.name}


def unavailable(adapter_name: str, reason: str) -> dict[str, Any]:
    """依赖缺失时的统一结构化返回 (不崩溃)。"""
    return {"ok": False, "runtime": adapter_name, "error": reason}


__all__ = ["AgentRuntime", "register_runtime", "unavailable"]
