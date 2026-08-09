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
from pathlib import Path
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
    "opencode-go": "OPENCODE_API_KEY",
}

_DEFAULT_PROVIDER = "dashscope"


def _user_llm_config() -> dict[str, str]:
    """用户主脑默认配置兜底: ~/.veya/config.json 的 llm 段。

    宿主与容器 (veya-data volume) 均可能配置; 无文件/损坏 → 空 dict。
    """
    try:
        p = Path.home() / ".veya" / "config.json"
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        llm = data.get("llm") or {}
        return {"provider": str(llm.get("provider") or "").lower(),
                "model": str(llm.get("model") or "")}
    except Exception:
        return {}


def get_provider_config(
    config: dict | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Resolve (provider, model) from explicit args → config → env → user config.json → defaults."""
    p = provider or (config or {}).get("provider")
    if not p:
        p = os.environ.get("VEYA_LLM_PROVIDER", _DEFAULT_PROVIDER)
    p = str(p).lower()
    m = model or (config or {}).get("model") or os.environ.get("VEYA_LLM_MODEL")
    if not m:
        # 用户主脑默认兜底 (config.json llm 段) — 否则无参调用落 anthropic/dashscope stub
        m = _user_llm_config().get("model") or _DEFAULT_MODELS.get(p, "default")
    return p, str(m)


def _opencode_go_key_from_auth() -> str:
    """opencode-go key 兜底: 读 opencode 本地凭据 (auth.json)。

    宿主侧通常无 OPENCODE_API_KEY env; opencode CLI 装过即有此文件。
    """
    try:
        path = os.path.expanduser("~/.local/share/opencode/auth.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ent = data.get("opencode-go") or {}
        return str(ent.get("key") or "")
    except Exception:
        return ""


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
    key = os.environ.get(env_name, "")
    if not key and provider == "opencode-go":
        key = _opencode_go_key_from_auth()
    return key


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
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 私网 endpoint (容器经 docker0 网关访问宿主桥) — 目标服务按 Host 头校验
    # 本机回环 (opencodex origin_rejected): 补 Host=127.0.0.1 让桥转发后放行。
    if _is_local_or_private(endpoint) and not endpoint.startswith(
        ("http://localhost", "http://127.0.0.1", "http://0.0.0.0")
    ):
        try:
            from urllib.parse import urlparse

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
    messages = _strip_empty_tool_calls(messages)
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


def _strip_empty_tool_calls(messages: list) -> list:
    """删除 ``tool_calls: []`` 空数组键 (所有 provider 统一兜底)。

    DeepSeek 等 openai 兼容 API 拒绝空数组: ``messages[i].tool_calls: []`` →
    HTTP 400 invalid_request_error。历史消息构造方 (coordinator/assembly) 可能
    写入空数组 (``message.get("tool_calls") or []``), 发送前必须剥掉该键。
    """
    out: list = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("tool_calls") == []:
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
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


def _in_container() -> bool:
    """容器环境检测 (与 engine_runner 一致)。"""
    return bool(os.environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")


def _is_local_or_private(endpoint: str | None) -> bool:
    """本地/内网 OpenAI 兼容端点 (Ollama / opencodex / 局域网网关) 无需 API Key。

    容器内经 docker0 网关 (如 192.168.16.1) 访问宿主回环服务时,
    endpoint 是私网 IP 而非 localhost — 同样免 key。
    """
    if not endpoint:
        return False
    if endpoint.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0")):
        return True
    if endpoint.startswith(("http://192.168.", "http://10.", "http://172.16.",
                            "http://172.17.", "http://172.18.", "http://172.19.",
                            "http://172.20.", "http://172.21.", "http://172.22.",
                            "http://172.23.", "http://172.24.", "http://172.25.",
                            "http://172.26.", "http://172.27.", "http://172.28.",
                            "http://172.29.", "http://172.30.", "http://172.31.")):
        return True
    return False


def _core_tool_schemas(tools: list | None) -> list | None:
    """全量工具面 → 核心执行子集 (本地兜底模型上下文有限)。

    本地 gpt-5.6-luna 的上下文小于云端 free 池: 全量 173 工具 + 50KB
    system prompt 会超限 → 空回复。降级兜底时只传核心执行工具面,
    保「能回复」优先 (fetch/reasonix/browser/sandbox/system/代码工具)。
    """
    if not tools:
        return None
    _CORE = {
        "fetch_url", "browser_run", "run_in_sandbox", "grep",
        "list_files", "read_file_ast", "delegate_to_genesis",
        "search_genesis_ledger", "get_market_data_schema",
        "run_backtest_coprocessor",
    }
    core: list = []
    for s in tools:
        name = ((s.get("function") or {}).get("name") or "")
        if name.startswith(("system_", "reasonix_")) or name in _CORE:
            core.append(s)
    return core or None


def _custom_proxy_url(provider: str) -> str | None:
    """自定义 provider (非内置) 在容器内的代理兜底 URL。

    内置 provider (dashscope/openai/... 国内/官方直连) 返回 None;
    容器内经桥 17890 可达宿主代理 (7890, clash) 时返回代理 URL —
    海外自定义端点被 GFW 间歇重置 (could not reach) 时自动兜底。
    """
    if provider in _ENDPOINTS or not _in_container():
        return None
    import urllib.request

    for gw in ("192.168.16.1", "172.18.0.1"):
        try:
            with urllib.request.urlopen(f"http://{gw}:17890/", timeout=0.5) as resp:
                if resp.status == 200:
                    return f"http://{gw}:17890"
        except Exception:
            continue
    return None


def _normalize_chat_endpoint(endpoint: str, provider: str) -> str:
    """归一化 openai 兼容 chat completions 端点。

    用户配置常给 base URL 形态 (``https://host/v1``), 而请求必须打到
    ``.../chat/completions`` — 否则出现 ``Invalid URL (POST /v1)`` 404。
    完整 URL (内置 _ENDPOINTS 均以 /chat/completions 结尾) 原样返回;
    相对/空 URL 明确报错 (避免 httpx 相对 URL 404 迷惑)。
    """
    e = (endpoint or "").strip()
    if not e:
        raise ValueError(f"provider {provider!r} 未配置有效 endpoint")
    if not e.startswith(("http://", "https://")):
        raise ValueError(
            f"provider {provider!r} endpoint 必须是完整 URL (http/https), 收到 {e!r}")
    if not e.rstrip("/").endswith("/chat/completions"):
        e = e.rstrip("/") + "/chat/completions"
    return e


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
        # 本地模型 (Ollama / opencodex 127.0.0.1) 无需 API Key — 与 llm_call
        # 的 local_endpoint 免 key 语义一致 (此前 provider_call 无条件拦 →
        # 本地 endpoint 也被降级成 stub)
        local = _is_local_or_private(endpoint)
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

    endpoint = endpoint or _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    endpoint = _normalize_chat_endpoint(endpoint, provider)
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
    endpoint = _normalize_chat_endpoint(endpoint, provider)
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


async def _aliased_llm_call(messages: list[dict], kwargs: dict) -> dict:
    """veya1.1 别名路由: 决策 → 单发 (short/text/tool/code/reason/vision) 或
    并行分派 (long) → 归一为 llm_call 返回格式。"""
    from veya.platform import load as _load_oskill

    oskill = _load_oskill("oskill")
    router = oskill.LLMRouter()

    async def _single(payload: dict) -> dict:
        if payload["provider"] == "opencode":
            # opencode-go 网关 key 直连 (OpenAI 兼容端点 /zen/go/v1) — 非 CLI
            # agent 模式: 主脑 system prompt 完整生效, 模型不裹 opencode 人格。
            # 矩阵 model 形如 opencode-go/deepseek-v4-flash → API 只认裸 ID。
            model = str(payload.get("model") or "opencode-go/deepseek-v4-flash")
            _, _, bare = model.partition("/")
            bare = bare or model
            # 网关对部分输入会返回字面量 'None'/空 (抖动) → 换模型重试,
            # 全失败 → 结构化错误消息 (error 标记跳过质量闸门), 绝不静默。
            alt_models = ["opencode-go/mimo-v2.5", "opencode-go/deepseek-v4-flash"]
            candidates = [model] + [m for m in alt_models if m != model]
            last_err = ""
            for cand in candidates:
                _, _, cand_bare = cand.partition("/")
                cand_bare = cand_bare or cand
                try:
                    resp = await llm_call(
                        payload["messages"],
                        config=kwargs.get("config"),
                        provider="opencode-go",
                        model=cand_bare,
                        endpoint="https://opencode.ai/zen/go/v1",
                        tools=payload.get("tools"),
                        default_content=(kwargs.get("default_content")
                                         or "opencode-go 调用失败"),
                    )
                except Exception as exc:  # 网络/鉴权失败 → 换模型重试
                    last_err = str(exc)
                    continue
                content = (
                    (resp.get("choices") or [{}])[0].get("message") or {}
                ).get("content") or ""
                tool_calls = (
                    (resp.get("choices") or [{}])[0].get("message") or {}
                ).get("tool_calls") or []
                # 有 tool_calls 的响应 content 为空是合法的 (opencode 模型把
                # 输出放 reasoning_content + tool_calls) — 不可误判为无效。
                if (not content.strip() and not tool_calls) or (
                    content.strip().lower() in ("none", "null")
                ):
                    last_err = f"opencode-go {cand_bare} 返回无效内容: {content!r}"
                    continue
                return resp
            # 全部候选空/失败 → 本地 frontier (gpt-5.6-luna) 兜底: free 池
            # 网关间歇性空回复 (可能持续数秒), 本地模型零网络抖动, 保证
            # 「绝不静默」在模型层彻底闭环。容器内走宿主桥 192.168.16.1
            # (Host 头重写已放行); 宿主默认 127.0.0.1:10100。
            try:
                frontier_endpoint = kwargs.get("endpoint") or os.environ.get(
                    "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
                )
                resp = await llm_call(
                    payload["messages"],
                    config=kwargs.get("config"),
                    provider="openai",
                    model="gpt-5.6-luna",
                    endpoint=frontier_endpoint,
                    # 本地模型上下文有限: 兜底时裁剪为核心工具面 (保回复优先)
                    tools=_core_tool_schemas(payload.get("tools")),
                    default_content="gpt-5.6-luna 兜底失败",
                )
                content = (
                    (resp.get("choices") or [{}])[0].get("message") or {}
                ).get("content") or ""
                if content.strip() and content.strip().lower() not in ("none", "null"):
                    resp["router"] = {"route": "frontier_fallback",
                                      "reason": "opencode-go empty → gpt-5.6-luna"}
                    return resp
            except Exception as exc:  # noqa: BLE001 — 兜底失败也绝不静默
                last_err = f"gpt-5.6-luna 兜底失败: {exc}"
            return {
                "choices": [{"message": {"role": "assistant",
                                          "content": (
                                              f"opencode-go 调用失败: "
                                              f"{last_err or '所有模型均失败'}")
                                          }}],
                "usage": {}, "opencode": True, "error": True,
            }
        return await llm_call(
            payload["messages"],
            config=kwargs.get("config"),
            provider=payload["provider"],
            model=payload["model"],
            tools=payload.get("tools"),
            endpoint=payload.get("endpoint") or kwargs.get("endpoint"),
            default_content=kwargs.get("default_content"),
        )

    result = await router.call_aliased(
        messages, _single, tools=kwargs.get("tools"),
        priority=str(kwargs.get("priority", "normal")),
        budget=kwargs.get("budget"),
    )
    # opencode-go 档: provider_call 不认识 opencode → 已由 _single 特判返回
    if result.get("opencode"):
        return result
    # 并行分派 → 聚合文本; 单发 → 原 llm_call 结构
    if result.get("parallel"):
        content = str(result.get("aggregated") or result.get("output") or "")
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "router": {"parallel": True, "chunks": result.get("chunks"),
                       "elapsed_s": result.get("elapsed_s")},
        }
    result.setdefault("usage", {})
    result["router"] = {"route": result.get("route"), "alias": result.get("alias")}
    # 外环兜底 (绝不静默): 无论内部哪个路径 (opencode 空 / quality gate 升级
    # 到全量工具超限等) 漏出空回复, 最后用本地 gpt-5.6-luna + 核心工具面
    # 兜一次。兜底是可靠性, 不是路由判断 — 模型决策不受影响。
    msg = ((result.get("choices") or [{}])[0].get("message") or {})
    content = (msg.get("content") or "")
    if ((not content.strip() or content.strip().lower() in ("none", "null"))
            and not msg.get("tool_calls")):
        try:
            frontier_endpoint = kwargs.get("endpoint") or os.environ.get(
                "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
            )
            resp = await llm_call(
                messages,
                config=kwargs.get("config"),
                provider="openai",
                model="gpt-5.6-luna",
                endpoint=frontier_endpoint,
                tools=_core_tool_schemas(kwargs.get("tools")),
                default_content="gpt-5.6-luna 兜底失败",
            )
            c2 = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if c2.strip() and c2.strip().lower() not in ("none", "null"):
                resp["router"] = {"route": "frontier_fallback",
                                  "reason": "empty → gpt-5.6-luna"}
                return resp
        except Exception:  # noqa: BLE001 — 兜底失败仍返回原结果 (后续各层继续兜底)
            pass
    return result





async def llm_call(messages: list[dict], **kwargs: Any) -> dict:
    """Non-streaming chat completion.

    Resolves provider/model from ``kwargs`` (``config``/``provider``/``model``),
    ``VEYA_LLM_PROVIDER``/``VEYA_LLM_MODEL`` env, or defaults. Falls back to
    a stub response when no API key is configured (keeps offline tests green).
    """
    provider, model = get_provider_config(
        kwargs.get("config"), provider=kwargs.get("provider"), model=kwargs.get("model")
    )
    # veya1.1 智能路由别名: 按任务类型路由 (deepseek-v4-flash / qwen3.7-flash),
    # 长输入并行分派快速回答 (RouteLLM 3O 内化, 见 docs/prd/LLM_ROUTER_PRD.md)
    if model in ("veya1.1", "veya-1.1") or provider == "veya1.1":
        return await _aliased_llm_call(messages, kwargs)
    config = kwargs.get("config") or {}
    # 自定义 endpoint: 顶层 kwarg > config["endpoints"][provider] > config["base_url"](NVIDIA NIM 等)
    endpoint = (
        kwargs.get("endpoint")
        or (config.get("endpoints") or {}).get(provider)
        or config.get("base_url")
        or os.environ.get("VEYA_LLM_ENDPOINT")
    )
    # 归一化到完整 chat/completions URL (base URL 形态自动补全) —
    # 提前到本作用域: 错误信息/重试看到的是真实请求 URL
    if endpoint:
        try:
            endpoint = _normalize_chat_endpoint(endpoint, provider)
        except ValueError as exc:
            content = kwargs.get("default_content", f"{_STUB_CONTENT} ({exc})")
            return {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
    # 本地/内网模型 (Ollama / opencodex / 网关桥) 无需 API Key
    local_endpoint = _is_local_or_private(endpoint)
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

    # 双通道客户端: 直连 + 代理兜底 (自定义海外端点被 GFW 间歇重置时)
    # 内置 provider (dashscope 等国内直连) 不走代理; 容器内经桥 17890 → 宿主 7890。
    proxy = _custom_proxy_url(provider)
    # 双通道客户端: 直连 + 代理兜底 (自定义海外端点被 GFW 间歇重置时)
    # 内置 provider (dashscope 等国内直连) 不走代理; 容器内经桥 17890 → 宿主 7890。
    proxy = _custom_proxy_url(provider)
    clients: list[httpx.AsyncClient] = [httpx.AsyncClient(timeout=timeout)]
    if proxy:
        clients.append(httpx.AsyncClient(timeout=timeout, proxy=proxy))
    try:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            client = clients[attempt % len(clients)]
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
                # 瞬时网络抖动(如 NIM 连接重置) — 指数退避重试 (直连/代理双通道交替)
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


# ---------------------------------------------------------------------------
# 路由调用 (freellmapi 机制内化: 统一模型 fallover / 用量跟踪 / 粘性 / 工具救援)
# ---------------------------------------------------------------------------

from veya.model_routing import (  # noqa: E402
    StickySession,
    UsageLedger,
    get_route,
    rescue_tool_calls,
)

# 逻辑模型 → 各 provider 的实际模型名 (未列出则同名)
_PROVIDER_MODEL_ALIAS: dict[tuple[str, str], str] = {
    ("deepseek-chat", "openrouter"): "deepseek/deepseek-chat",
    ("gpt-4o-mini", "openrouter"): "openai/gpt-4o-mini",
}


def _provider_model(logical_model: str, provider: str) -> str:
    """逻辑模型 → provider 实际模型名。"""
    return _PROVIDER_MODEL_ALIAS.get((logical_model, provider), logical_model)


async def llm_call_routed(
    messages: list[dict],
    *,
    logical_model: str | None = None,
    session_id: str | None = None,
    config: dict | None = None,
    ledger: UsageLedger | None = None,
    sticky: StickySession | None = None,
    max_attempts: int = 3,
    **kwargs: Any,
) -> dict:
    """路由版 llm_call: 统一模型 → provider 组 fallover + 用量跟踪 + 粘性 + 工具救援。

    Args:
        messages: 对话消息。
        logical_model: 逻辑模型名; None 用 kwargs["model"] 或默认 provider 模型。
            注册过路由 (register_route) 则走组内 fallover, 否则单 provider 直调。
        session_id: 粘性会话 id; 提供后同会话 TTL 内锁定逻辑模型。
        config / ledger / sticky: 可注入共享实例 (默认新建)。
        max_attempts: 组内最大尝试次数 (每 provider 一次)。
        **kwargs: 透传 llm_call (tools/max_tokens/temperature...)。

    Returns:
        OpenAI 格式响应; 若模型输出文本 tool call 则自动救援为结构化
        tool_calls (附带 ``_rescue: true`` 标记)。
    """
    ledger = ledger or UsageLedger()
    sticky = sticky or StickySession()

    # 粘性锁定: 已锁则用锁定模型
    if session_id:
        locked = sticky.get(session_id)
        if locked:
            logical_model = locked
    if logical_model is None:
        _, logical_model = get_provider_config(config, model=kwargs.get("model"))
    if session_id:
        sticky.lock(session_id, logical_model)

    providers = get_route(logical_model) or [
        get_provider_config(config, model=logical_model)[0]
    ]
    attempts: list[dict[str, Any]] = []
    last_error = "no provider succeeded"

    for provider in providers[:max_attempts]:
        model = _provider_model(logical_model, provider)
        # 用量门禁: 已超限的 provider 跳过
        ok, view = ledger.check(provider, model)
        if not ok:
            attempts.append({"provider": provider, "model": model, "error": "quota exceeded", "over": view["over"]})
            continue
        try:
            response = await llm_call(
                messages,
                config=config,
                provider=provider,
                model=model,
                **{k: v for k, v in kwargs.items() if k not in ("config", "provider", "model")},
            )
        except Exception as exc:  # 网络/超时/provider 异常 → 学习限额 + 下一位
            ledger.learn_limit(provider, model, error_body=str(exc))
            attempts.append({"provider": provider, "model": model, "error": f"{exc.__class__.__name__}: {exc}"})
            last_error = str(exc)
            continue

        # 用量记录 (成功才算)
        usage = response.get("usage") or {}
        ledger.record(
            provider, model,
            prompt_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0,
        )
        response["_routed"] = {"provider": provider, "model": model, "attempts": attempts}
        # 工具调用救援: 文本 tool call → 结构化
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, str) and not response.get("choices", [{}])[0].get("message", {}).get("tool_calls"):
            rescued = rescue_tool_calls(content)
            if rescued:
                response["choices"][0]["message"]["tool_calls"] = rescued
                response["_rescue"] = True
        return response

    # 全组失败: 返回结构化错误 (保留尝试轨迹)
    return {
        "_error": True,
        "error": last_error,
        "attempts": attempts,
        "logical_model": logical_model,
        "providers": providers,
    }
