"""produce_wechat_article 写手↔审核打回闭环: 端到端验证(注入假 LLM/假 mcp_hevi)。"""

from __future__ import annotations

import asyncio
import json

from server import wechat_article_pipeline as pipeline

_DRAFT = {
    "title": "标题A",
    "sections": [
        {"heading": "第一节", "body": "内容一", "image_brief": "图一"},
        {"heading": "第二节", "body": "内容二", "image_brief": "图二"},
    ],
    "closing": "结尾",
}


def _resp(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeTools:
    def __init__(self, hevi_ok: bool = False) -> None:
        self._hevi_ok = hevi_ok

    def has(self, name: str) -> bool:
        return name == "mcp_hevi" and self._hevi_ok

    async def execute(self, name: str, kwargs: dict) -> str:
        return json.dumps({"path": "/tmp/fake-image.png"})


def test_revise_then_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))
    calls = {"review": 0}

    async def fake_llm_call(messages, **kwargs):
        sys_prompt = messages[0]["content"]
        if "审核官" in sys_prompt:
            calls["review"] += 1
            if calls["review"] == 1:
                return _resp(
                    json.dumps(
                        {
                            "pass": False,
                            "issues": [
                                {
                                    "criterion": "readability",
                                    "section": "第二节",
                                    "detail": "太短",
                                    "fix_instruction": "补充细节",
                                }
                            ],
                        }
                    )
                )
            return _resp(json.dumps({"pass": True, "issues": []}))
        if "只改写" in sys_prompt:
            return _resp(json.dumps([{"heading": "第二节", "body": "补充后的内容二"}]))
        return _resp(json.dumps(_DRAFT))

    monkeypatch.setattr(pipeline, "llm_call", fake_llm_call)

    result = asyncio.run(
        pipeline.produce_wechat_article_tool(
            "测试主题", "测试要求", max_iterations=3, _master_tools=_FakeTools(hevi_ok=False)
        )
    )
    assert "✅" in result
    assert "迭代次数: 2" in result


def test_best_effort_after_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))

    async def fake_llm_call(messages, **kwargs):
        sys_prompt = messages[0]["content"]
        if "审核官" in sys_prompt:
            return _resp(
                json.dumps(
                    {
                        "pass": False,
                        "issues": [
                            {
                                "criterion": "readability",
                                "section": "第一节",
                                "detail": "不够好",
                                "fix_instruction": "重写",
                            }
                        ],
                    }
                )
            )
        if "只改写" in sys_prompt:
            return _resp(json.dumps([{"heading": "第一节", "body": "改写后"}]))
        return _resp(json.dumps(_DRAFT))

    monkeypatch.setattr(pipeline, "llm_call", fake_llm_call)

    result = asyncio.run(
        pipeline.produce_wechat_article_tool(
            "测试主题", "测试要求", max_iterations=2, _master_tools=_FakeTools(hevi_ok=False)
        )
    )
    assert "⚠️" in result
    assert "best-effort" in result
    assert "遗留问题" in result


def test_missing_image_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))

    async def fake_llm_call(messages, **kwargs):
        sys_prompt = messages[0]["content"]
        if "审核官" in sys_prompt:
            return _resp(json.dumps({"pass": True, "issues": []}))
        return _resp(json.dumps(_DRAFT))

    monkeypatch.setattr(pipeline, "llm_call", fake_llm_call)

    result = asyncio.run(
        pipeline.produce_wechat_article_tool(
            "测试主题", "测试要求", _master_tools=_FakeTools(hevi_ok=False)
        )
    )
    assert "✅" in result
    html_line = next(line for line in result.splitlines() if line.startswith("产物: "))
    html_path = html_line.split(": ", 1)[1]
    with open(html_path, encoding="utf-8") as f:
        html_text = f.read()
    assert "TODO 配图缺失" in html_text


def test_unparseable_reviewer_output_treated_as_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path))

    async def fake_llm_call(messages, **kwargs):
        sys_prompt = messages[0]["content"]
        if "审核官" in sys_prompt:
            return _resp("这不是 JSON")
        return _resp(json.dumps(_DRAFT))

    monkeypatch.setattr(pipeline, "llm_call", fake_llm_call)

    result = asyncio.run(
        pipeline.produce_wechat_article_tool(
            "测试主题", "测试要求", max_iterations=1, _master_tools=_FakeTools(hevi_ok=False)
        )
    )
    assert "⚠️" in result
    assert "best-effort" in result


def test_registers_as_high_impact_tool():
    from server.user_control import HIGH_IMPACT

    assert "produce_wechat_article" in HIGH_IMPACT
