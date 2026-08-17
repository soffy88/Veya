"""server.consensus — best-of-N 共识装配 (注入 stub llm, 不依赖真模型)。"""

from __future__ import annotations

from server.consensus import consensus_answer


def _resp(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


async def test_synthesize_merges_candidates():
    calls = {"gen": 0}

    async def stub_llm(messages, **kw):
        content = messages[-1]["content"]
        if "独立回答" in content:  # synthesize 调用
            return _resp("FINAL")
        calls["gen"] += 1
        return _resp(f"cand-{calls['gen']}")

    out = await consensus_answer("解释这个模糊需求", n=3, _llm=stub_llm)
    assert out["method"] == "synthesize"
    assert out["chosen"] == "FINAL"
    assert len(out["candidates"]) == 3


async def test_majority_without_synthesize():
    seq = ["A", "A", "B"]
    idx = {"i": 0}

    async def stub_llm(messages, **kw):
        text = seq[idx["i"] % len(seq)]
        idx["i"] += 1
        return _resp(text)

    out = await consensus_answer("t", n=3, synthesize=False, _llm=stub_llm)
    assert out["method"] == "majority"
    assert out["chosen"] == "A"


async def test_empty_candidates_are_dropped():
    seq = ["", "good", ""]
    idx = {"i": 0}

    async def stub_llm(messages, **kw):
        text = seq[idx["i"] % len(seq)]
        idx["i"] += 1
        return _resp(text)

    out = await consensus_answer("t", n=3, synthesize=False, _llm=stub_llm)
    # 两个空候选被丢弃 (raise → errors), 只剩 "good"
    assert out["chosen"] == "good"
    assert len(out["candidates"]) == 1
