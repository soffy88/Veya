"""P1-1 — the default veya1.2 brain tier chain must reach its live tier.

Offline. Every provider call is mocked; no network, no real keys.

Covers the three defects the 2026-10-22 production acceptance found:

- a dead tier (401/403 or a refused connection) no longer burns the tier
  timeout and its retries — the pool fast-breaks to the next tier;
- every tier carries an independent, short, env-configurable timeout so the
  120 s MasterAgent budget always leaves room for the live tier;
- a provider without a credential never enters the pool at all.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap
from veya import llm as hllm
from veya.obase import brain_breaker as bb


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


def _connect_error() -> httpx.ConnectError:
    request = httpx.Request("POST", "http://127.0.0.1:8791/v1/chat/completions")
    return httpx.ConnectError("connection refused", request=request)


def _ok(content: str = "answer") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {},
    }


@pytest.fixture
def pool_env(monkeypatch):
    """A deterministic three-tier pool: dead auth tier, dead connect tier, live."""
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("NIM_KEY_POOL", "nvapi-test")
    monkeypatch.setenv("VEYA_BRAIN_BREAKER", "1")
    bb.reset()
    yield
    bb.reset()


def test_fast_break_classifies_auth_and_connect_only():
    assert hllm._fast_break_kind(_http_error(401)) == "auth"
    assert hllm._fast_break_kind(_http_error(403)) == "auth"
    assert hllm._fast_break_kind(_connect_error()) == "connect"
    # A 429 or a read timeout must NOT fast-break: they are transient.
    assert hllm._fast_break_kind(_http_error(429)) == ""
    assert hllm._fast_break_kind(httpx.ReadTimeout("slow")) == ""
    assert hllm._fast_break(_http_error(401)) is True
    assert hllm._fast_break(httpx.ReadTimeout("slow")) is False


def test_tier_timeout_is_short_and_env_configurable(monkeypatch):
    monkeypatch.delenv("VEYA_TIER_TIMEOUT_S", raising=False)
    assert hllm._tier_timeout_s({}) == hllm._DEFAULT_TIER_TIMEOUT_S
    assert hllm._tier_timeout_s({"timeout": "9"}) == 9.0
    # The operator env knob wins over the shipped per-tier default.
    monkeypatch.setenv("VEYA_TIER_TIMEOUT_S", "25")
    assert hllm._tier_timeout_s({}) == 25.0
    assert hllm._tier_timeout_s({"timeout": "9"}) == 25.0


def test_nested_proxy_tier_is_bounded_by_a_whole_tier_deadline(pool_env, monkeypatch):
    """A tier that walks several upstream models internally cannot exceed its slice.

    veya-nim tries several NIM models inside one call, so a per-request timeout
    alone let it consume far more than one tier's worth of the turn budget.
    """

    original = hllm.llm_call

    async def slow_tier(messages, **kwargs):
        provider = kwargs.get("provider")
        if provider == "veya-nim":
            await asyncio.sleep(30)
        if provider == "opencode-go":
            raise _http_error(401)
        return _ok("live")

    monkeypatch.setattr(hllm, "llm_call", slow_tier)
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    monkeypatch.setenv("VEYA_TIER_TIMEOUT_S", "0.2")

    import time as _time

    t0 = _time.monotonic()
    result = asyncio.run(
        original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2")
    )
    elapsed = _time.monotonic() - t0
    assert elapsed < 5, f"nested tier was not bounded by its deadline ({elapsed:.1f}s)"
    assert result["router"]["provider"] == "openrouter"


def test_timed_out_tier_is_not_retried_in_the_same_turn(pool_env, monkeypatch):
    """One timing-out tier must not pay its timeout on every retry round.

    The three retry rounds were what pushed a multi-round turn past 120s even
    after each tier got a short timeout.
    """
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    monkeypatch.setenv("VEYA_TIER_TIMEOUT_S", "0.05")
    original = hllm.llm_call
    nim_attempts: list[int] = []

    async def fake(messages, **kwargs):
        provider = kwargs.get("provider", "")
        if provider == "veya-nim":
            nim_attempts.append(1)
            await asyncio.sleep(5)  # exceeds the tier deadline
        if provider == "opencode-go":
            raise _http_error(401)
        return _ok("live")

    monkeypatch.setattr(hllm, "llm_call", fake)
    result = asyncio.run(
        original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2")
    )
    assert len(nim_attempts) == 1, "a timed-out tier was retried in the same turn"
    assert result["router"]["provider"] == "openrouter"


def test_dead_nested_proxy_tier_is_not_reprobed_in_the_same_turn(pool_env, monkeypatch):
    """veya-nim swallows its own upstream failures and answers error=True.

    Re-probing it in the next retry round paid another full tier timeout on
    every round of a multi-round turn.
    """
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    original = hllm.llm_call
    nim_calls: list[int] = []

    async def fake(messages, **kwargs):
        provider = kwargs.get("provider", "")
        if provider == "opencode-go":
            raise _http_error(401)
        if provider == "veya-nim":
            nim_calls.append(1)
            return {"choices": [{"message": {"content": "nested proxy failed"}}], "error": True}
        return _ok("live")

    monkeypatch.setattr(hllm, "llm_call", fake)
    result = asyncio.run(
        original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2")
    )
    assert len(nim_calls) == 1, "the dead nested tier was re-probed in this turn"
    assert result["router"]["provider"] == "openrouter"


def test_turn_budget_stops_retry_rounds(pool_env, monkeypatch):
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    monkeypatch.setenv("VEYA_TIER_TIMEOUT_S", "0.05")
    monkeypatch.setenv("VEYA_BRAIN_TURN_BUDGET_S", "0.01")
    original = hllm.llm_call

    async def empty(messages, **kwargs):
        return {"choices": [{"message": {"content": ""}}], "usage": {}}

    async def no_frontier(*_a, **_k):
        return None

    monkeypatch.setattr(hllm, "llm_call", empty)
    monkeypatch.setattr(hllm, "_frontier_fallback", no_frontier)
    # No live tier: the pool must give up on its turn budget, not spin rounds.
    import time as _time

    t0 = _time.monotonic()
    result = asyncio.run(
        original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2")
    )
    assert _time.monotonic() - t0 < 2, "turn budget did not stop the retry rounds"
    assert result.get("error") is True


def test_default_pool_tiers_all_carry_a_short_timeout():
    for cand in hllm._VEYA12_DEFAULT_POOL:
        assert 0 < hllm._tier_timeout_s(cand) <= 30, cand


def test_dead_tiers_are_skipped_without_retrying(pool_env, monkeypatch):
    """401 then connection-refused must not consume the whole turn budget.

    Before the fix the pool walked 3 retry rounds over every tier, so the
    answer only arrived after the 120 s MasterAgent deadline.
    """
    seen: list[tuple[str, float]] = []
    original = hllm.llm_call

    async def fake_llm_call(messages, **kwargs):
        provider = kwargs.get("provider", "")
        seen.append((provider, kwargs.get("timeout", 0.0)))
        if provider == "opencode-go":
            raise _http_error(401)
        if provider == "veya-nim":
            raise _connect_error()
        return _ok("live-tier-answer")

    monkeypatch.setattr(hllm, "llm_call", fake_llm_call)
    # Only the three built-in tiers; no pi-derived candidates.
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)

    result = asyncio.run(
        original(
            [{"role": "user", "content": "hi"}],
            provider="veya1.2",
            model="veya1.2",
        )
    )
    providers = [p for p, _ in seen]
    assert "opencode-go" in providers and "veya-nim" in providers
    assert "openrouter" in providers
    # Each dead tier is attempted exactly once — no retry rounds, no waiting.
    assert providers.count("opencode-go") == 1
    assert providers.count("veya-nim") == 1
    # Every tier ran with the short per-tier timeout.
    assert all(timeout <= 30 for _, timeout in seen)
    assert result["choices"][0]["message"]["content"] == "live-tier-answer"
    assert result["router"]["provider"] == "openrouter"


def test_broken_tiers_open_a_breaker_for_the_next_call(pool_env, monkeypatch):
    """A 401/connect-refused tier opens a breaker, so the next call skips it."""
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    monkeypatch.setattr("veya.obase._llm_config._opencode_go_key_from_auth", lambda: "sk-auth")
    original = hllm.llm_call
    calls: list[str] = []

    async def fake(messages, **kwargs):
        provider = kwargs.get("provider", "")
        calls.append(provider)
        if provider == "opencode-go":
            raise _http_error(401)
        if provider == "veya-nim":
            # The pool has no NIM key pool here; keep the walk deterministic.
            raise _connect_error()
        return _ok()

    monkeypatch.setattr(hllm, "llm_call", fake)
    asyncio.run(original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2"))
    assert "opencode-go" in calls
    assert bb.breaker_open("opencode-go") is True

    # The second call skips the broken tier entirely: re-probing it on every
    # call is what re-paid the dead tier's timeout on each round.
    calls.clear()
    result = asyncio.run(
        original([{"role": "user", "content": "hi"}], provider="veya1.2", model="veya1.2")
    )
    assert "opencode-go" not in calls
    assert result["router"]["provider"] == "openrouter"
    assert result["choices"][0]["message"]["content"] == "answer"


def test_keyless_provider_never_enters_the_pool(pool_env, monkeypatch):
    """No key (env, auth.json, or key-pool file) → the tier is not in the pool."""
    monkeypatch.setenv("OPENCODE_API_KEY", "")
    monkeypatch.setenv("NIM_KEY_POOL", "")
    # No opencode auth.json fallback and no NIM key-pool file on this host.
    monkeypatch.setattr("veya.obase._llm_config._opencode_go_key_from_auth", lambda: "")
    monkeypatch.setattr(hllm._cp, "_NIM_KEY_POOL", type("P", (), {"is_file": lambda self: False})())
    hllm._cp.reset_nim_key_pool()
    monkeypatch.setattr(hllm, "_workbuddy_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_veya_gateway_candidates", lambda: [])
    monkeypatch.setattr(hllm, "_frognano_candidate", lambda: None)
    try:
        providers = [c["provider"] for c in hllm._veya12_pool()]
        assert "veya-nim" not in providers, "a keyless NIM tier must be dropped"
        assert "opencode-go" not in providers, "a keyless opencode-go tier must be dropped"
        assert "openrouter" in providers
    finally:
        hllm._cp.reset_nim_key_pool()


def test_gateway_tier_uses_tier_timeout_not_45s(monkeypatch):
    """A dead host gateway must cost a tier timeout, not 45 s."""
    from veya.obase._llm_config import pi_provider_entry

    monkeypatch.setattr(
        "veya.obase._llm_config.pi_provider_entry",
        lambda name: (
            {
                "baseUrl": "http://127.0.0.1:8791/v1",
                "apiKey": "k",
                "models": [{"id": "veya-free"}],
            }
            if name == "veya"
            else pi_provider_entry(name)
        ),
    )
    candidates = hllm._veya_gateway_candidates()
    assert candidates and candidates[0]["provider"] == "veya-gw"
    assert hllm._tier_timeout_s(candidates[0]) <= 30
