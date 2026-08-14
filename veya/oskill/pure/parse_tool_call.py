"""3O-PURE — parse_tool_call: LLM 输出 → tool 名 + 参数。

从现有主循环逻辑纯函数化（master_agent 的 tool_call 解析，原来 JSON 解析
失败时**静默**吞成 {}——这里改为显式错误记录，供 tool_pipeline 拦截）。

支持两种来源：
- ``parse_tool_calls``: OpenAI 格式 ``message.tool_calls``（结构化）；
- ``parse_tool_call_embed``: 文本内嵌 JSON（```json 块 或 首对花括号）。

幻觉拦截点：解析失败/参数非对象 → ToolCall.error 非空，管道据此拒绝执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """解析结果：name + arguments + 解析错误（error 非空 = 不可执行）。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    raw: str = ""
    error: str = ""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _coerce_args(raw: Any, *, tc_id: str = "") -> tuple[dict[str, Any], str]:
    """把 arguments 字段强转为 dict。返回 (args, error)。"""
    if raw is None:
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}, ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return {}, f"arguments JSON 解析失败: {exc.msg} (L{exc.lineno}:C{exc.colno})"
        if not isinstance(parsed, dict):
            return {}, f"arguments 必须是 JSON 对象, 实际是 {type(parsed).__name__}"
        return parsed, ""
    return {}, f"arguments 类型非法: {type(raw).__name__}"


def parse_tool_calls(message: dict) -> list[ToolCall]:
    """从消息解析 tool_calls → ToolCall 列表（纯函数）。

    支持两种形态：
    - OpenAI 线格式: {"function": {"name", "arguments"}}
    - Agent 内部扁平格式: {"name", "arguments"}（llm_message_to_agent 产出）

    原 master_agent 逻辑：``fn = tool_call.get("function")``；``arguments``
    为字符串时 json.loads。差异：解析失败不再静默 {}，而是 error 显式记录。
    """
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    out: list[ToolCall] = []
    for tc in raw_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if isinstance(fn, dict):
            # OpenAI 线格式
            name = str(fn.get("name") or "")
            tc_id = str(tc.get("id") or f"call_{name}")
            raw_args = fn.get("arguments")
        else:
            # Agent 内部扁平格式 (llm_message_to_agent 产出)
            name = str(tc.get("name") or "")
            tc_id = str(tc.get("id") or f"call_{name}")
            raw_args = tc.get("arguments")
        if not name:
            continue
        args, err = _coerce_args(raw_args, tc_id=tc_id)
        out.append(
            ToolCall(
                name=name,
                arguments=args,
                id=tc_id,
                raw=str(raw_args) if raw_args is not None else "",
                error=err,
            )
        )
    return out


def extract_json_object(text: str) -> str | None:
    """从文本中提取首个平衡 JSON 对象子串（含嵌套花括号）。找不到返回 None。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_tool_call_embed(content: str) -> ToolCall | None:
    """文本内嵌工具调用解析：```json 块 或 首对平衡花括号。

    期望形状: {"name": "tool_name", "arguments": {...} 或 JSON 字符串}。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    # 1) 代码块
    for block in _JSON_BLOCK_RE.findall(content):
        obj = extract_json_object(block)
        if obj is None:
            continue
        call = _parse_embed_object(obj)
        if call is not None:
            return call
    # 2) 全文首个 JSON 对象
    obj = extract_json_object(content)
    if obj is None:
        return None
    return _parse_embed_object(obj)


def _parse_embed_object(obj_text: str) -> ToolCall | None:
    try:
        data = json.loads(obj_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name") or data.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    args, err = _coerce_args(data.get("arguments"))
    return ToolCall(name=name, arguments=args, raw=obj_text, error=err)


__all__ = [
    "ToolCall",
    "extract_json_object",
    "parse_tool_call_embed",
    "parse_tool_calls",
]
