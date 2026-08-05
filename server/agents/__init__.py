"""Veya Genesis — 专属 3O 护库智能体 (Stateful Agent Entity)。

独立身份 (Isolated Identity): 专属 API Key,与 Veya 主业务物理隔离。
永久记忆 (Persistent Memory): Ledger + Experience Log,重启无缝恢复。
长期在线 (Daemon Mode): 常驻守护进程,只处理 3O 架构级指令。

子模块:
- genesis_memory:    永久记忆系统 (Memory Bank)
- architect_tools:   3O 物理执行层 (ThreeOPhysicalTools)
- genesis_agent:     完整独立 Agent 实体 (GenesisAgent)
- genesis_daemon:    常驻守护进程 (GenesisDaemon, python -m server.agents.genesis_daemon)
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GenesisAgent",
    "GenesisDaemon",
    "GenesisMemory",
    "ThreeOPhysicalTools",
]


def __getattr__(name: str) -> Any:
    """PEP 562 惰性导出: 避免 eager import 触发 runpy 警告并拖慢包导入。"""
    if name == "GenesisMemory":
        from server.agents.genesis_memory import GenesisMemory

        return GenesisMemory
    if name == "ThreeOPhysicalTools":
        from server.agents.architect_tools import ThreeOPhysicalTools

        return ThreeOPhysicalTools
    if name == "GenesisAgent":
        from server.agents.genesis_agent import GenesisAgent

        return GenesisAgent
    if name == "GenesisDaemon":
        from server.agents.genesis_daemon import GenesisDaemon

        return GenesisDaemon
    raise AttributeError(f"module 'server.agents' has no attribute {name!r}")
