"""G2: LLM 意图分类器测试。

覆盖：启发式快速路径、LLM 裁决、解析容错、缓存、回落行为、
coordinator 集成（simple → 并行单 squad / complex → DAG 三 squad）。
"""

import pytest

from hicode.intent import Intent, IntentClassifier, classify_intent


def _llm_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def no_key(monkeypatch):
    """无 API key 环境 → 走启发式路径（不调用 LLM）。"""
    monkeypatch.setattr("hicode.llm.get_api_key", lambda *a, **k: "")


@pytest.fixture
def with_key(monkeypatch):
    """有 API key + 可控 llm_call。"""
    monkeypatch.setattr("hicode.llm.get_api_key", lambda *a, **k: "test-key")
    calls: list[list[dict]] = []

    async def fake_llm_call(messages, **kwargs):
        calls.append(messages)
        content = kwargs.get("default_content") or '{"intent": "simple", "reason": "mock"}'
        return _llm_response(content)

    monkeypatch.setattr("hicode.llm.llm_call", fake_llm_call)
    return calls


# ── 启发式快速路径（无 key） ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_heuristic_simple(no_key):
    assert await classify_intent("查一下当前时间") is Intent.SIMPLE


@pytest.mark.asyncio
async def test_heuristic_complex_keyword(no_key):
    assert await classify_intent("重构整个项目的模块结构") is Intent.COMPLEX
    assert await classify_intent("refactor the CLI entry point") is Intent.COMPLEX


@pytest.mark.asyncio
async def test_heuristic_long_text(no_key):
    long_text = "请详细分析这个项目的架构并给出优化建议。" * 30
    assert len(long_text) >= 200
    assert await classify_intent(long_text) is Intent.COMPLEX


@pytest.mark.asyncio
async def test_heuristic_short_text(no_key):
    assert await classify_intent("hi") is Intent.SIMPLE


@pytest.mark.asyncio
async def test_empty_text(no_key):
    assert await classify_intent("") is Intent.SIMPLE
    assert await classify_intent(None) is Intent.SIMPLE


# ── LLM 裁决（有 key） ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_classifies_complex(with_key):
    """LLM 返回 complex → 走 DAG 分解路径（提示词注入验证）。"""
    from hicode.intent import _llm as intent_llm_module

    calls: list[list[dict]] = []

    async def fake_llm_call(messages, **kwargs):
        calls.append(messages)
        return _llm_response('{"intent": "complex", "reason": "多模块改动"}')

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    classifier = IntentClassifier()
    try:
        result = await classifier.classify("调整支付模块并梳理整体依赖")
    finally:
        intent_llm_module.llm_call = orig

    assert result is Intent.COMPLEX
    assert len(calls) == 1
    assert calls[0][0]["role"] == "system"  # 提示词注入
    assert "用户请求" in calls[0][1]["content"]


@pytest.mark.asyncio
async def test_llm_markdown_fenced_json(with_key):
    from hicode.intent import _llm as intent_llm_module

    async def fake_llm_call(messages, **kwargs):
        return _llm_response('```json\n{"intent": "simple", "reason": "x"}\n```')

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    try:
        result = await IntentClassifier().classify("查一下这个函数的用法")
    finally:
        intent_llm_module.llm_call = orig

    assert result is Intent.SIMPLE


@pytest.mark.asyncio
async def test_llm_garbage_falls_back_to_heuristic(with_key):
    from hicode.intent import _llm as intent_llm_module

    async def fake_llm_call(messages, **kwargs):
        return _llm_response("这个请求有点意思但我说不清")

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    try:
        result = await IntentClassifier().classify("帮我看看这个函数")
    finally:
        intent_llm_module.llm_call = orig

    # 解析失败 → 回落启发式（无关键词中间地带 → SIMPLE）
    assert result is Intent.SIMPLE


@pytest.mark.asyncio
async def test_llm_exception_falls_back(with_key):
    from hicode.intent import _llm as intent_llm_module

    async def fake_llm_call(messages, **kwargs):
        raise RuntimeError("provider down")

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    try:
        result = await IntentClassifier().classify("看看这个模块的边界")
    finally:
        intent_llm_module.llm_call = orig

    assert result is Intent.SIMPLE


# ── 缓存 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cache_avoids_repeated_llm_calls(with_key):
    from hicode.intent import _llm as intent_llm_module

    call_count = 0

    async def fake_llm_call(messages, **kwargs):
        nonlocal call_count
        call_count += 1
        return _llm_response('{"intent": "complex", "reason": "x"}')

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    classifier = IntentClassifier()
    try:
        text = "评估一下这个服务的整体性能表现"
        first = await classifier.classify(text)
        second = await classifier.classify(text)
        await classifier.classify("另一条不同的中等长度请求文本")
    finally:
        intent_llm_module.llm_call = orig

    assert first is Intent.COMPLEX
    assert second is Intent.COMPLEX
    assert call_count == 2  # 前两次命中缓存，第三条新文本再调一次


# ── coordinator 集成 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_coordinator_decompose_simple(no_key):
    from server.coordinator import Coordinator

    coordinator = Coordinator(enable_streaming=False)
    plan = await coordinator._decompose({"text": "帮我查一下时间"}, cost=None)
    assert plan.schedule == "parallel"
    assert len(plan.squads) == 1
    assert plan.squads[0].role == "execute"


@pytest.mark.asyncio
async def test_coordinator_decompose_complex(no_key):
    from server.coordinator import Coordinator

    coordinator = Coordinator(enable_streaming=False)
    plan = await coordinator._decompose({"text": "重构整个项目并梳理依赖"}, cost=None)
    assert plan.schedule == "dag"
    assert [s.role for s in plan.squads] == ["research", "plan", "execute"]
    # DAG 依赖
    assert plan.squads[1].depends_on == ["research"]
    assert plan.squads[2].depends_on == ["plan"]


@pytest.mark.asyncio
async def test_coordinator_uses_llm_when_key_present(with_key):
    """有 key 时 coordinator 的分类器走 LLM（mock 返回 complex）。"""
    from hicode.intent import _llm as intent_llm_module
    from server.coordinator import Coordinator

    async def fake_llm_call(messages, **kwargs):
        return _llm_response('{"intent": "complex", "reason": "mock"}')

    orig = intent_llm_module.llm_call
    intent_llm_module.llm_call = fake_llm_call
    coordinator = Coordinator(enable_streaming=False)
    try:
        plan = await coordinator._decompose(
            {"text": "帮我检查一下这个支付服务的整体运行状态"}, cost=None
        )
    finally:
        intent_llm_module.llm_call = orig

    assert plan.schedule == "dag"


@pytest.mark.asyncio
async def test_legacy_is_simple_heuristic_kept(no_key):
    """旧私有方法保留且行为不变（关键词 + 长度）。"""
    from server.coordinator import Coordinator

    coordinator = Coordinator(enable_streaming=False)
    assert coordinator._is_simple("普通请求文本") is True
    assert coordinator._is_simple("重构一下") is False
    assert coordinator._is_simple("x" * 200) is False


@pytest.mark.asyncio
async def test_module_level_helper_no_cache(no_key):
    """模块级便捷函数每次新建分类器（无共享缓存副作用）。"""
    a = await classify_intent("查一下天气")
    b = await classify_intent("查一下天气")
    assert a is Intent.SIMPLE
    assert b is Intent.SIMPLE
