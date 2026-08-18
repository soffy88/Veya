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


async def test_shared_prefix_branches_share_one_stem():
    seen = {"stem_calls": 0, "branch_had_stem": 0}

    async def stub_llm(messages, **kw):
        last = messages[-1]["content"]
        if "共享推理骨架" in last:  # 骨架生成 (只应发生一次)
            seen["stem_calls"] += 1
            return _resp("STEM: 关键约束 A、B")
        if "独立回答" in last:  # synthesize
            return _resp("FINAL")
        # 分支生成: 应能看到共享骨架作为 assistant 前缀
        if any(m["role"] == "assistant" and "STEM" in m["content"] for m in messages):
            seen["branch_had_stem"] += 1
        return _resp("cand")

    out = await consensus_answer("模糊任务", n=3, shared_prefix=True, _llm=stub_llm)
    assert seen["stem_calls"] == 1  # 骨架只生成一次 (共享)
    assert seen["branch_had_stem"] == 3  # 3 条分支都从同一骨架继续
    assert out["chosen"] == "FINAL"


async def test_shared_prefix_stem_failure_degrades_to_no_prefix():
    """骨架调用失败不该拖垮整次共识: 回落到无前缀, 分支照常产出。"""

    async def stub_llm(messages, **kw):
        last = messages[-1]["content"]
        if "共享推理骨架" in last:  # 骨架调用抛错
            raise RuntimeError("stem provider down")
        if "独立回答" in last:  # synthesize
            return _resp("FINAL")
        # 无前缀: 分支不应看到任何 assistant 骨架
        assert not any(m["role"] == "assistant" for m in messages)
        return _resp("cand")

    out = await consensus_answer("模糊任务", n=3, shared_prefix=True, _llm=stub_llm)
    assert out["chosen"] == "FINAL"
    assert len(out["candidates"]) == 3
