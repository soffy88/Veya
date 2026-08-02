"""
layer4/server/providers.py — multi-provider LLM client factory

Supports DashScope (qwen-plus), Anthropic (claude-*), OpenAI (gpt-*).
Selected by config["provider"] or HICODE_LLM_PROVIDER env var.
Falls back to DashScope if unset.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

# Pricing (USD per token, approximate)
_PRICING: dict[str, tuple[float, float]] = {
    "dashscope": (0.0004 / 1000, 0.0012 / 1000),
    "anthropic":  (3.0 / 1_000_000, 15.0 / 1_000_000),
    "openai":     (0.5 / 1_000_000,  1.5 / 1_000_000),
}

_DEFAULTS: dict[str, str] = {
    "dashscope": "qwen-plus",
    "anthropic":  "claude-haiku-4-5-20251001",
    "openai":     "gpt-4o-mini",
}

_ENDPOINTS: dict[str, str] = {
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "openai":    "https://api.openai.com/v1/chat/completions",
}


def _get_provider(config: dict | None) -> str:
    p = (config or {}).get("provider")
    if p:
        return str(p)
    return str(os.environ.get("HICODE_LLM_PROVIDER", "dashscope"))


def _get_api_key(provider: str) -> str:
    if not provider:
        return ""
    keys = {
        "dashscope": "DASHSCOPE_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
        "openai":     "OPENAI_API_KEY",
    }
    # 先计算 default 值，避免在 get 调用中发生错误
    default_env_name = f"{str(provider).upper()}_API_KEY"
    return os.environ.get(keys.get(provider, default_env_name), "")


async def _call_openai_compat(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    *,
    model: str,
    messages: list,
    tools: list | None,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    
    # DEBUG
    print(f"DEBUG: calling {endpoint} with model {model}")
    # print(f"DEBUG: body: {json.dumps(body)}")
    
    resp = await client.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


async def _call_anthropic(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    model: str,
    messages: list,
    tools: list | None,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Call Anthropic Messages API and normalize to OpenAI-format response."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    system_text = "\n".join(m["content"] for m in system_msgs) or None

    # Convert tool_calls/tool messages to Anthropic format
    ant_msgs = []
    for m in other_msgs:
        role = m.get("role", "user")
        if role == "tool":
            ant_msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": m.get("content", "")}],
            })
        elif m.get("tool_calls"):
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"]),
                })
            ant_msgs.append({"role": "assistant", "content": content})
        else:
            ant_msgs.append({"role": role, "content": m.get("content", "")})

    body: dict[str, Any] = {"model": model, "messages": ant_msgs, "max_tokens": max_tokens}
    if system_text:
        body["system"] = system_text
    if tools:
        ant_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]
        body["tools"] = ant_tools

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    # Normalize to OpenAI format
    content_text = ""
    tool_calls = []
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            content_text = blk.get("text", "")
        elif blk.get("type") == "tool_use":
            tool_calls.append({
                "id": blk.get("id", ""),
                "type": "function",
                "function": {"name": blk["name"], "arguments": json.dumps(blk.get("input", {}))},
            })
    usage = data.get("usage", {})
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": content_text or None,
            "tool_calls": tool_calls or None,
        }}],
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
    tools: list | None,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Dispatch to the correct provider and return an OpenAI-format response."""
    api_key = _get_api_key(provider)
    if not api_key:
        raise ValueError(f"API key not set for provider '{provider}'")
    resolved_model = model or _DEFAULTS.get(provider, "default")

    if provider == "anthropic":
        return await _call_anthropic(
            client, api_key, model=resolved_model, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )
    endpoint = _ENDPOINTS.get(provider, _ENDPOINTS["openai"])
    return await _call_openai_compat(
        client, endpoint, api_key,
        model=resolved_model, messages=messages, tools=tools, max_tokens=max_tokens,
    )


def calc_cost(provider: str, usage: dict) -> float:
    in_price, out_price = _PRICING.get(provider, (0.0, 0.0))
    return (
        usage.get("prompt_tokens", 0) * in_price
        + usage.get("completion_tokens", 0) * out_price
    )
