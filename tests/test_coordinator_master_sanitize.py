"""server.coordinator_master._sanitize_final_answer — 回归 (2026-08-16)。

背景: VEYA_AGENT_LOOP=strict (新主链, agent_loop_bridge.run_strict_chat) 早退
直接 return，此前完全跳过 chat_stream() 里"绝不静默"的兜底逻辑；即便
AgentLoop 已经捕获了具体失败原因 (result["error"])，用户也只会看到一句不带
任何诊断信息的通用文案 "⚠ 主脑未生成有效回答 (模型返回空内容 / 网关抖动)"。
_sanitize_final_answer 把兜底逻辑收成一个纯函数，新旧两条主链共用同一份。
"""

from __future__ import annotations

from server.coordinator_master import _sanitize_final_answer


def test_sanitize_leaves_non_empty_final_answer_untouched():
    result = {"final_answer": "已经是好好的回答", "error": "", "tool_calls": []}
    out = _sanitize_final_answer(result)
    assert out["final_answer"] == "已经是好好的回答"


def test_sanitize_prefers_specific_error_over_generic_message():
    result = {"final_answer": "", "error": "LLM call failed: 401 Unauthorized", "tool_calls": []}
    out = _sanitize_final_answer(result)
    assert "401 Unauthorized" in out["final_answer"]
    assert "网关抖动" not in out["final_answer"]


def test_sanitize_error_with_tool_trace_mentions_both():
    result = {
        "final_answer": "",
        "error": "LLM call failed: timeout",
        "tool_calls": [{"tool": "list_files"}, {"tool": "grep"}],
    }
    out = _sanitize_final_answer(result)
    assert "timeout" in out["final_answer"]
    assert "list_files" in out["final_answer"]


def test_sanitize_no_error_but_tool_trace_summarizes_progress():
    result = {"final_answer": "", "error": "", "tool_calls": [{"tool": "write_file"}]}
    out = _sanitize_final_answer(result)
    assert "write_file" in out["final_answer"]
    assert "继续" in out["final_answer"]


def test_sanitize_nothing_available_falls_back_to_generic_message():
    result = {"final_answer": "", "error": "", "tool_calls": []}
    out = _sanitize_final_answer(result)
    assert "网关抖动" in out["final_answer"]


def test_sanitize_treats_none_and_null_strings_as_empty():
    for placeholder in ("none", "None", "null", "NULL"):
        result = {"final_answer": placeholder, "error": "", "tool_calls": []}
        out = _sanitize_final_answer(result)
        assert out["final_answer"] != placeholder
        assert out["final_answer"].strip()
