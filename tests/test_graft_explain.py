"""server.graft_explain — Graft 讲解层(nanonets/graft "资深工程师讲解" 内化)。

graft_context.py 只给结构(定义位置/调用方/被调方), 零 LLM。这层补"这部分是
干什么的"大白话解释, 按内容哈希缓存("builds that understanding once")。用
tmp_path 隔离缓存文件, 不写脏真实的 ~/.veya/graft_explain_cache.json(那是
用户真实数据, 假测试解释不该混进去)。
"""

from __future__ import annotations

import pytest

from server.graft_explain import GraftExplainCache, explain_module

_MODULE_SRC = (
    "def verify_token(tok):\n    return _decode(tok)\n\ndef _decode(tok):\n    return tok\n"
)


def _llm_returning(content: str):
    async def fake(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return fake


@pytest.mark.asyncio
async def test_explain_module_generates_and_caches(tmp_path):
    cache = GraftExplainCache(cache_path=tmp_path / "cache.json")
    llm = _llm_returning("这个模块负责校验用户 token, 解码后交上游使用。")

    text = await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token", "_decode"],
        cache=cache,
        llm_call_fn=llm,
    )

    assert "校验用户 token" in text
    from server.graft_explain import _content_hash

    assert cache.get("auth.py", _content_hash(_MODULE_SRC)) == text


@pytest.mark.asyncio
async def test_explain_module_cache_hit_skips_llm_call(tmp_path):
    cache = GraftExplainCache(cache_path=tmp_path / "cache.json")
    calls = []

    async def counting_llm(messages, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"role": "assistant", "content": "解释文本"}}]}

    await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache,
        llm_call_fn=counting_llm,
    )
    assert len(calls) == 1

    # 第二次源码不变 → 命中缓存, 不再调用 LLM
    text2 = await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache,
        llm_call_fn=counting_llm,
    )
    assert len(calls) == 1
    assert text2 == "解释文本"


@pytest.mark.asyncio
async def test_explain_module_content_change_invalidates_cache(tmp_path):
    cache = GraftExplainCache(cache_path=tmp_path / "cache.json")
    calls = []

    async def counting_llm(messages, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"role": "assistant", "content": f"解释{len(calls)}"}}]}

    await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache,
        llm_call_fn=counting_llm,
    )
    text2 = await explain_module(
        module="auth.py",
        source=_MODULE_SRC + "\ndef extra(): pass\n",
        symbol_names=["verify_token", "extra"],
        cache=cache,
        llm_call_fn=counting_llm,
    )
    assert len(calls) == 2
    assert text2 == "解释2"


@pytest.mark.asyncio
async def test_explain_module_stub_response_returns_empty_not_cached(tmp_path):
    cache = GraftExplainCache(cache_path=tmp_path / "cache.json")
    llm = _llm_returning("LLM provider not configured — this is a shim response.")

    text = await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache,
        llm_call_fn=llm,
    )
    assert text == ""
    assert cache.get("auth.py", "anything") is None  # 未被污染成"safe"式假缓存


@pytest.mark.asyncio
async def test_explain_module_llm_exception_returns_empty_not_crash(tmp_path):
    cache = GraftExplainCache(cache_path=tmp_path / "cache.json")

    async def boom(messages, **kwargs):
        raise RuntimeError("network down")

    text = await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache,
        llm_call_fn=boom,
    )
    assert text == ""


@pytest.mark.asyncio
async def test_explain_cache_persists_across_instances(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache1 = GraftExplainCache(cache_path=cache_path)
    llm = _llm_returning("持久化的解释")
    await explain_module(
        module="auth.py",
        source=_MODULE_SRC,
        symbol_names=["verify_token"],
        cache=cache1,
        llm_call_fn=llm,
    )

    cache2 = GraftExplainCache(cache_path=cache_path)  # 新实例, 冷启动读盘
    from server.graft_explain import _content_hash

    assert cache2.get("auth.py", _content_hash(_MODULE_SRC)) == "持久化的解释"
