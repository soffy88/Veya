"""server/runtimes/__init__.py — 三框架运行时适配器装配。

注册: agent_registry runtime 类型 (L1/L2/L3 全量, 幂等)。
状态: RUNTIME_LEDGER pending → registered (装配期, operator_ledger)。
"""

from __future__ import annotations

from typing import Any

from obase.agent_registry import AgentRegistry

from server.runtimes.agentscope_bridge import agentscope_bridge
from server.runtimes.base import AgentRuntime, register_runtime
from server.runtimes.pi_bridge import pi_bridge
from server.runtimes.prime_agent import prime_agent_runtime

ALL_RUNTIMES: list[AgentRuntime] = [
    prime_agent_runtime,   # L1 内核
    pi_bridge,             # L2 工具链
    agentscope_bridge,     # L3 平台
]


def register_all_runtimes(registry: AgentRegistry | None = None) -> dict[str, Any]:
    """注册全部适配器 (幂等)。"""
    registered: list[str] = []
    skipped: list[str] = []
    for rt in ALL_RUNTIMES:
        out = register_runtime(rt, registry)
        (registered if "registered" in out else skipped).append(rt.name)
    return {"registered": registered, "skipped": skipped}


def runtime_status() -> list[dict[str, Any]]:
    """各适配器健康 (探测, 不初始化)。"""
    import asyncio

    out = []
    for rt in ALL_RUNTIMES:
        try:
            h = asyncio.run(rt.health())
        except Exception as e:
            h = {"ok": False, "error": str(e)[:200]}
        out.append({"name": rt.name, "description": getattr(rt, "description", ""), **h})
    return out


__all__ = [
    "ALL_RUNTIMES",
    "agentscope_bridge",
    "pi_bridge",
    "prime_agent_runtime",
    "register_all_runtimes",
    "runtime_status",
]
