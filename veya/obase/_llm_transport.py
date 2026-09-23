"""veya/obase/_llm_transport — httpx provider transport layer.

Package-private helper for :mod:`veya.obase.llm` (obase self-contained base
layer, SPEC v3.0 §3.4). Owns the real network I/O: the OpenAI-compatible and
Anthropic Messages HTTP calls plus the single-shot / streaming provider
dispatch. Config tables and pure wire-protocol translation are injected from
the sibling ``_llm_config`` / ``_llm_protocol`` modules.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from veya.obase._llm_config import _DEFAULT_MODELS, _ENDPOINTS, get_api_key
from veya.obase._llm_protocol import (
    _is_local_or_private,
    _normalize_anthropic_response,
    _normalize_chat_endpoint,
    prepare_messages_for_provider,
)


def _is_opencode_endpoint(endpoint: str) -> bool:
    """endpoint 是否指向 opencode zen 网关（opencode-go / opencode）。"""
    host = (urlparse(endpoint).hostname or "").lower()
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def _opencode_session_headers(endpoint: str) -> dict[str, str]:
    """opencode zen 网关要求 ``x-opencode-session``。

    缺失该头时网关直接 ``400 MissingSessionID``（"cannot be routed efficiently"），
    与 key、模型名无关。该头只用于网关侧的会话路由，任何非空 id 均可；
    这里按请求生成一个随机 id，不依赖 OpenCode 客户端的会话概念。
    """
    if not _is_opencode_endpoint(endpoint):
        return {}
    return {"x-opencode-session": uuid.uuid4().hex}


def _opencode_thinking_messages(messages: list, endpoint: str) -> list:
    """zen 的 thinking 模型要求 assistant 的 tool_calls 轮次回传 reasoning_content。

    ``deepseek-v4.1-flash`` 等以 thinking 模式运行；只要历史里带 tool_calls 的
    assistant 消息缺少 ``reasoning_content``，下一次请求就会被上游直接
    ``400 The `reasoning_content` in the thinking mode must be passed back``。
    veya 的历史只保留 content/tool_calls（reasoning 不回灌上下文，以免与其它
    provider 不兼容并爆炸上下文），因此仅在 opencode.ai 端点上补空串占位。
    """
    if not _is_opencode_endpoint(endpoint):
        return messages
    patched: list = []
    for msg in messages:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and msg.get("tool_calls")
            and "reasoning_content" not in msg
        ):
            patched.append({**msg, "reasoning_content": ""})
        else:
            patched.append(msg)
    return patched


async def _call_openai_compat(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    *,
    model: str,
    messages: list,
    tools: list | None,
    max_tokens: int = 4096,
    stream: bool = False,
    temperature: float | None = None,
    tool_choice: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "model": model,
        "messages": _opencode_thinking_messages(messages, endpoint),
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(_opencode_session_headers(endpoint))
    # 私网 endpoint (容器经 docker0 网关访问宿主桥) — 目标服务按 Host 头校验
    # 本机回环 (opencodex origin_rejected): 补 Host=127.0.0.1 让桥转发后放行。
    if _is_local_or_private(endpoint) and not endpoint.startswith(
        ("http://localhost", "http://127.0.0.1", "http://0.0.0.0")
    ):
        try:
            _port = urlparse(endpoint).port or 80
            headers["Host"] = f"127.0.0.1:{_port}"
        except Exception:
            pass
    return await client.post(
        endpoint,
        headers=headers,
        json=body,
    )


async def _call_anthropic(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    model: str,
    messages: list,
    tools: list | None,
    max_tokens: int = 4096,
    stream: bool = False,
    temperature: float | None = None,
    tool_choice: str | None = None,
) -> httpx.Response:
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    system_text = "\n".join(m["content"] for m in system_msgs) or None

    ant_msgs: list[dict] = []
    for m in other_msgs:
        role = m.get("role", "user")
        if role == "tool":
            ant_msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": m.get("content", ""),
                        }
                    ],
                }
            )
        elif m.get("tool_calls"):
            content: list[dict] = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    }
                )
            ant_msgs.append({"role": "assistant", "content": content})
        else:
            ant_msgs.append({"role": role, "content": m.get("content", "")})

    body: dict[str, Any] = {"model": model, "messages": ant_msgs, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    if system_text:
        body["system"] = system_text
    if tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]
        # 调用方传的是 OpenAI 风格字符串("auto"/"none"/"required"); Anthropic
        # Messages API 要的是 {"type": ...} 对象, "required" 对应的字段名是 "any"。
        body["tool_choice"] = {
            "type": {"required": "any"}.get(tool_choice or "auto", tool_choice or "auto")
        }
    if stream:
        body["stream"] = True
    return await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
    )


async def provider_call(
    client: httpx.AsyncClient,
    provider: str,
    *,
    model: str | None,
    messages: list,
    tools: list | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """Single non-streaming completion. Returns an OpenAI-format response dict.

    ``endpoint`` overrides the built-in provider endpoint (e.g. NVIDIA NIM's
    OpenAI-compatible ``https://integrate.api.nvidia.com/v1/chat/completions``).
    ``api_key`` overrides the env lookup (Genesis 专属 Key 注入用, 物理隔离).
    """
    request_endpoint = endpoint or _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    api_key = api_key or get_api_key(provider)
    if not api_key:
        # 本地模型 (Ollama / opencodex 127.0.0.1) 无需 API Key — 与 llm_call
        # 的 local_endpoint 免 key 语义一致 (此前 provider_call 无条件拦 →
        # 本地 endpoint 也被降级成 stub)
        local = _is_local_or_private(request_endpoint)
        if not local:
            raise ValueError(f"API key not set for provider '{provider}'")
        api_key = "local"
    resolved_model = model or _DEFAULT_MODELS.get(provider, "default")
    messages = prepare_messages_for_provider(messages, provider)

    if provider == "anthropic" and not endpoint:
        resp = await _call_anthropic(
            client,
            api_key,
            model=resolved_model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_choice=tool_choice,
        )
        resp.raise_for_status()
        return _normalize_anthropic_response(resp.json())

    request_endpoint = _normalize_chat_endpoint(request_endpoint, provider)
    resp = await _call_openai_compat(
        client,
        request_endpoint,
        api_key,
        model=resolved_model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_choice=tool_choice,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def provider_stream(
    client: httpx.AsyncClient,
    provider: str,
    *,
    model: str | None,
    messages: list,
    tools: list | None = None,
    max_tokens: int = 4096,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a completion, yielding OpenAI-format delta events:
    ``{"choices": [{"delta": {"content": "..."}}]}``.
    """
    request_endpoint = endpoint or _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    api_key = api_key or get_api_key(provider)
    if not api_key:
        if not _is_local_or_private(request_endpoint):
            raise ValueError(f"API key not set for provider '{provider}'")
        api_key = "local"
    resolved_model = model or _DEFAULT_MODELS.get(provider, "default")
    messages = prepare_messages_for_provider(messages, provider)

    if provider == "anthropic":
        resp = await _call_anthropic(
            client,
            api_key,
            model=resolved_model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            stream=True,
        )
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield {"choices": [{"delta": {"content": delta["text"]}}]}
            elif event.get("type") == "message_delta":
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return

    request_endpoint = _normalize_chat_endpoint(request_endpoint, provider)
    resp = await _call_openai_compat(
        client,
        request_endpoint,
        api_key,
        model=resolved_model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        stream=True,
    )
    resp.raise_for_status()
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            return
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            yield {"choices": [{"delta": {"content": content}}]}
        if choice.get("finish_reason"):
            yield {"choices": [{"delta": {}, "finish_reason": choice["finish_reason"]}]}
