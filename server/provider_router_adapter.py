"""Layer4 assembly for the canonical 3O provider router.

The adapter binds the existing Veya LLM facade to the 3O transaction.  The
facade remains the credential-owning transport implementation; this module
does not copy its endpoint, retry, or secret-resolution tables.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from veya.platform import load

_ACTIVE_CONFIG: ContextVar[dict[str, Any] | None] = ContextVar("veya_provider_config", default=None)


class ProviderRouterAdapter:
    """Bind Veya's existing LLM facade to ``ProviderRouterEngine``."""

    def __init__(
        self,
        *,
        provider_specs: list[Any] | None = None,
        output_dir: Path | None = None,
        usage_sink: Callable[[Any], Any] | None = None,
    ) -> None:
        obase = load("obase")
        oprim = load("oprim")
        oskill = load("oskill")
        omodul = load("omodul")
        oservi = load("oservi")
        self._obase = obase
        self._oprim = oprim
        self._usage_sink = usage_sink
        self.records: list[Any] = []
        self.output_dir = output_dir or Path(".veya/provider")
        self._provider_specs = list(provider_specs or [])
        self._engine = oservi.ProviderRouterEngine(
            select_provider=oskill.select_provider,
            fallback_decision=oskill.fallback_decision,
            provider_call=oprim.provider_call,
            usage_record=oprim.usage_record,
            provider_inference_transaction=omodul.provider_inference_transaction,
            provider_caller=self._call_existing_facade,
            provider_health_probe=oprim.provider_health_probe,
            pricing_lookup=oprim.pricing_lookup,
            trigger={"on_demand": True},
            config={"output_dir": str(self.output_dir)},
            name="veya-provider-router",
        )
        self._engine.run()

    @staticmethod
    def _default_specs(
        provider: str | None, model: str | None, config: Mapping[str, Any]
    ) -> list[Any]:
        """Build metadata from the existing Veya config tables."""
        from veya.obase._llm_config import _DEFAULT_MODELS, _PRICING
        from veya.obase.llm import get_provider_config

        selected_provider, selected_model = get_provider_config(
            dict(config), provider=provider, model=model
        )
        names = [selected_provider]
        for fallback in config.get("fallback_providers", []) or []:
            if str(fallback) not in names:
                names.append(str(fallback))
        specs = []
        for name in names:
            model_name = (
                model
                if name == selected_provider and model
                else _DEFAULT_MODELS.get(name, selected_model)
            )
            input_price, output_price = _PRICING.get(name, (0.0, 0.0))
            pricing = load("obase").Pricing(
                provider=name,
                model=model_name,
                input_usd_per_token=input_price / 1_000_000,
                output_usd_per_token=output_price / 1_000_000,
            )
            specs.append(
                load("obase").ProviderSpec(
                    name=name,
                    models=(
                        load("obase").ModelSpec(
                            name=model_name,
                            provider=name,
                            pricing=pricing,
                            capabilities=frozenset({"chat"}),
                            supports_streaming=True,
                            supports_tools=True,
                        ),
                    ),
                    credential_ref=f"veya-config:{name}",
                )
            )
        return specs

    async def _call_existing_facade(self, request: Any) -> Any:
        from veya.obase.llm import llm_call, llm_stream

        messages = [dict(message) for message in request.messages]
        kwargs: dict[str, Any] = {
            "provider": request.provider,
            "model": request.model,
            "tools": [dict(tool) for tool in request.tools] or None,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        config = _ACTIVE_CONFIG.get() or {}
        if config:
            kwargs["config"] = config
        if request.stream:
            return llm_stream(messages, **kwargs)
        return await llm_call(messages, **kwargs)

    async def _record(self, record: Any) -> Any:
        self.records.append(record)
        if self._usage_sink is None:
            return record
        result = self._usage_sink(record)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def _specs(
        self, provider: str | None, model: str | None, config: Mapping[str, Any]
    ) -> list[Any]:
        return self._provider_specs or self._default_specs(provider, model, config)

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        stream: bool = False,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Run a non-streaming transaction with the legacy response shape."""
        active_config = dict(config or {})
        token = _ACTIVE_CONFIG.set(active_config)
        try:
            result = await self._engine.infer(
                {
                    "messages": messages,
                    "tools": tools,
                    "candidates": self._specs(provider, model, active_config),
                    "config": {
                        "llm_provider": provider or "",
                        "llm_model": model or "",
                        "streaming": stream,
                        "fallback_policy": active_config.get("fallback_policy", {}),
                        "max_attempts": active_config.get("max_attempts", 2),
                    },
                    "usage_sink": self._record,
                    "request_ref": active_config.get("request_ref"),
                },
                output_dir=self.output_dir,
                on_step=on_event,
            )
        finally:
            _ACTIVE_CONFIG.reset(token)
        if result.get("status") == "completed":
            response = result.get("response")
            return response if isinstance(response, dict) else result
        return result

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay preserved provider events while the transaction is running."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def relay(event: dict[str, Any]) -> None:
            if event.get("event") == "provider_event" and isinstance(event.get("data"), dict):
                queue.put_nowait(event["data"])
            if on_event is not None:
                on_event(event)

        task = asyncio.create_task(
            self(
                messages,
                tools=tools,
                config=config,
                provider=provider,
                model=model,
                stream=True,
                on_event=relay,
            )
        )
        try:
            while True:
                if task.done() and queue.empty():
                    break
                event = await queue.get()
                if event is not None:
                    yield event
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


__all__ = ["ProviderRouterAdapter"]
