"""PR-12 provider router and usage contract tests."""
# Imports after ``veya.platform.load`` are intentional: the 3O submodules are
# mounted lazily by the project assembly layer.
# ruff: noqa: E402, I001

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from veya.platform import load


load("obase")
load("oprim")
load("oskill")
load("omodul")
load("oservi")

from obase import (
    ModelSpec,
    Pricing,
    ProviderCallRequest,
    ProviderHealth,
    ProviderRegistry,
    ProviderSpec,
    UsageRecord,
)
from omodul import (
    ProviderInferenceConfig,
    ProviderInferenceInput,
    provider_inference_transaction,
)
from oservi import ServiceManifest, assemble
from oskill import aggregate_usage, fallback_decision, select_provider
from oprim import (
    pricing_lookup,
    provider_call,
    provider_health_probe,
    usage_record,
)
from server.provider_router_adapter import ProviderRouterAdapter


def _spec(name: str, model: str, *, priority: int = 0, healthy: bool = True) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        priority=priority,
        health=ProviderHealth(name, healthy=healthy),
        models=(
            ModelSpec(
                name=model,
                provider=name,
                capabilities=frozenset({"chat"}),
                pricing=Pricing(name, model, 1e-6, 2e-6),
            ),
        ),
        credential_ref=f"ref:{name}",
    )


def test_provider_metadata_registry_and_selection_are_execution_only() -> None:
    ProviderRegistry.clear()
    registry = ProviderRegistry.get()
    registry.register_spec(_spec("slow", "slow-model", priority=1))
    registry.register_spec(_spec("fast", "fast-model", priority=5))

    selected = select_provider(registry.list_specs(), capability="chat", preferred_provider="fast")
    assert selected["provider"] == "fast"
    assert selected["model"] == "fast-model"
    assert selected["credential_ref"] == "ref:fast"
    assert "messages" not in selected


@pytest.mark.asyncio
async def test_provider_atomics_normalize_health_call_usage_and_pricing() -> None:
    spec = _spec("demo", "demo-model")
    request = ProviderCallRequest("demo", "demo-model", messages=())
    called: list[str] = []

    async def caller(value: ProviderCallRequest) -> dict[str, Any]:
        called.append(value.provider)
        return {"ok": True}

    assert (await provider_call(request, caller=caller))["ok"] is True
    assert called == ["demo"]
    assert (await provider_health_probe(spec, probe=lambda _: True))["healthy"] is True

    records: list[UsageRecord] = []
    record = UsageRecord("demo", "demo-model", input_tokens=2, output_tokens=3)
    await usage_record(record, sink=records.append)
    assert records == [record]
    assert (
        pricing_lookup(
            {"category": "llm", "provider": "demo", "model": "demo-model", "unit": "per_token"},
            table={("demo", "demo-model"): spec.models[0].pricing},
        )
        is spec.models[0].pricing
    )


@pytest.mark.asyncio
async def test_transaction_falls_back_without_losing_usage(tmp_path: Path) -> None:
    specs = [_spec("first", "first-model", priority=2), _spec("second", "second-model")]
    calls: list[str] = []
    records: list[UsageRecord] = []

    async def call(request: ProviderCallRequest) -> dict[str, Any]:
        calls.append(request.provider)
        if request.provider == "first":
            raise RuntimeError("first unavailable")
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 5},
        }

    result = await provider_inference_transaction(
        ProviderInferenceConfig(max_attempts=2),
        ProviderInferenceInput(
            messages=[{"role": "user", "content": "hello"}],
            candidates=specs,
            provider_call=call,
            select_provider=select_provider,
            fallback_decision=fallback_decision,
            usage_record=usage_record,
            usage_sink=records.append,
        ),
        tmp_path,
    )

    assert result["status"] == "completed"
    assert calls == ["first", "second"]
    assert [record.provider for record in records] == ["first", "second"]
    assert records[0].success is False
    assert records[1].total_tokens == 9
    assert result["usage"]["estimated_cost_usd"] == pytest.approx(14e-6)


@pytest.mark.asyncio
async def test_streaming_transaction_preserves_thinking_tool_and_usage(tmp_path: Path) -> None:
    spec = _spec("stream", "stream-model")
    seen: list[dict[str, Any]] = []

    async def stream_call(_: ProviderCallRequest):
        for event in (
            {"type": "thinking", "text": "checking"},
            {"type": "tool_call", "name": "file_read", "arguments": {"path": "x"}},
            {"type": "text_delta", "text": "done"},
            {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 3}},
        ):
            yield event

    def on_step(event: dict[str, Any]) -> None:
        if event.get("event") == "provider_event":
            seen.append(event["data"])

    result = await provider_inference_transaction(
        ProviderInferenceConfig(streaming=True),
        ProviderInferenceInput(
            messages=[{"role": "user", "content": "hello"}],
            candidates=[spec],
            provider_call=stream_call,
            select_provider=select_provider,
            fallback_decision=fallback_decision,
            usage_record=usage_record,
        ),
        tmp_path,
        on_step=on_step,
    )

    assert result["status"] == "completed"
    assert [event["type"] for event in seen] == ["thinking", "tool_call", "text_delta", "usage"]
    assert result["usage"]["input_tokens"] == 2
    assert result["usage"]["output_tokens"] == 3


@pytest.mark.asyncio
async def test_router_engine_injection_contract_and_usage_view(tmp_path: Path) -> None:
    spec = _spec("engine", "engine-model")
    recorded: list[UsageRecord] = []

    async def adapter(request: ProviderCallRequest) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "engine-ok"}}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    engine = assemble(
        ServiceManifest(
            name="test-provider-router",
            skeleton="provider_router",
            inject={
                "select_provider": select_provider,
                "fallback_decision": fallback_decision,
                "provider_call": provider_call,
                "usage_record": usage_record,
                "provider_inference_transaction": provider_inference_transaction,
                "provider_caller": adapter,
            },
            trigger={"on_demand": True},
            config={"output_dir": str(tmp_path)},
        )
    )
    assert engine.health()["status"] == "stopped"
    engine.run()
    result = await engine.infer(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "candidates": [spec],
            "usage_sink": recorded.append,
        },
        output_dir=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["response"]["choices"][0]["message"]["content"] == "engine-ok"
    assert len(recorded) == 1
    assert engine.health()["details"]["semantic_routing"] is False


def test_usage_aggregation_and_fallback_are_pure() -> None:
    records = [UsageRecord("a", "m", 2, 3, latency_ms=4, estimated_cost_usd=0.1)]
    before = records[0].to_dict()
    assert aggregate_usage(records)["total_tokens"] == 5
    assert fallback_decision([{"success": False, "error_type": "timeout"}], max_attempts=2)["retry"]
    assert records[0].to_dict() == before


@pytest.mark.asyncio
async def test_layer4_adapter_reuses_existing_facade_without_secret_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec("fixture", "fixture-model")
    import veya.obase.llm as facade

    async def fake_llm_call(messages: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        assert messages[0]["role"] == "user"
        return {
            "choices": [{"message": {"content": "bound"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }

    monkeypatch.setattr(facade, "llm_call", fake_llm_call)
    adapter = ProviderRouterAdapter(provider_specs=[spec], output_dir=tmp_path)
    result = await adapter([{"role": "user", "content": "hello"}])

    assert result["choices"][0]["message"]["content"] == "bound"
    assert len(adapter.records) == 1
    assert "credential" not in adapter.records[0].to_dict()
