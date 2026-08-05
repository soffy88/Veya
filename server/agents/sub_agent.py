"""Veya Sub-Agent: 蜂群中的轻量级工作节点(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 oskill.sub_agent.SubAgent。
本层注入 veya.llm.llm_call 作为 LLM 调用器并保留 VeyaSubAgent API。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veya.llm import get_provider_config, llm_call
from veya.platform import oskill as _load_oskill

_oskill = _load_oskill()


class VeyaSubAgent:
    """蜂群工作节点: 角色面具 + 项目上下文 + 单任务执行(委托主库技能)。"""

    def __init__(
        self,
        role: str,
        context: str,
        api_key: str | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        llm_fn: Callable | None = None,
        temperature: float = 0.2,
        max_retries: int = 3,
    ):
        self.role = role
        self.context = context
        self.api_key = api_key or ""
        self.model = model
        self.provider = provider
        self.endpoint = endpoint
        self.temperature = temperature
        self.max_retries = max_retries

        # 蜂群专用 Key 只注入本实例 config
        self._llm_config: dict[str, Any] = {}
        if self.api_key:
            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["providers"] = {resolved_provider: {"api_key": self.api_key}}
        if endpoint:
            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["endpoints"] = {resolved_provider: endpoint}

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

        self._skill = _oskill.sub_agent.SubAgent(
            role=role,
            context=context,
            llm_caller=_bound_llm,
            temperature=temperature,
            max_retries=max_retries,
        )

    def get_system_prompt(self) -> str:
        return self._skill.get_system_prompt()

    async def execute(self, task_instruction: str) -> str:
        return await self._skill.execute(task_instruction)
