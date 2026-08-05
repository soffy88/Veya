"""Veya Swarm Manager: 多智能体并发与规约引擎(薄适配层)。

3O 单一来源 (§1.4): 编排器本体已固化为主库
omodul.swarm_orchestrator.SwarmOrchestrator + oskill.sub_agent.SubAgent。
本层职责:
1. 把主库 EventBus 的 swarm_notify 事件桥接到 Veya fire_step(SSE 通知中心);
2. 注入 veya.llm.llm_call 作为主库编排器的 LLM 调用器;
3. 保留既有 API(SwarmOrchestrator / run_swarm / _run_and_notify)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from server.events import fire_step
from veya.llm import llm_call
from veya.platform import obase as _load_obase
from veya.platform import omodul as _load_omodul
from veya.platform import oskill as _load_oskill

_obase = _load_obase()
_omodul = _load_omodul()
_oskill = _load_oskill()


def _bridge_swarm_notify(event: Any) -> None:
    """主库事件总线 → Veya SSE 管道(fire_step)。"""
    p = event.payload
    fire_step(
        {
            "type": "swarm",
            "level": p.get("level", "INFO"),
            "title": p.get("title", ""),
            "content": p.get("content", ""),
        }
    )


_bridge_registered = False


def _ensure_swarm_bridge() -> None:
    """把 swarm_notify 桥接订阅注册到主库默认事件总线(幂等)。"""
    global _bridge_registered
    if _bridge_registered:
        return
    _obase.event_bus.default_event_bus.subscribe("swarm_notify", _bridge_swarm_notify)
    _bridge_registered = True


class SwarmOrchestrator:
    """蜂群调度器: Map-Reduce 并发执行与规约(委托主库 omodul 编排器)。"""

    def __init__(
        self,
        master_api_key: str | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        llm_fn: Callable | None = None,
        sub_agent_factory: Callable | None = None,
        temperature: float = 0.2,
        notify_delay: float = 0.5,
    ):
        _ensure_swarm_bridge()

        # 宿主注入 LLM 调用器(带 config/model/provider/endpoint 装配)
        user_llm = llm_fn or llm_call

        def _bound_llm(messages, **kwargs):
            return user_llm(
                messages,
                config=self._llm_config,
                model=model,
                provider=provider,
                endpoint=endpoint,
                **kwargs,
            )

        # 用户侧 Key 只注入本实例 config
        self._llm_config: dict[str, Any] = {}
        if master_api_key:
            from veya.llm import get_provider_config

            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["providers"] = {resolved_provider: {"api_key": master_api_key}}
        if endpoint:
            from veya.llm import get_provider_config

            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["endpoints"] = {resolved_provider: endpoint}

        def default_factory(role: str, context: str):
            return _oskill.sub_agent.SubAgent(
                role=role,
                context=context,
                llm_caller=_bound_llm,
                temperature=temperature,
            )
        self._engine = _omodul.swarm_orchestrator.SwarmOrchestrator(
            llm_caller=_bound_llm,
            sub_agent_factory=sub_agent_factory or default_factory,
            notify_delay=notify_delay,
        )

    async def run_swarm(self, overarching_goal: str, sub_tasks: list[dict]) -> str:
        return await self._engine.run_swarm(overarching_goal, sub_tasks)

    async def _run_and_notify(self, agent: Any, instruction: str, index: int) -> str:
        return await self._engine._run_and_notify(agent, instruction, index)
