"""InferX free-model registration for the veya-free pool.

DeepSeek V4.1 Flash 1017 Basic is $0/$0 on the vendor homepage, verified by a
live probe 2026-11-03 (HTTP 200, 1.1s). The vendor's /models catalog lists ids
only (no pricing/free flags), so gateway discovery's free filter cannot
qualify it: the seed entry + the daily probe is the qualification path, and
the lifecycle evicts it on repeated failure like any other seed.
"""

from __future__ import annotations

import pytest

from veya import llm as hllm
from veya.obase import canonical_proxies as cp
from veya.obase.free_pool import FreePoolLifecycle, FreePoolSnapshot


def test_inferx_provider_registration():
    assert hllm._ENDPOINTS["inferx"] == "https://model.inferx.net/endpoints/v1/chat/completions"
    assert hllm._API_KEY_ENV["inferx"] == "INFERX_API_KEY"
    assert hllm._PRICING["inferx"] == (0.0, 0.0)
    assert hllm._DEFAULT_MODELS["inferx"] == "deepseek-v4.1-flash-1017-basic"


def test_inferx_seed_entry():
    entries = [entry for entry in hllm._VEYA12_FREE_POOL if entry["provider"] == "inferx"]
    assert entries == [
        {
            "provider": "inferx",
            "model": "deepseek-v4.1-flash-1017-basic",
            "endpoint": "https://model.inferx.net/endpoints/v1",
            "source": "inferx",
        }
    ]


def test_free_pool_allows_inferx_by_default(monkeypatch):
    monkeypatch.delenv("VEYA_FREE_POOL_PROVIDERS", raising=False)
    assert "inferx" in cp._free_pool_allowed_providers()


def test_free_pool_candidates_include_inferx(monkeypatch):
    entry = {
        "provider": "inferx",
        "model_id": "deepseek-v4.1-flash-1017-basic",
        "canonical_proxy": "veya-free",
        "capabilities": ["text"],
        "discovered": True,
        "healthy": True,
        "credentials_valid": True,
        "endpoint_available": True,
        "model_available": True,
        "cooldown_until": None,
        "eligible": True,
        "latency_ms": 1100,
        "endpoint": "https://model.inferx.net/endpoints/v1/chat/completions",
    }
    monkeypatch.setattr(
        cp, "_load_state", lambda: {"models": {"inferx:deepseek-v4.1-flash-1017-basic": entry}}
    )
    monkeypatch.delenv("VEYA_FREE_POOL_PROVIDERS", raising=False)
    assert cp.free_pool_candidates() == [
        {
            "provider": "inferx",
            "model": "deepseek-v4.1-flash-1017-basic",
            "source": "inferx",
            "endpoint": "https://model.inferx.net/endpoints/v1/chat/completions",
        }
    ]


@pytest.mark.asyncio
async def test_seed_survives_healthy_catalog_without_free_entries(tmp_path):
    """Discovery lists the id but cannot flag it free; the probed seed stays."""
    seed = {
        "provider": "inferx",
        "model": "deepseek-v4.1-flash-1017-basic",
        "endpoint": "https://model.inferx.net/endpoints/v1",
        "source": "inferx",
    }
    lifecycle = FreePoolLifecycle([seed], state_path=tmp_path / "pool.json")

    async def healthy(entry):
        return True, ""

    active = await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(),
            available={"inferx": frozenset({"deepseek-v4.1-flash-1017-basic"})},
            healthy_sources=frozenset({"inferx"}),
            errors={},
        ),
        healthy,
    )
    assert [item["model"] for item in active] == ["deepseek-v4.1-flash-1017-basic"]


@pytest.mark.asyncio
async def test_seed_removed_when_catalog_drops_the_model(tmp_path):
    seed = {
        "provider": "inferx",
        "model": "deepseek-v4.1-flash-1017-basic",
        "endpoint": "https://model.inferx.net/endpoints/v1",
        "source": "inferx",
    }
    lifecycle = FreePoolLifecycle([seed], state_path=tmp_path / "pool.json")

    async def healthy(entry):
        return True, ""

    await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(),
            available={"inferx": frozenset({"deepseek-v4.1-flash-1017-basic"})},
            healthy_sources=frozenset({"inferx"}),
            errors={},
        ),
        healthy,
    )
    active = await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(),
            available={"inferx": frozenset()},
            healthy_sources=frozenset({"inferx"}),
            errors={},
        ),
        healthy,
    )
    assert active == []
