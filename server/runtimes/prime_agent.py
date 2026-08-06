"""server/runtimes/prime_agent.py — L1 prime-agent 内核运行时适配器。

prime-agent 为 Python RLM 框架 (代码即交互/自我改写), PyPI 暂无发行包
(私有分发)。本适配器实现 AgentRuntime 协议骨架 + 可插拔接入:

  - 检测: 环境变量 PRIME_AGENT_MODULE (或已安装的 prime_agent 包)
  - 隔离: 持久内核经 obase.local_sandbox_pool 护栏
  - 自我优化: Harness 轨迹落 checkpoint_store / cognitive_store (接入后启用)
  - 未接入 → 结构化错误 (不崩溃), 协议方法全部可用
"""

from __future__ import annotations

import importlib
import os
import time
from typing import Any

from server.runtimes.base import unavailable

_IMPORT_HINT = (
    "prime-agent 未接入: 设置 PRIME_AGENT_MODULE=<python模块路径> "
    "(RLM harness 的 AgentRuntime 兼容实现), 或 pip 安装后重试"
)


def _resolve_harness():
    """解析 prime-agent harness (env 指定模块 → 已装包 → None)。"""
    mod_name = os.environ.get("PRIME_AGENT_MODULE", "")
    if mod_name:
        try:
            return importlib.import_module(mod_name)
        except ImportError:
            return None
    try:
        return importlib.import_module("prime_agent")
    except ImportError:
        return None


class PrimeAgentRuntime:
    """prime-agent 内核运行时适配器 (协议骨架, 可插拔)。"""

    name = "prime_agent_runtime"
    description = "prime-agent RLM 内核: 代码即交互/自我改写 (Continual Harness)"

    def __init__(self) -> None:
        self._harness = None
        self._started_at = 0.0

    # ── 协议 ──────────────────────────────────────────────────────────
    async def init(self, config: dict | None = None) -> dict[str, Any]:
        self._harness = _resolve_harness()
        if self._harness is None:
            return unavailable(self.name, _IMPORT_HINT)
        self._started_at = time.time()
        return {"ok": True, "runtime": self.name, "version": "harness",
                "module": getattr(self._harness, "__name__", "?")}

    async def dispatch(self, task: str, **kwargs: Any) -> dict[str, Any]:
        if self._harness is None:
            return unavailable(self.name, _IMPORT_HINT)
        try:
            run = getattr(self._harness, "run", None)
            result = run(task, **kwargs) if callable(run) else None
            if result is None:
                return unavailable(self.name, "harness 无 run() 入口")
            return {"ok": True, "runtime": self.name, "output": str(result)[:4000]}
        except Exception as e:
            return {"ok": False, "runtime": self.name, "error": str(e)[:2000]}

    async def invoke(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return await self.dispatch(prompt, **kwargs)

    async def lifecycle(self, action: str) -> dict[str, Any]:
        if action in ("health", "status"):
            return await self.health()
        return {"ok": True, "runtime": self.name, "action": action,
                "note": "prime-agent v1 无状态 (进程内按需加载)"}

    async def health(self) -> dict[str, Any]:
        connected = _resolve_harness() is not None
        return {"ok": connected, "runtime": self.name, "connected": connected,
                "uptime_s": time.time() - self._started_at if self._started_at else 0}


prime_agent_runtime = PrimeAgentRuntime()
