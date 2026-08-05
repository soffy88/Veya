"""Veya Model Router: 算力经济学与多模型动态调度引擎(薄适配层)。

3O 单一来源 (§1.4): 本体已固化为主库 omodul.model_router.ModelRouter
(基于 oprim._task_tier 分级 + obase.cost_tracker 记账)。
本层注入 veya.llm.llm_call 双配置(旗舰/经济)并保留脚手架 API。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veya.llm import get_provider_config, llm_call
from veya.platform import omodul as _load_omodul

_omodul = _load_omodul()


class VeyaModelRouter:
    """动态模型路由: 轻任务走经济模型, 高难度走旗舰模型(委托主库路由器)。"""

    def __init__(
        self,
        flagship_api_key: str | None = None,
        cheap_api_key: str | None = None,
        *,
        flagship_model: str | None = None,
        cheap_model: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        llm_fn: Callable | None = None,
    ):
        user_llm = llm_fn or llm_call
        resolved_provider = provider or get_provider_config(None, provider=provider)[0]

        def _caller(key: str | None, model: str | None) -> Callable:
            config: dict[str, Any] = {}
            if key:
                config["providers"] = {resolved_provider: {"api_key": key}}
            if endpoint:
                config["endpoints"] = {resolved_provider: endpoint}

            def _bound(messages, **kwargs):
                return user_llm(
                    messages,
                    config=config,
                    model=model,
                    provider=resolved_provider,
                    endpoint=endpoint,
                    **kwargs,
                )

            return _bound

        self.flagship_model = flagship_model or "flagship"
        self.cheap_model = cheap_model or "economy"
        self._router = _omodul.model_router.ModelRouter(
            flagship_caller=_caller(flagship_api_key, flagship_model),
            economy_caller=_caller(cheap_api_key or flagship_api_key, cheap_model),
            flagship_model=self.flagship_model,
            economy_model=self.cheap_model,
        )

    async def completion(
        self,
        prompt: str,
        task_type: str = "general",
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """根据任务元数据动态路由模型, 兼顾性能与成本。"""
        return await self._router.completion(prompt, task_type=task_type, system_prompt=system_prompt, **kwargs)

    def get_stats(self) -> dict[str, Any]:
        return self._router.get_stats()
