"""veya.model_routing 测试 — 统一模型 fallover / 用量跟踪 / 粘性会话 / 工具救援 (freellmapi 内化)。"""

from __future__ import annotations

import json
import time

from veya.model_routing import (
    STICKY_TTL_SECONDS,
    Quota,
    StickySession,
    UsageLedger,
    get_route,
    list_routes,
    register_route,
    rescue_tool_calls,
)

# ── 1. 统一模型 + 组内 fallover (路由注册) ───────────────────────────

def test_register_and_get_route():
    register_route("test-model", ["alpha", "beta"])
    assert get_route("test-model") == ["alpha", "beta"]
    assert "test-model" in list_routes()
    # 未注册 → 空
    assert get_route("nope") == []


def test_default_routes_present():
    routes = list_routes()
    assert "glm-4-flash" in routes
    assert routes["glm-4-flash"] == ["zhipu", "dashscope"]


def test_register_overrides():
    register_route("glm-4-flash", ["only-zhipu"])
    assert get_route("glm-4-flash") == ["only-zhipu"]
    register_route("glm-4-flash", ["zhipu", "dashscope"])  # 恢复


# ── 2. 用量跟踪 + 限额学习 ───────────────────────────────────────────

def test_ledger_counts_and_limits():
    ledger = UsageLedger()
    now = time.time()
    ledger.record("zhipu", "glm-4-flash", prompt_tokens=100, completion_tokens=50, ts=now)
    ledger.record("zhipu", "glm-4-flash", prompt_tokens=200, completion_tokens=100, ts=now)

    ok, view = ledger.check("zhipu", "glm-4-flash", quota=Quota(rpm=10), ts=now)
    assert ok
    assert view["counts"]["rpm"] == 2
    assert view["counts"]["tpm"] == 450

    # 超 RPM → 不可用
    ok, view = ledger.check("zhipu", "glm-4-flash", quota=Quota(rpm=1), ts=now)
    assert not ok
    assert "rpm" in view["over"]


def test_ledger_sliding_window():
    ledger = UsageLedger()
    now = time.time()
    ledger.record("p", "m", ts=now - 120)  # 窗口外 (RPM 60s)
    ok, view = ledger.check("p", "m", quota=Quota(rpm=1), ts=now)
    assert ok  # 窗口外的不计数
    assert view["counts"]["rpm"] == 0


def test_learn_limit_from_headers():
    ledger = UsageLedger()
    quota = ledger.learn_limit(
        "groq", "model-x",
        response_headers={"x-ratelimit-limit-rpm": "30", "x-ratelimit-limit-tpm": "6000"},
    )
    assert quota is not None
    assert quota.rpm == 30
    assert quota.tpm == 6000
    # learn 后 check 自动用学习到的限额
    _, view = ledger.check("groq", "model-x", ts=time.time())
    assert view["limits"].rpm == 30


def test_learn_limit_from_error_body():
    ledger = UsageLedger()
    quota = ledger.learn_limit(
        "groq", "model-y",
        error_body="Error 429: RPM limit reached, limit 20, remaining 0",
    )
    assert quota is not None
    assert quota.rpm == 20


# ── 3. 粘性会话 ──────────────────────────────────────────────────────

def test_sticky_lock_and_get():
    sticky = StickySession()
    sticky.lock("sess-1", "glm-4-flash", ts=1000)
    assert sticky.get("sess-1", ts=1500) == "glm-4-flash"  # TTL 内
    assert sticky.get("sess-2", ts=1500) is None  # 未锁


def test_sticky_expiry():
    sticky = StickySession(ttl=30)
    sticky.lock("sess-1", "glm-4-flash", ts=1000)
    assert sticky.get("sess-1", ts=1029) == "glm-4-flash"
    assert sticky.get("sess-1", ts=1031) is None  # 过期 → 解除


def test_sticky_clear():
    sticky = StickySession()
    sticky.lock("sess-1", "glm-4-flash", ts=1000)
    sticky.clear("sess-1")
    assert sticky.get("sess-1", ts=1000) is None
    assert sticky.snapshot() == {}


def test_default_ttl_constant():
    assert STICKY_TTL_SECONDS == 1800


# ── 4. 工具调用救援 ──────────────────────────────────────────────────

def test_rescue_code_block_single():
    content = '```json\n{"name": "get_weather", "arguments": {"city": "beijing"}}\n```'
    calls = rescue_tool_calls(content)
    assert calls is not None
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "beijing"}


def test_rescue_bare_object():
    content = '{"name": "calc", "arguments": {"a": 1, "b": 2}}'
    calls = rescue_tool_calls(content)
    assert calls is not None
    assert calls[0]["function"]["name"] == "calc"


def test_rescue_tool_calls_wrapper():
    content = '{"tool_calls": [{"name": "f1", "arguments": {"x": 1}}, {"name": "f2", "arguments": {}}]}'
    calls = rescue_tool_calls(content)
    assert calls is not None
    assert len(calls) == 2
    assert calls[1]["function"]["name"] == "f2"


def test_rescue_function_wrapper():
    content = '```json\n{"function": {"name": "f", "arguments": {"k": "v"}}}\n```'
    calls = rescue_tool_calls(content)
    assert calls is not None
    assert calls[0]["function"]["name"] == "f"


def test_rescue_returns_none_for_plain_text():
    assert rescue_tool_calls("这是普通回答, 没有工具调用。") is None
    assert rescue_tool_calls("") is None
    assert rescue_tool_calls("```json\n{\"not\": \"a tool\"}\n```") is None


# ── llm_call_routed 组合 (mock veya.llm.llm_call) ────────────────────

def test_routed_fallover_on_failure(monkeypatch):
    import veya.llm as llm
    from veya.llm import llm_call_routed
    from veya.model_routing import UsageLedger

    calls = []

    async def fake_llm_call(messages, **kw):
        calls.append(kw.get("provider"))
        if kw.get("provider") == "zhipu":
            raise RuntimeError("429 rate limited")
        return {"choices": [{"message": {"content": "ok from dashscope"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr(llm, "llm_call", fake_llm_call)
    ledger = UsageLedger()

    result = __import__("asyncio").run(llm_call_routed(
        [{"role": "user", "content": "hi"}],
        logical_model="glm-4-flash", ledger=ledger,
    ))
    assert result["_routed"]["provider"] == "dashscope"  # fallover 成功
    assert [a["provider"] for a in result["_routed"]["attempts"]] == ["zhipu"]
    # 用量已记录 (成功 provider)
    _, view = ledger.check("dashscope", "glm-4-flash")
    assert view["counts"]["tpm"] == 15


def test_routed_all_fail_returns_error_trail(monkeypatch):
    import veya.llm as llm
    from veya.llm import llm_call_routed

    async def fake_llm_call(messages, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm, "llm_call", fake_llm_call)
    result = __import__("asyncio").run(llm_call_routed(
        [{"role": "user", "content": "hi"}], logical_model="glm-4-flash"
    ))
    assert result["_error"] is True
    assert len(result["attempts"]) == 2  # zhipu + dashscope
    assert result["logical_model"] == "glm-4-flash"


def test_routed_sticky_session(monkeypatch):
    import veya.llm as llm
    from veya.llm import llm_call_routed
    from veya.model_routing import StickySession

    providers_called = []

    async def fake_llm_call(messages, **kw):
        providers_called.append(kw.get("provider"))
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    monkeypatch.setattr(llm, "llm_call", fake_llm_call)
    sticky = StickySession()

    # 第一次: 路由选 zhipu 并锁定
    __import__("asyncio").run(llm_call_routed(
        [{"role": "user", "content": "hi"}], logical_model="glm-4-flash",
        session_id="sess-a", sticky=sticky,
    ))
    assert providers_called == ["zhipu"]
    # 第二次: 即使传不同模型, 粘性锁定 glm-4-flash
    __import__("asyncio").run(llm_call_routed(
        [{"role": "user", "content": "hi2"}], logical_model="deepseek-chat",
        session_id="sess-a", sticky=sticky,
    ))
    assert providers_called == ["zhipu", "zhipu"]  # 仍走 glm-4-flash


def test_routed_rescues_tool_calls(monkeypatch):
    import veya.llm as llm
    from veya.llm import llm_call_routed

    async def fake_llm_call(messages, **kw):
        return {"choices": [{"message": {
            "content": '```json\n{"name": "get_weather", "arguments": {"city": "x"}}\n```'}}],
            "usage": {}}

    monkeypatch.setattr(llm, "llm_call", fake_llm_call)
    result = __import__("asyncio").run(llm_call_routed(
        [{"role": "user", "content": "weather?"}], logical_model="glm-4-flash"
    ))
    assert result["_rescue"] is True
    tool_calls = result["choices"][0]["message"]["tool_calls"]
    assert tool_calls[0]["function"]["name"] == "get_weather"
