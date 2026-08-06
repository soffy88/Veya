"""server/runtimes/agentscope_bridge.py — L3 agentscope 平台编排桥 (双向翻译)。

agentscope (阿里多智能体平台, pip 2.x): Event Bus / 中间件 / MCP / Skill Hub。
本桥:
  - init: 检测 agentscope 安装 → 版本; 未装 → 结构化 (含安装指引)
  - dispatch: 经 agentscope MsgHub/Agent 派发 (安装后启用)
  - 事件映射: agentscope 事件 ↔ obase.event_bus (翻译桥, 见 PRD §5)
  - 中间件 ↔ veya hooks: 权限/脱敏/测试门 对应 (装配期挂载)
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from server.runtimes.base import unavailable

_IMPORT_HINT = (
    "agentscope 未安装: pip install agentscope (2.x) "
    "—— Event Bus / 中间件 / MCP / Skill Hub 桥接需该包"
)

# agentscope 事件 → veya event_bus 主题 (PRD §5 事件映射表)
_EVENT_MAP: dict[str, str] = {
    "start": "agent.start",
    "message": "agent.message",
    "end": "agent.end",
    "error": "agent.error",
}


class AgentScopeBridge:
    """agentscope 平台编排桥 (双向翻译, 可插拔)。"""

    name = "agentscope_bridge"
    description = "agentscope 平台桥: Event Bus 翻译 + 中间件↔hooks + MCP/Skill Hub"

    def __init__(self) -> None:
        self._ascope: Any = None
        self._started_at = 0.0

    def _load(self) -> Any | None:
        try:
            return importlib.import_module("agentscope")
        except ImportError:
            return None

    def _translate_event(self, ascope_event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """agentscope 事件 → (veya event_bus 主题, payload)。"""
        ev_type = (ascope_event.get("type") or "").lower()
        topic = _EVENT_MAP.get(ev_type, f"agent.{ev_type or 'unknown'}")
        return topic, {"source": "agentscope", **ascope_event}

    async def publish_to_veya(self, ascope_event: dict[str, Any]) -> dict[str, Any]:
        """翻译桥: agentscope 事件 → obase.event_bus。"""
        try:
            from obase.event_bus import event_bus

            topic, payload = self._translate_event(ascope_event)
            event_bus.publish(topic, payload)
            return {"ok": True, "topic": topic}
        except Exception as e:
            return {"ok": False, "error": f"event_bus 发布失败: {e}"}

    # ── 协议 ──────────────────────────────────────────────────────────
    async def init(self, config: dict | None = None) -> dict[str, Any]:
        self._ascope = self._load()
        if self._ascope is None:
            return unavailable(self.name, _IMPORT_HINT)
        self._started_at = time.time()
        version = getattr(self._ascope, "__version__", "unknown")
        return {"ok": True, "runtime": self.name, "version": version}

    async def dispatch(self, task: str, **kwargs: Any) -> dict[str, Any]:
        if self._ascope is None:
            return unavailable(self.name, _IMPORT_HINT)
        try:
            # 安装后: agentscope 的 Agent/MsgHub 派发 (具体 API 以安装版本为准)
            agent_cls = getattr(self._ascope, "Agent", None)
            if agent_cls is None:
                return unavailable(self.name, "agentscope 无 Agent 入口 (版本差异)")
            agent = agent_cls(name=f"veya-{int(time.time())}")
            reply = agent(task)
            return {"ok": True, "runtime": self.name,
                    "output": str(getattr(reply, "content", reply))[:4000]}
        except Exception as e:
            return {"ok": False, "runtime": self.name, "error": str(e)[:2000]}

    async def invoke(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return await self.dispatch(prompt, **kwargs)

    async def lifecycle(self, action: str) -> dict[str, Any]:
        if action in ("health", "status"):
            return await self.health()
        return {"ok": True, "runtime": self.name, "action": action,
                "note": "agentscope bridge v1 按需实例 (无平台常驻)"}

    async def health(self) -> dict[str, Any]:
        connected = self._load() is not None
        return {"ok": connected, "runtime": self.name, "connected": connected,
                "event_map": _EVENT_MAP}


agentscope_bridge = AgentScopeBridge()
