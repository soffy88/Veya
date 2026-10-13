"""W11 bot chat: veya-free tries the host gateway pool before direct :free models."""

from __future__ import annotations

import asyncio

from veya.obase import llm


def _run(monkeypatch, gateway):
    seen: list[list[str]] = []

    async def fake_rr(messages, kwargs, *, pool, start, **_kw):
        rotated = pool[start:] + pool[:start]
        seen.append([c["provider"] for c in rotated])
        return {"choices": [{"message": {"content": "ok"}}]}

    direct = [
        {"provider": "openrouter", "model": "a:free"},
        {"provider": "openrouter", "model": "b:free"},
    ]
    monkeypatch.setattr(llm._cp, "free_pool_candidates", lambda: list(direct))
    monkeypatch.setattr(llm, "_veya_gateway_candidates", lambda: list(gateway))
    monkeypatch.setattr(llm, "_veya12_rr_call", fake_rr)
    for _ in range(3):
        resp = asyncio.run(llm._veya12_free_call([], {}))
    return seen, resp


def test_gateway_first_every_call(monkeypatch):
    gw = [{"provider": "veya-gw", "model": "veya-free", "endpoint": "http://gw/v1"}]
    seen, resp = _run(monkeypatch, gw)
    assert all(order[0] == "veya-gw" for order in seen)
    assert all(len(order) == 3 for order in seen)
    assert resp["router"]["POOL_SOURCE"].startswith("gateway+")


def test_no_gateway_keeps_direct_pool(monkeypatch):
    seen, resp = _run(monkeypatch, [])
    assert all("veya-gw" not in order and len(order) == 2 for order in seen)
    assert resp["router"]["POOL_SOURCE"] == "model-state.json:eligible"


def test_veya_gw_provider_is_not_re_resolved_as_alias(monkeypatch):
    """provider=veya-gw + model=veya-free must hit the gateway HTTP endpoint,
    not recurse into the in-process veya-free pool."""

    calls: list[str] = []

    async def boom(*_a, **_k):
        raise AssertionError("re-entered the in-process veya-free pool")

    async def fake_provider_call(client, provider, **kw):
        calls.append(":".join([provider, str(kw.get("model")), str(kw.get("endpoint"))]))
        return {"choices": [{"message": {"content": "5"}}]}

    monkeypatch.setattr(llm._cp, "veya_free_call", boom)
    monkeypatch.setattr(llm, "provider_call", fake_provider_call)
    monkeypatch.setenv("VEYA_GW_API_KEY", "x")
    resp = asyncio.run(
        llm.llm_call(
            [{"role": "user", "content": "hi"}],
            provider="veya-gw",
            model="veya-free",
            endpoint="http://127.0.0.1:8791/v1",
        )
    )
    assert resp["choices"][0]["message"]["content"] == "5"
    assert calls and calls[0].startswith("veya-gw:veya-free:http://127.0.0.1:8791")
