"""
veya/llm.py — canonical multi-provider LLM client

Consolidates the provider layer (previously split across ``server/providers.py``
and the stub ``llm_call``/``llm_stream`` in ``veya/compat.py``) into a single
canonical implementation supporting:

- Non-streaming chat completion  (OpenAI-format response dict)
- Streaming chat completion     (SSE parsed to OpenAI delta events)
- Tool calling                  (OpenAI-compatible + Anthropic Messages API)
- Cost estimation               (approximate USD per provider)
- Graceful stub fallback        (when no API key is configured)

Providers: ``dashscope`` (qwen-plus), ``anthropic`` (claude-*), ``openai`` (gpt-*).

Selection order: ``config["provider"]`` > ``VEYA_LLM_PROVIDER`` env > ``dashscope``.
API keys are read from ``{PROVIDER}_API_KEY`` env vars (or ``config["providers"]``).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Approximate pricing in USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "dashscope": (0.4, 1.2),
    "anthropic": (3.0, 15.0),
    "openai": (0.5, 1.5),
    "deepseek": (0.27, 1.1),
    "openrouter": (0.15, 0.6),
    "moonshot": (0.2, 2.0),
    "zhipu": (0.1, 0.1),
}

_DEFAULT_MODELS: dict[str, str] = {
    "dashscope": "qwen-plus",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "openrouter": "openai/gpt-4o-mini",
    "moonshot": "moonshot-v1-8k",
    "zhipu": "glm-4-flash",
}

_ENDPOINTS: dict[str, str] = {
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "moonshot": "https://api.moonshot.cn/v1/chat/completions",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
}

_API_KEY_ENV: dict[str, str] = {
    "dashscope": "DASHSCOPE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
}

_DEFAULT_PROVIDER = "dashscope"


def get_provider_config(
    config: dict | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Resolve (provider, model) from explicit args → config → env → defaults."""
    p = provider or (config or {}).get("provider")
    if not p:
        p = os.environ.get("VEYA_LLM_PROVIDER", _DEFAULT_PROVIDER)
    p = str(p).lower()
    m = model or (config or {}).get("model") or os.environ.get("VEYA_LLM_MODEL")
    if not m:
        m = _DEFAULT_MODELS.get(p, "default")
    return p, str(m)


def get_api_key(provider: str, config: dict | None = None) -> str:
    """Return the API key for a provider (env var > config dict)."""
    providers_cfg = (config or {}).get("providers") or {}
    if isinstance(providers_cfg, dict):
        key = providers_cfg.get(provider)
        if isinstance(key, dict):
            key = key.get("api_key")
        if key:
            return str(key)
    env_name = _API_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")
    return os.environ.get(env_name, "")


def calc_cost(provider: str, usage: dict) -> float:
    """Approximate cost in USD for a usage dict (OpenAI or Anthropic keys)."""
    in_price, out_price = _PRICING.get(provider, (0.0, 0.0))
    in_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    out_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    return in_tokens * in_price / 1_000_000 + out_tokens * out_price / 1_000_000


# ---------------------------------------------------------------------------
# Low-level provider calls
# ---------------------------------------------------------------------------


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
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    return await client.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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


def prepare_messages_for_provider(messages: list, provider: str) -> list:
    """Normalize OpenAI-style messages (plain text or content blocks) for a provider.

    G12 multimodal: ``openai``/``dashscope`` accept content-block lists natively
    (``[{"type": "text", ...}, {"type": "image_url", ...}]``) and pass through
    unchanged. ``anthropic`` needs ``image_url`` blocks converted to its native
    ``{"type": "image", "source": {...}}`` format and list content wrapped as
    text blocks.
    """
    if provider != "anthropic":
        return messages
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and msg.get("role") in ("user", "assistant"):
            msg = dict(msg)
            msg["content"] = _to_anthropic_content_blocks(content)
        out.append(msg)
    return out


def _parse_image_url(url: str) -> tuple[str | None, str | None]:
    """Split a data URI into ``(media_type, base64_data)``; plain URLs → (None, None)."""
    if isinstance(url, str) and url.startswith("data:"):
        header, _, payload = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return media_type, payload
    return None, None


def _to_anthropic_content_blocks(content: list) -> list[dict]:
    """Convert OpenAI-style content blocks → Anthropic content blocks (G12).

    - ``{"type": "text", ...}`` passes through
    - ``{"type": "image_url", ...}`` → ``{"type": "image", "source": {...}}``
      (data URI → base64; plain URL → url source)
    - unknown blocks pass through untouched
    """
    blocks: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append({"type": "text", "text": str(block)})
            continue
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image_url":
            url = block.get("image_url") or {}
            if isinstance(url, dict):
                url = url.get("url", "")
            media_type, data = _parse_image_url(str(url))
            if data is not None:
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    }
                )
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": str(url)}})
        else:
            blocks.append(block)
    return blocks


def _normalize_anthropic_response(data: dict) -> dict:
    """Normalize an Anthropic Messages response to OpenAI format."""
    content_text = ""
    tool_calls: list[dict] = []
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            content_text = blk.get("text", "")
        elif blk.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": blk.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": blk["name"],
                        "arguments": json.dumps(blk.get("input", {})),
                    },
                }
            )
    usage = data.get("usage", {})
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content_text or None,
                    "tool_calls": tool_calls or None,
                }
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
        },
    }


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
    api_key = api_key or get_api_key(provider)
    if not api_key:
        raise ValueError(f"API key not set for provider '{provider}'")
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

    endpoint = endpoint or _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    resp = await _call_openai_compat(
        client,
        endpoint,
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
) -> AsyncIterator[dict[str, Any]]:
    """Stream a completion, yielding OpenAI-format delta events:
    ``{"choices": [{"delta": {"content": "..."}}]}``.
    """
    api_key = get_api_key(provider)
    if not api_key:
        raise ValueError(f"API key not set for provider '{provider}'")
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

    endpoint = _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    resp = await _call_openai_compat(
        client,
        endpoint,
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


# ---------------------------------------------------------------------------
# Framework-level entry points (used by compat, commands, TUI, routes)
# ---------------------------------------------------------------------------

_STUB_CONTENT = "LLM provider not configured — this is a shim response."


async def llm_call(messages: list[dict], **kwargs: Any) -> dict:
    """Non-streaming chat completion.

    Resolves provider/model from ``kwargs`` (``config``/``provider``/``model``),
    ``VEYA_LLM_PROVIDER``/``VEYA_LLM_MODEL`` env, or defaults. Falls back to
    a stub response when no API key is configured (keeps offline tests green).
    """
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    config = kwargs.get("config") or {}
    # 自定义 endpoint: 顶层 kwarg > config["endpoints"][provider] > config["base_url"](NVIDIA NIM 等)
    endpoint = (
        kwargs.get("endpoint")
        or (config.get("endpoints") or {}).get(provider)
        or config.get("base_url")
        or os.environ.get("VEYA_LLM_ENDPOINT")
    )
    # 本地模型 (Ollama 等) 无需 API Key —— 有本地 endpoint 时跳过 key 检查
    local_endpoint = bool(endpoint) and (
        endpoint.startswith("http://localhost")
        or endpoint.startswith("http://127.0.0.1")
        or endpoint.startswith("http://0.0.0.0")
    )
    if not get_api_key(provider, kwargs.get("config")) and not local_endpoint:
        content = kwargs.get("default_content", _STUB_CONTENT)
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    timeout = kwargs.get("timeout", 120.0)
    tools = kwargs.get("tools")
    max_tokens = kwargs.get("max_tokens", 4096)
    temperature = kwargs.get("temperature")
    tool_choice = kwargs.get("tool_choice")
    # 专属 Key 注入: config["providers"][provider] 优先于环境变量(Genesis 物理隔离)
    api_key = get_api_key(provider, config)
    retries = int(kwargs.get("retries", 2))
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    return await provider_call(
                        client,
                        provider,
                        model=model,
                        messages=messages,
                        tools=tools,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        endpoint=endpoint,
                        api_key=api_key,
                        tool_choice=tool_choice,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                        httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    # 瞬时网络抖动(如 NIM 连接重置) — 指数退避重试
                    last_exc = exc
                    if attempt < retries:
                        await asyncio.sleep(1.5 * (2 ** attempt))
            raise last_exc if last_exc else RuntimeError("llm_call retry exhausted")
        except ValueError as exc:
            # Missing key etc. — degrade to stub rather than crashing the caller.
            content = kwargs.get("default_content", f"{_STUB_CONTENT} ({exc})")
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        except httpx.HTTPStatusError as exc:
            # Provider rejected the request (bad key, rate limit, unknown model, ...) —
            # surface the status + body instead of a raw 500 with no explanation.
            status = exc.response.status_code
            detail = exc.response.text.strip()[:300]
            content = kwargs.get(
                "default_content",
                f"{provider} rejected the request (HTTP {status}): {detail or 'no detail returned'}",
            )
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        except httpx.HTTPError as exc:
            # Network/timeout/connect errors talking to the provider endpoint.
            content = kwargs.get(
                "default_content",
                f"could not reach {provider} ({endpoint or 'default endpoint'}): {exc}",
            )
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }


async def llm_stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
    """Streaming chat completion (OpenAI delta events), stub fallback."""
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    if not get_api_key(provider, kwargs.get("config")):
        content = kwargs.get("default_content", "LLM streaming not configured — shim.")
        for word in content.split():
            yield {"choices": [{"delta": {"content": word + " "}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        return

    timeout = kwargs.get("timeout", 120.0)
    tools = kwargs.get("tools")
    max_tokens = kwargs.get("max_tokens", 4096)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async for event in provider_stream(
                client,
                provider,
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            ):
                yield event
        except ValueError as exc:
            yield {"choices": [{"delta": {"content": f"{_STUB_CONTENT} ({exc}) "}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
