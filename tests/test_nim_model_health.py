"""NIM per-model health: 404/410 are model verdicts, not key verdicts.

Live landing S5/S7/S8/S9/S12 intermittently timed out at 300 s while the free
pool kept selecting retired NIM models:

* ``nvidia/nemotron-3-super-120b-a12b`` answered HTTP 404;
* ``meta/llama-3.2-11b-vision-instruct`` answered HTTP 410.

The old runner charged those failures to the *key* (``mark_failure``), retried
the same dead model on another key, and after three hits put a healthy key into
a 300 s cooldown.  A hung model was worse: it consumed the whole tier deadline,
so the healthy models behind it were never reached.

Offline: every provider call is mocked; no network, no real keys.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap
from veya.obase import canonical_proxies as cp


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


def _ok(content: str = "ok") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {},
    }


def _entry(model_id: str, latency: int = 1000) -> dict:
    return {
        "provider": "nvidia-nim",
        "canonical_proxy": "veya-nim",
        "model_id": model_id,
        "eligible": True,
        "discovered": True,
        "healthy": True,
        "credentials_valid": True,
        "endpoint_available": True,
        "model_available": True,
        "cooldown_until": None,
        "latency_ms": latency,
    }


@pytest.fixture(autouse=True)
def _clean_model_health(monkeypatch):
    cp.reset_model_health()
    monkeypatch.setattr(cp, "_nim_cursor", 0)
    yield
    cp.reset_model_health()


@pytest.fixture
def key_pool(monkeypatch, tmp_path):
    pool_file = tmp_path / "nim-key-pool"
    pool_file.write_text("k1\nk2\nk3\n", encoding="utf-8")
    monkeypatch.setattr(cp, "_NIM_KEY_POOL", pool_file)
    cp.reset_nim_key_pool()
    try:
        yield cp.nim_key_pool()
    finally:
        cp.reset_nim_key_pool()


def test_404_and_410_do_not_charge_the_key(key_pool):
    """A retired model must not cool a healthy key down (§8.3)."""
    for status in (404, 410):
        assert key_pool.mark_failure(1, status) == "model-unavailable"
        state = key_pool.states[1]
        assert state.healthy is True
        assert state.cooldown_until == 0.0
        assert state.consecutive_failures == 0

    # The key is still eligible for the next request.
    assert 1 in [key_id for key_id, _ in key_pool.next_keys(3)]


def test_retired_model_is_dropped_from_the_eligible_pool(monkeypatch):
    live, dead = "live/model", "retired/model"
    monkeypatch.setattr(
        cp,
        "_load_state",
        lambda: {
            "models": {
                "nvidia-nim:live": _entry(live, 2000),
                "nvidia-nim:dead": _entry(dead, 900),
            }
        },
    )
    assert dead in [e["model_id"] for e in cp.eligible_nim_models()]

    cp._mark_model_dead(dead)
    assert [e["model_id"] for e in cp.eligible_nim_models()] == [live]


def test_nim_call_skips_a_retired_model_after_one_attempt(monkeypatch, key_pool):
    """A 404/410 stops the key walk immediately and falls through to the next model."""
    dead, live = "retired/model", "live/model"
    monkeypatch.setattr(cp, "eligible_nim_models", lambda: [_entry(dead, 900), _entry(live, 2000)])

    calls: list[str] = []

    async def fake_provider_call(client, provider, *, model, **kwargs):
        calls.append(model)
        if model == dead:
            raise _http_error(410)
        return _ok("answer")

    import veya.obase.llm as hllm

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)

    resp = asyncio.run(cp.veya_nim_call([{"role": "user", "content": "hi"}], {"timeout": 5}))

    assert resp["choices"][0]["message"]["content"] == "answer"
    assert calls.count(dead) == 1, f"the retired model was retried on another key: {calls}"
    assert cp._model_unavailable(dead)
    # The key was not charged for the model verdict.
    assert all(state.consecutive_failures == 0 for state in key_pool.states.values())


def test_nim_call_marks_a_hung_model_slow_and_moves_on(monkeypatch, key_pool):
    """A timeout is a model verdict: try the next model, not the next key."""
    slow, live = "slow/model", "live/model"
    monkeypatch.setattr(cp, "eligible_nim_models", lambda: [_entry(slow, 900), _entry(live, 2000)])

    calls: list[str] = []

    async def fake_provider_call(client, provider, *, model, **kwargs):
        calls.append(model)
        if model == slow:
            raise httpx.ReadTimeout("hung", request=httpx.Request("POST", "https://x.test"))
        return _ok("answer")

    import veya.obase.llm as hllm

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)

    resp = asyncio.run(cp.veya_nim_call([{"role": "user", "content": "hi"}], {"timeout": 5}))

    assert resp["choices"][0]["message"]["content"] == "answer"
    assert calls == [slow, live], f"a hung model was retried on the next key: {calls}"
    assert cp._model_unavailable(slow)
