"""G14 — 缓存/性能模块的真实收益基准(mock 延迟对比)。

这些不是单元正确性测试,而是"受益验证":证明 LRU/SmartCache/IncrementalComputer
对慢 LLM 调用的实际加速。用 asyncio.sleep 模拟网络延迟,断言:
- 缓存命中路径显著快于未命中路径(时间界宽松,避免抖动)
- 命中时底层函数零次重算(函数调用计数)
- 增量计算:依赖未变 → 不重算;依赖变化 → 只重算受影响项
"""

from __future__ import annotations

import asyncio
import time

import pytest

from veya.cache import LRUCache, cached, generate_cache_key
from veya.performance import (
    CacheStrategy,
    create_incremental_computer,
    create_smart_cache,
)

# 模拟一次 LLM 往返的延迟
_MOCK_LLM_LATENCY = 0.15


def _mock_slow_llm(text: str) -> str:
    time.sleep(_MOCK_LLM_LATENCY)
    return f"answer:{text}"


# ---------------------------------------------------------------------------
# LRUCache
# ---------------------------------------------------------------------------


def test_lru_cache_hit_avoids_recompute():
    cache = LRUCache(max_size=10)
    calls = {"n": 0}

    def slow(x: int) -> int:
        calls["n"] += 1
        return x * 2

    cache.set("a", 1)
    assert cache.get("a") == 1
    # LRU 结构: key → value 接口(命中直接从缓存取,不调 slow)

    # 真实收益对比:未命中(计算) vs 命中(取缓存)
    key = generate_cache_key("slow", 42)
    t0 = time.time()
    first = slow(42)  # uncached compute
    uncached = time.time() - t0
    cache.set(key, first)

    t0 = time.time()
    for _ in range(50):
        hit = cache.get(key)
    cached_total = time.time() - t0

    assert hit == first
    assert cached_total < uncached * 10  # 50 次命中 < 1 次计算×10


@pytest.mark.asyncio
async def test_cached_decorator_skips_slow_llm():
    calls = {"n": 0}

    @cached(ttl=60)
    async def llm_roundtrip(prompt: str) -> str:
        calls["n"] += 1
        await asyncio.sleep(_MOCK_LLM_LATENCY)
        return f"generated:{prompt}"

    t0 = time.time()
    first = await llm_roundtrip("hi")
    cold = time.time() - t0

    t0 = time.time()
    second = await llm_roundtrip("hi")
    hot = time.time() - t0

    assert first == second
    assert calls["n"] == 1  # 第二次未调用底层函数
    assert hot < cold * 0.5  # 命中显著快于冷启动
    assert hot < 0.05


@pytest.mark.asyncio
async def test_cached_ttl_expiry_recomputes():
    calls = {"n": 0}

    @cached(ttl=0.05)
    async def llm_roundtrip(prompt: str) -> str:
        calls["n"] += 1
        return f"gen:{prompt}"

    await llm_roundtrip("x")
    await llm_roundtrip("x")  # hit
    assert calls["n"] == 1
    await asyncio.sleep(0.08)  # 过期
    await llm_roundtrip("x")  # miss → 重算
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# SmartCache
# ---------------------------------------------------------------------------


def test_smart_cache_hit_rate_and_latency_benefit():
    cache = create_smart_cache(max_size=100, strategy=CacheStrategy.LRU)

    def slow_llm(text: str) -> str:
        time.sleep(_MOCK_LLM_LATENCY)
        return f"smart:{text}"

    key = "q1"
    # 先做一次真实计算(未命中)
    t0 = time.time()
    value = slow_llm("q1")
    cold = time.time() - t0
    cache.set(key, value)

    # 预热后再测纯命中延迟
    t0 = time.time()
    for _ in range(20):
        hit = cache.get(key)
    hot = time.time() - t0

    assert hit == value
    assert hot < cold * 10  # 20 次命中 << 1 次冷计算
    stats = cache.get_stats()
    assert stats.hits >= 20
    assert stats.hit_rate > 0.9  # 预热后命中率 > 90%


def test_smart_cache_ttl_miss():
    cache = create_smart_cache(max_size=10)
    cache.set("k", "v", ttl=0.05)
    assert cache.get("k") == "v"
    time.sleep(0.08)
    assert cache.get("k") is None  # TTL 过期 → 未命中
    stats = cache.get_stats()
    assert stats.misses >= 1


# ---------------------------------------------------------------------------
# IncrementalComputer — 依赖未变不重算
# ---------------------------------------------------------------------------


def test_incremental_computer_skips_recompute_when_deps_unchanged():
    ic = create_incremental_computer()
    compute_calls = {"summary": 0, "research": 0}

    def research(project: str) -> str:
        compute_calls["research"] += 1
        time.sleep(_MOCK_LLM_LATENCY)
        return f"research({project})"

    def summary(r: str) -> str:
        compute_calls["summary"] += 1
        time.sleep(_MOCK_LLM_LATENCY)
        return f"summary[{r}]"

    ic.register("research", research, deps=[])
    ic.register("summary", summary, deps=["research"])

    ic.set_value("research", research("hicode"))
    t0 = time.time()
    s1 = ic.get_value("summary")
    first_cost = time.time() - t0

    # 依赖未变 → 二次读取零重算,直接返回缓存
    t0 = time.time()
    s2 = ic.get_value("summary")
    second_cost = time.time() - t0

    assert s1 == s2
    assert compute_calls["summary"] == 1  # 未重算
    assert second_cost < first_cost * 0.5  # 命中收益


def test_incremental_computer_recomputes_only_on_dirty_dep():
    ic = create_incremental_computer()
    compute_calls = {"summary": 0}

    def summary(r: str) -> str:
        compute_calls["summary"] += 1
        return f"summary[{r}]"

    ic.register("summary", summary, deps=["research"])
    ic.set_value("research", "v1")
    assert ic.get_value("summary") == "summary[v1]"
    assert compute_calls["summary"] == 1

    # 依赖变化 → 脏标记 → 只重算 summary
    ic.set_value("research", "v2")
    assert ic.get_value("summary") == "summary[v2]"
    assert compute_calls["summary"] == 2


# ---------------------------------------------------------------------------
# 综合基准:LLM 调用 10 次对比(模拟真实场景)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_benchmark_llm_calls_cached_vs_uncached():
    """10 次同 prompt 调用:cached 总耗时显著低于 uncached。"""

    @cached(ttl=300)
    async def llm(prompt: str) -> str:
        await asyncio.sleep(_MOCK_LLM_LATENCY)
        return f"out:{prompt}"

    t0 = time.time()
    for _ in range(10):
        await llm("same-prompt")
    cached_total = time.time() - t0

    async def uncached(prompt: str) -> str:
        await asyncio.sleep(_MOCK_LLM_LATENCY)
        return f"out:{prompt}"

    t0 = time.time()
    for _ in range(10):
        await uncached("same-prompt")
    uncached_total = time.time() - t0

    # 10 次缓存调用应接近 1 次真实调用成本
    assert cached_total < uncached_total * 0.4
    assert cached_total < 0.3  # < 0.15(一次调用)+ 抖动
