"""阶段 2 回归: oskill 纯函数层 (8 元素) — 幻觉拦截防线。

覆盖 veya/oskill/pure/ 全部元素:
- protocol_translate: AgentMessage ↔ LLM 标准消息
- context_compress: 滑动窗口 / token 预算裁剪 / 关键信息抽取
- ast_parse: 语法检查 / 定义清单 / 结构统计 / 禁入模块
- diff_apply: unified diff 生成 / 应用 / 统计
- parse_tool_call: OpenAI tool_calls + 文本内嵌 JSON (幻觉拦截: 解析失败显式报错)
- validate_args: JSON Schema 绝对校验 + 旧格式桥
- evaluate_stop_condition: 完成/最大轮次/致命错误/无效回复
- genetic_weight_calc: 预留占位 (确定性)

纯净性本身由 scripts/check_oskill_pure.py --strict 强制 (CI 守护),
本文件验证行为正确性。
"""

from __future__ import annotations

import pytest

from veya.oskill.pure.ast_parse import (
    find_definitions,
    forbidden_imports,
    structure_summary,
    syntax_check,
)
from veya.oskill.pure.context_compress import (
    estimate_tokens,
    extract_key_info,
    sliding_window,
    truncate_to_token_budget,
)
from veya.oskill.pure.diff_apply import apply_unified_diff, diff_stats, make_unified_diff
from veya.oskill.pure.evaluate_stop_condition import (
    StopDecision,
    evaluate_stop_condition,
    is_invalid_response,
)
from veya.oskill.pure.genetic_weight_calc import calc_weights, default_weights
from veya.oskill.pure.parse_tool_call import (
    extract_json_object,
    parse_tool_call_embed,
    parse_tool_calls,
)
from veya.oskill.pure.protocol_translate import (
    agent_messages_to_llm,
    llm_message_to_agent,
)
from veya.oskill.pure.validate_args import (
    ValidationResult,
    schema_of_legacy,
    validate_args,
)

# ---------------------------------------------------------------------------
# protocol_translate
# ---------------------------------------------------------------------------


def test_translate_strips_empty_tool_calls():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "tool_calls": []},
    ]
    out = agent_messages_to_llm(msgs, provider="openai")
    assert out[1] == {"role": "assistant", "content": "ok"}


def test_translate_anthropic_content_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    out = agent_messages_to_llm(msgs, provider="anthropic")
    blocks = out[0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"


def test_translate_passthrough_non_anthropic():
    msgs = [{"role": "user", "content": "hi"}]
    assert agent_messages_to_llm(msgs, provider="dashscope") == msgs


def test_llm_message_to_agent_normalizes():
    msg = {
        "role": "assistant",
        "content": "thinking",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
        ],
    }
    out = llm_message_to_agent(msg)
    assert out["tool_calls"] == [{"id": "c1", "name": "echo", "arguments": "{}"}]


# ---------------------------------------------------------------------------
# context_compress
# ---------------------------------------------------------------------------


def _msgs(n: int, role: str = "user") -> list[dict]:
    return [{"role": role, "content": f"msg-{i}"} for i in range(n)]


def test_sliding_window_keeps_system_pinned():
    msgs = [{"role": "system", "content": "sys"}, *_msgs(10)]
    out = sliding_window(msgs, max_messages=5)
    assert out[0]["content"] == "sys"
    assert len(out) == 5
    assert out[-1]["content"] == "msg-9"


def test_sliding_window_without_system():
    out = sliding_window(_msgs(10), max_messages=4, system_pinned=True)
    assert len(out) == 4
    assert out[-1]["content"] == "msg-9"


def test_sliding_window_no_truncation():
    assert sliding_window(_msgs(3), max_messages=5) == _msgs(3)


def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcd") >= 1
    assert estimate_tokens("") == 0


def test_truncate_to_token_budget():
    msgs = _msgs(20)
    out = truncate_to_token_budget(msgs, max_tokens=30)
    # 每条 msg-i ≈ 1 token, 预算 30 应保留约最近 30 条以内
    assert len(out) <= 20
    assert out[-1]["content"] == "msg-19"  # 最近消息必须保留
    assert out[0]["content"] != "msg-0" or len(out) == 20  # 旧消息被裁


def test_truncate_keeps_system_pinned():
    msgs = [{"role": "system", "content": "S" * 10}, *_msgs(50)]
    out = truncate_to_token_budget(msgs, max_tokens=20)
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "msg-49"


def test_extract_key_info_truncates_deterministically():
    assert extract_key_info("abc", max_chars=5) == "abc"
    out = extract_key_info("abcdefgh", max_chars=5)
    assert out.endswith("…") and len(out) == 5


# ---------------------------------------------------------------------------
# ast_parse
# ---------------------------------------------------------------------------


def test_syntax_check_ok_and_error():
    ok, err = syntax_check("def f():\n    return 1\n")
    assert ok and err == ""
    ok, err = syntax_check("def f(:\n")
    assert not ok
    assert "语法错误" in err


def test_find_definitions():
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "X = 1\n"
        "def f(a):\n    return a\n"
        "async def g():\n    pass\n"
        "class C:\n    pass\n"
    )
    defs = find_definitions(code)
    kinds = {d["name"]: d["kind"] for d in defs}
    assert kinds["f"] == "function"
    assert kinds["g"] == "async_function"
    assert kinds["C"] == "class"
    assert kinds["X"] == "assign"
    assert any(d["kind"] == "import" for d in defs)
    assert any(d["kind"] == "import_from" for d in defs)


def test_structure_summary():
    s = structure_summary("def a():\n    pass\nclass B:\n    pass\nimport json\n")
    assert s == {
        "functions": 1,
        "classes": 1,
        "imports": 1,
        "top_level_assigns": 0,
        "has_syntax_error": False,
    }
    assert structure_summary("def broken(:\n")["has_syntax_error"] is True


def test_forbidden_imports():
    code = "import os\nimport pathlib\nfrom requests import get\nimport json\n"
    assert forbidden_imports(code, ("os", "requests", "pathlib")) == ["os", "pathlib", "requests"]
    assert forbidden_imports("import json\n", ("os",)) == []


# ---------------------------------------------------------------------------
# diff_apply
# ---------------------------------------------------------------------------


def test_make_and_apply_unified_diff():
    old = "line1\nline2\nline3\nline4\nline5\n"
    new = "line1\nline2\nline3-CHANGED\nline4\nline5\n"
    diff = make_unified_diff(old, new)
    stats = diff_stats(diff)
    assert stats["added"] == 1
    assert stats["removed"] == 1
    ok, result = apply_unified_diff(old, diff)
    assert ok
    assert result == new


def test_apply_diff_insertion_and_deletion():
    old = "a\nb\nc\n"
    new = "a\nx\ny\nc\n"
    ok, result = apply_unified_diff(old, make_unified_diff(old, new))
    assert ok and result == new

    old = "a\nb\nc\nd\n"
    new = "a\nd\n"
    ok, result = apply_unified_diff(old, make_unified_diff(old, new))
    assert ok and result == new


def test_apply_diff_mismatch_reports_failure():
    old = "a\nb\nc\n"
    diff = make_unified_diff(old, "x\ny\nz\n")
    # diff 与源不符 (第三行不同) → 应用必须失败
    wrong_src = "a\nb\nDIFFERENT\n"
    ok, err = apply_unified_diff(wrong_src, diff)
    assert not ok
    assert "不匹配" in err


def test_apply_diff_noop():
    old = "same\ncontent\n"
    diff = make_unified_diff(old, old)
    assert diff == ""  # 无差异 → 空 diff
    ok, _result = apply_unified_diff(old, diff)
    assert not ok  # 空 diff 无 hunk


def test_diff_apply_idempotent_roundtrip():
    src = "\n".join(f"row{i}" for i in range(50))
    target = "\n".join(f"row{i}!" if i % 3 == 0 else f"row{i}" for i in range(50))
    diff = make_unified_diff(src, target)
    ok, result = apply_unified_diff(src, diff)
    assert ok and result == target


# ---------------------------------------------------------------------------
# parse_tool_call — 幻觉拦截核心
# ---------------------------------------------------------------------------


def _openai_msg(tool_calls: list) -> dict:
    return {"role": "assistant", "content": "go", "tool_calls": tool_calls}


def test_parse_tool_calls_ok():
    msg = _openai_msg(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path": "a.txt", "content": "hi"}',
                },
            }
        ]
    )
    calls = parse_tool_calls(msg)
    assert len(calls) == 1
    assert calls[0].name == "write_file"
    assert calls[0].arguments == {"path": "a.txt", "content": "hi"}
    assert calls[0].error == ""


def test_parse_tool_calls_dict_arguments():
    msg = _openai_msg([{"function": {"name": "echo", "arguments": {"text": "x"}}}])
    calls = parse_tool_calls(msg)
    assert calls[0].arguments == {"text": "x"}


def test_parse_tool_calls_bad_json_records_error_not_silent():
    """幻觉拦截: arguments 是坏 JSON 时显式 error, 绝不再静默 {}。"""
    msg = _openai_msg([{"function": {"name": "write_file", "arguments": "{not json"}}])
    calls = parse_tool_calls(msg)
    assert calls[0].error != ""
    assert "解析失败" in calls[0].error


def test_parse_tool_calls_non_object_arguments():
    msg = _openai_msg([{"function": {"name": "echo", "arguments": "[1,2]"}}])
    calls = parse_tool_calls(msg)
    assert calls[0].error != ""
    assert "必须是 JSON 对象" in calls[0].error


def test_parse_tool_calls_empty_and_malformed():
    assert parse_tool_calls({"role": "assistant", "content": "done"}) == []
    assert parse_tool_calls({"tool_calls": [{"function": {"name": "x"}}]})[0].name == "x"
    assert parse_tool_calls({"tool_calls": ["garbage"]}) == []


def test_parse_tool_calls_flat_agent_form():
    """Agent 内部扁平形态 (llm_message_to_agent 产出) 也必须能解析。"""
    msg = {
        "role": "assistant",
        "content": "go",
        "tool_calls": [{"id": "c1", "name": "write_file", "arguments": {"path": "a.txt"}}],
    }
    calls = parse_tool_calls(msg)
    assert len(calls) == 1
    assert calls[0].name == "write_file"
    assert calls[0].arguments == {"path": "a.txt"}
    assert calls[0].error == ""
    # 扁平形态 + 坏 arguments 同样显式报错
    bad = {"tool_calls": [{"name": "x", "arguments": "{bad"}]}
    assert parse_tool_calls(bad)[0].error != ""
    # 无名 tool_call 跳过
    assert parse_tool_calls({"tool_calls": [{"arguments": {}}]}) == []


def test_parse_tool_call_embed_json_block():
    content = (
        '请执行:\n```json\n{"name": "fetch_url", "arguments": {"url": "https://a.com"}}\n```\n谢谢'
    )
    call = parse_tool_call_embed(content)
    assert call is not None
    assert call.name == "fetch_url"
    assert call.arguments == {"url": "https://a.com"}


def test_parse_tool_call_embed_plain_object_and_nested():
    call = parse_tool_call_embed('{"name": "run", "arguments": {"a": {"b": [1, 2]}}}')
    assert call is not None
    assert call.arguments == {"a": {"b": [1, 2]}}
    assert parse_tool_call_embed("没有工具调用") is None
    assert parse_tool_call_embed("") is None


def test_extract_json_object_nested_braces():
    text = 'prefix {"k": {"deep": "}"}} suffix'
    assert extract_json_object(text) == '{"k": {"deep": "}"}}'


# ---------------------------------------------------------------------------
# validate_args — JSON Schema 绝对校验
# ---------------------------------------------------------------------------


def test_validate_required_and_type():
    schema = {
        "type": "object",
        "required": ["path", "mode"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": ["read", "write"]},
            "lines": {"type": "integer", "minimum": 1},
        },
    }
    r: ValidationResult = validate_args({"path": "a", "mode": "read", "lines": 3}, schema)
    assert r.ok
    r = validate_args({"path": "", "mode": "delete"}, schema)
    assert not r.ok
    assert any("minLength" in e for e in r.errors)
    r = validate_args({"mode": "read"}, schema)
    assert not r.ok
    assert any("缺少必填字段 'path'" in e for e in r.errors)
    r = validate_args({"path": "a", "mode": "read", "lines": "three"}, schema)
    assert not r.ok
    assert any("整数" in e or "integer" in e for e in r.errors)


def test_validate_additional_properties_and_anyof():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "additionalProperties": False,
    }
    assert not validate_args({"a": 1, "b": 2}, schema).ok
    assert validate_args({"a": 1}, schema).ok

    schema2 = {
        "type": "object",
        "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
    }
    assert validate_args({"v": "x"}, schema2).ok
    assert validate_args({"v": 3}, schema2).ok
    assert not validate_args({"v": {"x": 1}}, schema2).ok


def test_validate_array_items_and_bounds():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}
        },
    }
    assert validate_args({"tags": ["a", "b"]}, schema).ok
    assert not validate_args({"tags": []}, schema).ok
    assert not validate_args({"tags": ["a", 1]}, schema).ok


def test_validate_pattern_and_const():
    schema = {
        "type": "object",
        "properties": {"code": {"type": "string", "pattern": r"^[a-z]+$"}},
    }
    assert validate_args({"code": "abc"}, schema).ok
    assert not validate_args({"code": "ABC123"}, schema).ok
    assert not validate_args({"code": "a b"}, schema).ok
    schema2 = {"type": "object", "properties": {"mode": {"const": 5}}}
    assert validate_args({"mode": 5}, schema2).ok
    assert not validate_args({"mode": 6}, schema2).ok


def test_validate_non_object_rejected():
    r = validate_args("[1,2]", {"type": "object"})
    assert not r.ok
    assert "JSON 对象" in r.errors[0]


def test_schema_of_legacy_bridge():
    legacy = {
        "name": {"required": True, "type": "str"},
        "count": {"required": False, "type": "int"},
        "ratio": {"required": False, "type": "float"},
        "flag": {"required": False, "type": "bool"},
        "items": {"required": False, "type": "list"},
    }
    schema = schema_of_legacy(legacy)
    assert schema["required"] == ["name"]
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["properties"]["count"] == {"type": "integer"}
    assert schema["properties"]["ratio"] == {"type": "number"}
    assert schema["properties"]["flag"] == {"type": "boolean"}
    assert schema["properties"]["items"] == {"type": "array"}
    # 旧语义兼容: 缺必填 → 失败
    assert not validate_args({"count": 1}, schema).ok
    assert validate_args({"name": "x", "count": 2}, schema).ok
    # int 语义: 1.5 不再是合法 int
    assert not validate_args({"name": "x", "count": 1.5}, schema).ok


# ---------------------------------------------------------------------------
# evaluate_stop_condition
# ---------------------------------------------------------------------------


def test_stop_completed_on_direct_answer():
    d: StopDecision = evaluate_stop_condition(
        round_count=1, max_rounds=10, tool_calls=[], last_content="答案是 42"
    )
    assert d.stop and d.kind == "completed"


def test_stop_max_rounds():
    d = evaluate_stop_condition(
        round_count=10, max_rounds=10, tool_calls=[{"name": "x"}], last_content=""
    )
    assert d.stop and d.kind == "max_rounds"


def test_stop_fatal_error_wins():
    d = evaluate_stop_condition(
        round_count=0,
        max_rounds=10,
        tool_calls=[{"name": "x"}],
        last_content="",
        fatal_error="LLM 调用失败",
    )
    assert d.stop and d.kind == "fatal_error"


def test_stop_invalid_response():
    d = evaluate_stop_condition(round_count=1, max_rounds=10, tool_calls=[], last_content="none")
    assert d.stop and d.kind == "invalid_response"
    d = evaluate_stop_condition(round_count=1, max_rounds=10, tool_calls=[], last_content=None)
    assert d.kind == "invalid_response"


def test_continue_when_tools_present():
    d = evaluate_stop_condition(
        round_count=2, max_rounds=10, tool_calls=[{"function": {"name": "x"}}], last_content=""
    )
    assert not d.stop and d.kind == "continue"


def test_is_invalid_response_variants():
    for v in ("", "none", "null", "None", "N/A", None):
        assert is_invalid_response(v)
    for v in ("真实回答", "42", "done?", "ok", "OK", "done", "完成", "已完成"):
        assert not is_invalid_response(v)  # 有内容的回复不是疲劳标记 (2026-08-16)


# ---------------------------------------------------------------------------
# genetic_weight_calc (预留占位)
# ---------------------------------------------------------------------------


def test_default_weights_deterministic_and_isolated():
    w1 = default_weights()
    w2 = default_weights()
    assert w1 == w2
    w1["success_rate"] = 99.0  # 修改拷贝不影响默认
    assert default_weights()["success_rate"] == 1.0


def test_calc_weights_deterministic():
    history = [
        {"success_rate": 1.0, "avg_duration_ms": 100.0},
        {"success_rate": 0.5, "avg_duration_ms": 200.0},
    ]
    w1 = calc_weights(history)
    w2 = calc_weights(history)
    assert w1 == w2
    assert w1["success_rate"] == pytest.approx(0.75)
    assert w1["avg_duration_ms"] == pytest.approx(150.0)
    assert calc_weights([]) == default_weights()
    assert calc_weights([{"garbage": "x"}]) == default_weights()
