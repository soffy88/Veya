"""G1 — tests for the canonical LLM provider layer (veya/llm.py).

Covers:
- config resolution (explicit > env > defaults)
- OpenAI-compatible completion via mocked httpx transport
- Anthropic response normalization (incl. tool_use blocks)
- streaming (OpenAI SSE + Anthropic SSE) via mocked transport
- cost calculation
- stub fallback when no API key is configured
- backward-compat delegation from veya.compat
"""

from __future__ import annotations

import json

import httpx
import pytest

from veya import llm as hllm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_ok_response(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("POST", "http://x"))


def _make_openai_completion(content: str = "Hello from mock") -> dict:
    return {
        "id": "chatcmpl-mock",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_anthropic_completion(content: str = "Hi from claude") -> dict:
    return {
        "content": [{"type": "text", "text": content}],
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "stop_reason": "end_turn",
    }


def _make_anthropic_tool_use() -> dict:
    return {
        "content": [
            {"type": "text", "text": "I'll search."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search",
                "input": {"query": "veya"},
            },
        ],
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }


class _Transport:
    """httpx.MockTransport-compatible handler factory."""

    def __init__(self, handler):
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_get_provider_config_explicit_wins(monkeypatch):
    monkeypatch.delenv("VEYA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VEYA_LLM_MODEL", raising=False)
    provider, model = hllm.get_provider_config(
        {"provider": "openai", "model": "gpt-4o"}, provider="anthropic"
    )
    assert provider == "anthropic"  # explicit kwarg beats config dict
    assert model == "gpt-4o"


def test_get_provider_config_env_and_defaults(monkeypatch):
    monkeypatch.setattr(hllm, "_user_llm_config", lambda: {})  # 隔离宿主用户配置
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    monkeypatch.delenv("VEYA_LLM_MODEL", raising=False)
    provider, model = hllm.get_provider_config(None)
    assert provider == "openai"
    assert model == hllm._DEFAULT_MODELS["openai"]

    monkeypatch.delenv("VEYA_LLM_PROVIDER")
    provider, _ = hllm.get_provider_config(None)
    assert provider == hllm._DEFAULT_PROVIDER == "dashscope"


def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    assert hllm.get_api_key("openai") == "sk-test-123"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert hllm.get_api_key("openai") == ""


def test_get_api_key_from_config():
    assert (
        hllm.get_api_key("openai", {"providers": {"openai": {"api_key": "cfg-key"}}}) == "cfg-key"
    )


# ---------------------------------------------------------------------------
# provider_call (non-streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_call_openai():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["stream"] is False
        assert request.headers["Authorization"] == "Bearer sk-test-123"
        return _openai_ok_response(_make_openai_completion())

    client = _client_with(handler)
    with monkeypatch_env("OPENAI_API_KEY", "sk-test-123"):
        data = await hllm.provider_call(
            client, "openai", model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    assert data["choices"][0]["message"]["content"] == "Hello from mock"
    assert data["usage"]["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_provider_call_anthropic_normalization():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert json.loads(request.content)["max_tokens"] == 4096
        return _openai_ok_response(_make_anthropic_completion())

    client = _client_with(handler)
    with monkeypatch_env("ANTHROPIC_API_KEY", "sk-ant-test"):
        data = await hllm.provider_call(
            client,
            "anthropic",
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert data["choices"][0]["message"]["content"] == "Hi from claude"
    assert data["usage"]["prompt_tokens"] == 12
    assert data["usage"]["completion_tokens"] == 7


@pytest.mark.asyncio
async def test_provider_call_anthropic_tool_use():
    async def handler(request: httpx.Request) -> httpx.Response:
        return _openai_ok_response(_make_anthropic_tool_use())

    client = _client_with(handler)
    with monkeypatch_env("ANTHROPIC_API_KEY", "sk-ant-test"):
        data = await hllm.provider_call(
            client,
            "anthropic",
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "search veya"}],
            tools=[
                {
                    "function": {
                        "name": "search",
                        "description": "s",
                        "parameters": {"type": "object", "properties": {}},
                    }
                }
            ],
        )
    msg = data["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"query": "veya"}


@pytest.mark.asyncio
async def test_provider_call_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client_with(lambda r: _openai_ok_response({}))
    with pytest.raises(ValueError, match="API key not set"):
        await hllm.provider_call(client, "openai", model="gpt-4o-mini", messages=[])


@pytest.mark.asyncio
async def test_provider_call_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, text="boom")

    client = _client_with(handler)
    with monkeypatch_env("OPENAI_API_KEY", "sk-test-123"), pytest.raises(httpx.HTTPStatusError):
        await hllm.provider_call(client, "openai", model="gpt-4o-mini", messages=[])


# ---------------------------------------------------------------------------
# provider_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_stream_openai_sse():
    sse = (
        'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=sse, headers={"content-type": "text/event-stream"}, request=request
        )

    client = _client_with(handler)
    with monkeypatch_env("OPENAI_API_KEY", "sk-test-123"):
        events = [
            ev
            async for ev in hllm.provider_stream(client, "openai", model="gpt-4o-mini", messages=[])
        ]
    texts = [
        ev["choices"][0]["delta"]["content"]
        for ev in events
        if "content" in ev["choices"][0]["delta"]
    ]
    assert texts == ["Hello", " world"]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_provider_stream_anthropic_sse():
    sse = (
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi "}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"there"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=sse, headers={"content-type": "text/event-stream"}, request=request
        )

    client = _client_with(handler)
    with monkeypatch_env("ANTHROPIC_API_KEY", "sk-ant-test"):
        events = [
            ev
            async for ev in hllm.provider_stream(
                client, "anthropic", model="claude-haiku-4-5-20251001", messages=[]
            )
        ]
    texts = [
        ev["choices"][0]["delta"]["content"]
        for ev in events
        if "content" in ev["choices"][0]["delta"]
    ]
    assert texts == ["Hi ", "there"]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Framework-level llm_call / llm_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_call_stub_fallback(monkeypatch):
    monkeypatch.setattr(hllm, "_user_llm_config", lambda: {})  # 隔离宿主用户配置
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    result = await hllm.llm_call([{"role": "user", "content": "hi"}])
    assert "shim response" in result["choices"][0]["message"]["content"]
    assert result["usage"]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_llm_call_real_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    original_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return _openai_ok_response(_make_openai_completion("Real answer"))

    monkeypatch.setattr(
        hllm.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    result = await hllm.llm_call([{"role": "user", "content": "hi"}])
    assert result["choices"][0]["message"]["content"] == "Real answer"


@pytest.mark.asyncio
async def test_llm_stream_stub_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    events = [ev async for ev in hllm.llm_stream([{"role": "user", "content": "hi"}])]
    text = "".join(ev["choices"][0]["delta"].get("content", "") for ev in events)
    assert "shim" in text
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_llm_stream_real_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    sse = (
        'data: {"choices":[{"delta":{"content":"streamed "},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=sse, headers={"content-type": "text/event-stream"}, request=request
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        hllm.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    events = [ev async for ev in hllm.llm_stream([{"role": "user", "content": "hi"}])]
    text = "".join(ev["choices"][0]["delta"].get("content", "") for ev in events)
    assert text == "streamed "


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_calc_cost():
    assert hllm.calc_cost(
        "openai", {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    ) == pytest.approx(0.5)
    assert hllm.calc_cost(
        "openai", {"prompt_tokens": 0, "completion_tokens": 1_000_000}
    ) == pytest.approx(1.5)
    # Anthropic-style usage keys
    assert hllm.calc_cost(
        "anthropic", {"input_tokens": 1_000_000, "output_tokens": 0}
    ) == pytest.approx(3.0)
    assert hllm.calc_cost("unknown-provider", {"prompt_tokens": 100}) == 0.0


# ---------------------------------------------------------------------------
# Backward compat delegation (veya.compat.llm_call / llm_stream)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compat_llm_call_delegates(monkeypatch):
    from veya import compat

    monkeypatch.setattr(hllm, "_user_llm_config", lambda: {})  # 隔离宿主用户配置
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    result = await compat.llm_call(
        [{"role": "user", "content": "hi"}], default_content="custom fallback"
    )
    assert result["choices"][0]["message"]["content"] == "custom fallback"


@pytest.mark.asyncio
async def test_compat_llm_stream_delegates(monkeypatch):
    from veya import compat

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "openai")
    events = [
        ev
        async for ev in compat.llm_stream(
            [{"role": "user", "content": "hi"}], default_content="stream fallback"
        )
    ]
    text = "".join(ev["choices"][0]["delta"].get("content", "") for ev in events)
    assert "fallback" in text


# ---------------------------------------------------------------------------
# server.providers backward-compat re-export
# ---------------------------------------------------------------------------


def test_server_providers_reexport():
    import server.providers as sp

    assert sp.provider_call is hllm.provider_call
    assert sp.calc_cost is hllm.calc_cost
    assert sp._get_provider({"provider": "openai"}) == "openai"


# ---------------------------------------------------------------------------
# tiny context-manager helper for env vars
# ---------------------------------------------------------------------------


class monkeypatch_env:
    """Minimal context manager to set/restore a single env var."""

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self._old = None
        self._had = False

    def __enter__(self):
        import os

        self._had = self.key in os.environ
        if self._had:
            self._old = os.environ[self.key]
        os.environ[self.key] = self.value

    def __exit__(self, *exc):
        import os

        if self._had:
            os.environ[self.key] = self._old
        else:
            os.environ.pop(self.key, None)


# ---------------------------------------------------------------------------
# 空 tool_calls 清洗 (DeepSeek 400: messages[i].tool_calls 不能是空数组)
# ---------------------------------------------------------------------------


def test_prepare_messages_strips_empty_tool_calls():
    from veya.llm import prepare_messages_for_provider

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "thinking", "tool_calls": []},  # ← 400 源头
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    for provider in ("deepseek", "openai", "dashscope", "anthropic"):
        out = prepare_messages_for_provider(msgs, provider)
        assert all(m.get("tool_calls") != [] for m in out), f"{provider}: 空数组残留"
        assert out[1] == {"role": "assistant", "content": "thinking"}  # 键被剥除
        # 非空 tool_calls 必须保留
        msgs2 = [
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
            }
        ]
        out2 = prepare_messages_for_provider(msgs2, provider)
        assert out2[0]["tool_calls"], f"{provider}: 有效 tool_calls 被误删"


def test_prepare_messages_does_not_mutate_input():
    from veya.llm import prepare_messages_for_provider

    msgs = [{"role": "assistant", "content": "x", "tool_calls": []}]
    snapshot = list(msgs)
    prepare_messages_for_provider(msgs, "deepseek")
    assert msgs == snapshot, "输入消息不得被原地修改"


# ---------------------------------------------------------------------------
# endpoint 归一化 (custom provider base URL → /chat/completions)
# ---------------------------------------------------------------------------


def test_normalize_chat_endpoint():
    from veya.llm import _normalize_chat_endpoint

    # base URL 形态 → 补 /chat/completions
    assert (
        _normalize_chat_endpoint("https://token.example.com/v1", "custom")
        == "https://token.example.com/v1/chat/completions"
    )
    assert (
        _normalize_chat_endpoint("https://host.example.com", "custom")
        == "https://host.example.com/chat/completions"
    )
    # 完整 URL (内置形态) → 原样
    assert (
        _normalize_chat_endpoint("https://api.deepseek.com/v1/chat/completions", "deepseek")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        _normalize_chat_endpoint("https://open.bigmodel.cn/api/paas/v4/chat/completions", "zhipu")
        == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    )
    # 非法 URL → 明确报错 (不再 httpx 相对 URL 404 迷惑)
    with pytest.raises(ValueError, match="完整 URL"):
        _normalize_chat_endpoint("/v1", "custom")
    with pytest.raises(ValueError, match="未配置"):
        _normalize_chat_endpoint("", "custom")


# ---------------------------------------------------------------------------
# 双通道: 自定义 provider 代理兜底
# ---------------------------------------------------------------------------


def test_custom_proxy_url_internal_providers_none(monkeypatch):
    from veya.llm import _custom_proxy_url

    monkeypatch.setattr("veya.llm._in_container", lambda: True)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=0.5: (_ for _ in ()).throw(OSError())
    )
    # 内置 provider 不走代理
    assert _custom_proxy_url("dashscope") is None
    assert _custom_proxy_url("openai") is None


def test_custom_proxy_url_bridge_detected(monkeypatch):
    from veya.llm import _custom_proxy_url

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("veya.llm._in_container", lambda: True)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0.5: _Resp())
    assert _custom_proxy_url("custom-tokenrouter") == "http://192.168.16.1:17890"
    # 宿主 (非容器) 不走代理
    monkeypatch.setattr("veya.llm._in_container", lambda: False)
    assert _custom_proxy_url("custom-tokenrouter") is None


# ── veya1.1 别名: opencode-go 空回复 → gpt-5.6-luna 本地兜底 ─────────


def test_aliased_llm_falls_back_to_frontier_on_empty(monkeypatch):
    """opencode-go 返回空/'None' 时自动降级本地 frontier, 绝不静默。"""
    import asyncio
    import os

    from veya import llm as hllm

    calls: list[str] = []
    monkeypatch.setattr("os.environ", {**os.environ, "OPENCODE_API_KEY": "sk-test"})

    async def _no_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(hllm.asyncio, "sleep", _no_sleep)

    async def flaky_provider(client, provider, **kw):
        # 底层网络层: opencode-go 持续空回复, openai (gpt-5.6-luna) 正常
        calls.append(kw.get("model") or "?")
        if provider == "opencode-go":
            return {"choices": [{"message": {"role": "assistant", "content": "None"}}], "usage": {}}
        if provider == "openai":
            return {
                "choices": [{"message": {"role": "assistant", "content": "兜底成功回复"}}],
                "usage": {},
            }
        raise AssertionError(f"unexpected provider {provider}")

    monkeypatch.setattr(hllm, "provider_call", flaky_provider)
    resp = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你是谁你能做什么"}], provider="veya1.1", model="veya1.1"
        )
    )
    # 先走 free 池候选 (deepseek/kimi-k2.7-code), 空回复后落到 gpt-5.6-luna 兜底
    assert calls[-1] == "gpt-5.6-luna", f"最后必须兜底到 gpt-5.6-luna, 实际 {calls}"
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    assert msg.get("content") == "兜底成功回复"

    # 绝不静默: 兜底 (gpt-5.6-luna) 也失败时返回结构化错误而非空白
    async def all_empty(client, provider, **kw):
        calls.append(kw.get("model") or "?")
        return {"choices": [{"message": {"role": "assistant", "content": ""}}], "usage": {}}

    monkeypatch.setattr(hllm, "provider_call", all_empty)
    resp2 = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你是谁你能做什么"}], provider="veya1.1", model="veya1.1"
        )
    )
    assert resp2.get("error") is True
    assert str(
        ((resp2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
