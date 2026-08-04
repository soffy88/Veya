"""
veya_tui.providers — LLM Provider 适配器
==========================================
目前支持：
  - Anthropic  (ANTHROPIC_API_KEY)
  - DeepSeek   (DEEPSEEK_API_KEY)  ← OpenAI 兼容格式

扩展点：任何 OpenAI 兼容 API 都可以用 make_openai_compat_caller() 接入。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# DeepSeek 模型列表
DEEPSEEK_MODELS = {
    "deepseek-chat": "deepseek-chat",  # DeepSeek-V3，最强通用
    "deepseek-reasoner": "deepseek-reasoner",  # DeepSeek-R1，含思维链
    "deepseek-coder": "deepseek-chat",  # alias → V3（V3 已包含代码）
}

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def make_deepseek_caller(
    model: str = "deepseek-chat",
    *,
    api_key: str | None = None,
    base_url: str = DEEPSEEK_BASE_URL,
    max_tokens: int = 8192,
) -> Any:
    """
    构造 DeepSeek LLMCaller。

    返回的 caller 签名与 oprim.llm_complete 期望的完全一致：
      async (*, messages, tools, max_tokens, system, **kw) -> dict

    响应格式规范化为 Anthropic 风格（content 列表），让 omodul 无感知。

    Args:
        model: DeepSeek 模型名，如 "deepseek-chat" / "deepseek-reasoner"。
        api_key: DeepSeek API key；None 时从 DEEPSEEK_API_KEY 环境变量读取。
        base_url: API 基础 URL，默认 https://api.deepseek.com。
        max_tokens: 最大输出 token 数。

    Returns:
        async callable — LLMCaller Protocol 兼容。

    Raises:
        ImportError: openai SDK 未安装。
        ValueError: API key 未设置。

    Example:
        >>> caller = make_deepseek_caller("deepseek-chat")
        >>> resp = await caller(messages=[{"role":"user","content":"hi"}])
        >>> resp["content"][0]["text"]
        'Hello!'
    """
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        raise ImportError(  # pragma: no cover
            "openai SDK required for DeepSeek: pip install openai"
        ) from e

    key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError(
            "DEEPSEEK_API_KEY not set. Get your key at https://platform.deepseek.com/api_keys"
        )

    # DeepSeek 使用 OpenAI 兼容接口
    actual_model = DEEPSEEK_MODELS.get(model, model)
    client = AsyncOpenAI(api_key=key, base_url=base_url)

    async def caller(
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = max_tokens,
        system: str | None = None,
        **kw,
    ) -> dict:
        # system prompt 注入（OpenAI 格式放在 messages[0]）
        msgs = list(messages)  # pragma: no cover
        if system:  # pragma: no cover
            # 如果已有 system 消息则替换，否则插入
            if msgs and msgs[0].get("role") == "system":  # pragma: no cover
                msgs[0] = {"role": "system", "content": system}  # pragma: no cover
            else:
                msgs.insert(0, {"role": "system", "content": system})  # pragma: no cover

        # OpenAI tools 格式转换（oprim 传的是 Anthropic 格式）
        oai_tools = None  # pragma: no cover
        if tools:  # pragma: no cover
            oai_tools = [_anthropic_tool_to_openai(t) for t in tools]  # pragma: no cover

        kw2: dict = dict(  # pragma: no cover
            model=actual_model,
            messages=msgs,
            max_tokens=max_tokens,
        )
        if oai_tools:  # pragma: no cover
            kw2["tools"] = oai_tools  # pragma: no cover
            kw2["tool_choice"] = "auto"  # pragma: no cover

        resp = await client.chat.completions.create(**kw2)  # pragma: no cover

        # 规范化响应 → Anthropic 风格（让 omodul/oprim 无感知）
        return _openai_resp_to_anthropic(resp)  # pragma: no cover

    # 附上元信息，供 StatusBar 显示
    caller.__name__ = f"deepseek/{actual_model}"  # type: ignore[attr-defined]
    caller._model = actual_model  # type: ignore[attr-defined]
    caller._provider = "deepseek"  # type: ignore[attr-defined]

    return caller


def _anthropic_tool_to_openai(tool: dict) -> dict:
    """Anthropic tool schema → OpenAI function tool schema。"""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _openai_resp_to_anthropic(resp: Any) -> dict:
    """
    OpenAI ChatCompletion → Anthropic Messages 格式。

    Anthropic 格式:
      {
        "content": [{"type": "text", "text": "..."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": N, "output_tokens": M},
      }
    """
    choice = resp.choices[0] if resp.choices else None
    content = []

    if choice:
        msg = choice.message

        # 文本内容
        if msg.content:
            content.append({"type": "text", "text": msg.content})

        # tool_calls → Anthropic tool_use 格式
        if msg.tool_calls:
            import json

            for tc in msg.tool_calls:
                try:
                    inp = json.loads(tc.function.arguments or "{}")
                except Exception:  # pragma: no cover
                    inp = {}  # pragma: no cover
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": inp,
                    }
                )

        # reasoning_content（DeepSeek-R1 思维链）→ thinking block
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            # 插入到 content 最前面
            content.insert(0, {"type": "thinking", "thinking": reasoning})

    # stop_reason 规范化
    finish = choice.finish_reason if choice else "stop"
    stop_reason = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
    }.get(finish or "stop", "end_turn")

    # usage
    usage = resp.usage
    return {
        "content": content,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        },
        "model": getattr(resp, "model", ""),
        "id": getattr(resp, "id", ""),
    }


def make_openai_compat_caller(
    model: str,
    *,
    api_key: str,
    base_url: str,
) -> Any:
    """
    通用 OpenAI 兼容 caller 工厂。
    可接入任何兼容 OpenAI Chat Completions API 的服务。

    Args:
        model: 模型名。
        api_key: API key。
        base_url: 服务 base URL。

    Returns:
        async LLMCaller。
    """
    return make_deepseek_caller(model, api_key=api_key, base_url=base_url)  # pragma: no cover


def detect_provider(model: str) -> str:
    """
    根据模型名推断 provider。

    Returns:
        "anthropic" | "deepseek" | "unknown"
    """
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("deepseek"):
        return "deepseek"
    return "unknown"


def get_caller(
    model: str = "deepseek-chat",
    *,
    provider: str | None = None,
) -> Any:
    """
    统一的 caller 获取入口。自动根据模型名或 provider 选择适配器。

    Args:
        model: 模型名（决定 provider，除非显式指定）。
        provider: "anthropic" | "deepseek"；None 时自动推断。

    Returns:
        async LLMCaller。

    Raises:
        ValueError: provider 未知或 key 未设置。
    """
    p = provider or detect_provider(model)

    if p == "deepseek":
        return make_deepseek_caller(model)

    if p == "anthropic":
        try:  # pragma: no cover
            import anthropic  # pragma: no cover

            client = anthropic.Anthropic()  # pragma: no cover

            async def anthropic_caller(
                *,
                messages,
                tools=None,  # pragma: no cover
                max_tokens=4096,
                system=None,
                **kw,
            ):
                def _call():  # pragma: no cover
                    kw2: dict = dict(
                        model=model,
                        messages=messages,  # pragma: no cover
                        max_tokens=max_tokens,
                    )
                    if system:  # pragma: no cover
                        kw2["system"] = system  # pragma: no cover
                    if tools:  # pragma: no cover
                        kw2["tools"] = tools  # pragma: no cover
                    return client.messages.create(**kw2)  # pragma: no cover

                msg = await asyncio.to_thread(_call)  # pragma: no cover
                return {  # pragma: no cover
                    "content": [
                        {"type": b.type, "text": getattr(b, "text", "")} for b in msg.content
                    ],
                    "stop_reason": msg.stop_reason,
                    "usage": {
                        "input_tokens": msg.usage.input_tokens,
                        "output_tokens": msg.usage.output_tokens,
                    },
                }

            return anthropic_caller  # pragma: no cover
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install anthropic") from e  # pragma: no cover

    raise ValueError(f"Unknown provider '{p}' for model '{model}'. Supported: anthropic, deepseek")
