"""server.runtimes — 三框架运行时装配 (shim 层)。

3O 单一来源: 协议与适配器在主库 oservi.runtime_bridge; 本层只做:
  1. re-export (兼容既有引用)
  2. 装配注册 (Infra.init → agent_registry runtime 类型)
"""

from __future__ import annotations

from typing import Any

from veya.platform import load as _load

_oservi = _load("oservi")
from oservi.runtime_bridge import (  # noqa: E402
    ALL_RUNTIMES,
    AgentRuntime,
    agentscope_bridge,
    pi_bridge,
    prime_agent_runtime,
    register_all_runtimes,
)

from server.runtimes.base import register_runtime  # noqa: E402


def runtime_status() -> list[dict[str, Any]]:
    """各适配器健康 (同步探测: 无 loop 场景; async 端点请直接 await health)。"""
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
    "AgentRuntime",
    "agentscope_bridge",
    "pi_bridge",
    "prime_agent_runtime",
    "register_all_runtimes",
    "register_runtime",
    "runtime_status",
]
