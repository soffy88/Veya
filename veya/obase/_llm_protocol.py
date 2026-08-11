"""veya/obase/_llm_protocol — pure LLM wire-protocol translation helpers.

Package-private helper for :mod:`veya.obase.llm` (obase self-contained base
layer, SPEC v3.0 §3.4). Pure functions only: OpenAI ⇄ Anthropic message /
content-block normalization, endpoint canonicalization, local/private endpoint
detection, and the core-tool-subset filter. No network I/O, no env mutation,
no monkeypatched dependencies — safe to import from both the facade and the
transport layer.
"""

from __future__ import annotations

import json


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


def _is_local_or_private(endpoint: str | None) -> bool:
    """本地/内网 OpenAI 兼容端点 (Ollama / opencodex / 局域网网关) 无需 API Key。

    容器内经 docker0 网关 (如 192.168.16.1) 访问宿主回环服务时,
    endpoint 是私网 IP 而非 localhost — 同样免 key。
    """
    if not endpoint:
        return False
    if endpoint.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0")):
        return True
    if endpoint.startswith(
        (
            "http://192.168.",
            "http://10.",
            "http://172.16.",
            "http://172.17.",
            "http://172.18.",
            "http://172.19.",
            "http://172.20.",
            "http://172.21.",
            "http://172.22.",
            "http://172.23.",
            "http://172.24.",
            "http://172.25.",
            "http://172.26.",
            "http://172.27.",
            "http://172.28.",
            "http://172.29.",
            "http://172.30.",
            "http://172.31.",
        )
    ):
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
        "fetch_url",
        "browser_run",
        "run_in_sandbox",
        "grep",
        "list_files",
        "read_file_ast",
        "delegate_to_genesis",
        "search_genesis_ledger",
        "get_market_data_schema",
        "run_backtest_coprocessor",
    }
    core: list = []
    for s in tools:
        name = (s.get("function") or {}).get("name") or ""
        if name.startswith(("system_", "reasonix_")) or name in _CORE:
            core.append(s)
    return core or None


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
        raise ValueError(f"provider {provider!r} endpoint 必须是完整 URL (http/https), 收到 {e!r}")
    if not e.rstrip("/").endswith("/chat/completions"):
        e = e.rstrip("/") + "/chat/completions"
    return e
