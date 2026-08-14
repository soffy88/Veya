"""3O-PURE — protocol_translate: AgentMessage ↔ LLM 标准消息。

从 veya/obase/_llm_protocol.py 复制并纯函数化的消息翻译层：

- ``agent_messages_to_llm``: Agent 内部消息 → 各 provider 线格式
  （openai/dashscope 直通；anthropic 转 content blocks；剥空 tool_calls）；
- ``llm_message_to_agent``: LLM 响应消息 → Agent 内部标准形态。

纯函数：输入消息列表，输出消息列表，无 I/O、无全局、无随机。
"""

from __future__ import annotations

from typing import Any


def strip_empty_tool_calls(messages: list) -> list:
    """删除 ``tool_calls: []`` 空数组键（所有 provider 统一兜底）。

    DeepSeek 等 openai 兼容 API 拒绝空数组（HTTP 400）；消息构造方可能写入
    ``tool_calls: []``，发送前必须剥掉该键。
    """
    out: list = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("tool_calls") == []:
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
        out.append(msg)
    return out


def parse_image_url(url: str) -> tuple[str | None, str | None]:
    """拆分 data URI 为 ``(media_type, base64_data)``；普通 URL → (None, None)。"""
    if isinstance(url, str) and url.startswith("data:"):
        header, _, payload = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return media_type, payload
    return None, None


def to_anthropic_content_blocks(content: list) -> list:
    """OpenAI content-block 列表 → Anthropic 原生块格式。

    支持的块类型: text / image_url(→image source) / image。
    """
    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "text", "text": str(block.get("text", ""))})
        elif btype == "image_url":
            media_type, data = parse_image_url(str(block.get("image_url", {}).get("url", "")))
            source: dict[str, Any]
            if media_type and data:
                source = {"type": "base64", "media_type": media_type, "data": data}
            else:
                source = {"type": "url", "url": str(block.get("image_url", {}).get("url", ""))}
            out.append({"type": "image", "source": source})
        elif btype == "image":
            out.append(block)
    return out


def agent_messages_to_llm(messages: list, *, provider: str = "openai") -> list:
    """Agent 内部消息 → provider 线格式（纯函数，无副作用）。"""
    normalized = strip_empty_tool_calls(messages)
    if provider != "anthropic":
        return normalized
    out: list[dict] = []
    for msg in normalized:
        content = msg.get("content")
        if isinstance(content, list) and msg.get("role") in ("user", "assistant"):
            msg = dict(msg)
            msg["content"] = to_anthropic_content_blocks(content)
        out.append(msg)
    return out


def normalize_tool_calls(tool_calls: Any) -> list[dict]:
    """LLM 消息里的 tool_calls → Agent 标准形态列表（容忍 None/非列表）。"""
    if not isinstance(tool_calls, list):
        return []
    out: list[dict] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        out.append(
            {
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
            }
        )
    return out


def llm_message_to_agent(message: dict) -> dict:
    """LLM 响应消息 → Agent 内部标准形态。

    产出: {role, content(str|None), tool_calls(list[dict])} ——
    供 oskill_parse_tool_call / session 记录使用。
    """
    return {
        "role": message.get("role", "assistant"),
        "content": message.get("content"),
        "tool_calls": normalize_tool_calls(message.get("tool_calls")),
    }


__all__ = [
    "agent_messages_to_llm",
    "llm_message_to_agent",
    "normalize_tool_calls",
    "parse_image_url",
    "strip_empty_tool_calls",
    "to_anthropic_content_blocks",
]
