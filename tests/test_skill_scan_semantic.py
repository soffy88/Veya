"""server.skill_scan_semantic 测试(补 skill_scan.py AST 层盲区的语义审查)。

AST 扫描只看 subprocess/eval/危险 import 这类调用面, 完全不检查作为字符串
字面量存在的指令内容("SYSTEM_PROMPT = \"\"\"...\"\"\"")——这层用 LLM 语义
判断: prompt-injection / 诱导性破坏外泄行为(即使调用的 API 本身正常) / 文档
跟实际行为不一致。
"""

from __future__ import annotations

import pytest

from server.skill_scan_semantic import scan_skill_semantics


def _llm_returning(content: str):
    async def fake(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return fake


@pytest.mark.asyncio
async def test_safe_verdict_parsed():
    llm = _llm_returning('{"verdict": "safe", "concerns": [], "reasoning": "看起来正常"}')
    result = await scan_skill_semantics(
        name="x", source="def main(): return 1", manifest_description="do x", llm_call_fn=llm
    )
    assert result == {"verdict": "safe", "concerns": [], "reasoning": "看起来正常"}


@pytest.mark.asyncio
async def test_malicious_verdict_with_concerns():
    llm = _llm_returning(
        '{"verdict": "malicious", "concerns": ["读取 ~/.ssh 并上传"], "reasoning": "指令诱导外泄凭证"}'
    )
    result = await scan_skill_semantics(
        name="evil", source="...", manifest_description="innocuous helper", llm_call_fn=llm
    )
    assert result["verdict"] == "malicious"
    assert "读取 ~/.ssh 并上传" in result["concerns"]


@pytest.mark.asyncio
async def test_extracts_json_from_surrounding_prose():
    llm = _llm_returning(
        'Here is my analysis:\n{"verdict": "suspicious", "concerns": ["a"], "reasoning": "b"}\nDone.'
    )
    result = await scan_skill_semantics(
        name="x", source="...", manifest_description="", llm_call_fn=llm
    )
    assert result["verdict"] == "suspicious"


@pytest.mark.asyncio
async def test_stub_response_is_unscanned_not_safe():
    """LLM 未配置(stub 回落) 绝不能被当成 verdict=safe——那是"没查"不是"查过安全"。"""
    llm = _llm_returning("LLM provider not configured — this is a shim response.")
    result = await scan_skill_semantics(
        name="x", source="...", manifest_description="", llm_call_fn=llm
    )
    assert result["verdict"] == "unscanned"


@pytest.mark.asyncio
async def test_malformed_json_is_unscanned_not_silently_safe():
    llm = _llm_returning("I refuse to answer in JSON.")
    result = await scan_skill_semantics(
        name="x", source="...", manifest_description="", llm_call_fn=llm
    )
    assert result["verdict"] == "unscanned"


@pytest.mark.asyncio
async def test_unknown_verdict_value_is_unscanned():
    llm = _llm_returning('{"verdict": "definitely_fine", "concerns": [], "reasoning": "x"}')
    result = await scan_skill_semantics(
        name="x", source="...", manifest_description="", llm_call_fn=llm
    )
    assert result["verdict"] == "unscanned"


@pytest.mark.asyncio
async def test_llm_exception_is_unscanned_not_crash():
    async def boom(messages, **kwargs):
        raise RuntimeError("network down")

    result = await scan_skill_semantics(
        name="x", source="...", manifest_description="", llm_call_fn=boom
    )
    assert result["verdict"] == "unscanned"
    assert "network down" in result["reasoning"]


@pytest.mark.asyncio
async def test_long_source_truncated_not_crash():
    llm = _llm_returning('{"verdict": "safe", "concerns": [], "reasoning": "ok"}')
    huge_source = "x = 1\n" * 100_000
    result = await scan_skill_semantics(
        name="x",
        source=huge_source,
        manifest_description="",
        llm_call_fn=llm,
        max_source_chars=100,
    )
    assert result["verdict"] == "safe"
