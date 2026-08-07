"""LLM 智能路由门禁 — veya1.1 别名 / 7 档分类 / 长文并行快速回答。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))


# =========================================================================
# oprim._llm_router — 档位分类与矩阵
# =========================================================================

def test_route_vision_to_dashscope():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]}])
    assert d["route"] == "vision"
    assert d["provider"] == "dashscope"
    assert d["model"] == "qwen3.7-flash"


def test_route_text_to_deepseek_flash():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "你好"}])
    assert d["route"] == "quick"          # <300 tokens
    assert d["provider"] == "deepseek"
    assert d["model"] == "deepseek-v4-flash"


def test_route_long():
    from oprim._llm_router import route_decision

    long_text = "长文" * 15000              # 30000 chars → ~8500 tokens > 6000 阈值
    d = route_decision([{"role": "user", "content": long_text}])
    assert d["route"] == "long"


def test_route_tool_and_code():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "调用工具"}],
                       tools=[{"type": "function", "function": {"name": "x"}}])
    assert d["route"] == "tool"
    d2 = route_decision([{"role": "user", "content": "def foo():\n    return 1"}])
    assert d2["route"] == "code"


def test_route_fallback_on_bad_matrix(tmp_path):
    from oprim._llm_router import load_matrix, route_decision

    bad = tmp_path / "llm-router.json"
    bad.write_text("{not json")
    m = load_matrix(str(bad))
    assert m["fallback"]["model"] == "deepseek-v4-flash"   # 损坏 → 默认
    d = route_decision([{"role": "user", "content": "x"}], matrix=m)
    assert d["provider"] == "deepseek"


def test_matrix_hot_reload(tmp_path):
    from oprim._llm_router import load_matrix

    f = tmp_path / "router.json"
    f.write_text('{"routes": {"text": {"provider": "openai", "model": "gpt-x"}}}')
    m1 = load_matrix(str(f))
    assert m1["routes"]["text"]["model"] == "gpt-x"
    f.write_text('{"routes": {"text": {"provider": "openai", "model": "gpt-y"}}}')
    import time as _t
    _t.sleep(0.01)
    m2 = load_matrix(str(f))
    assert m2["routes"]["text"]["model"] == "gpt-y"        # mtime 热重载


# =========================================================================
# oprim._parallel_llm — 切分与并行聚合
# =========================================================================

def test_split_prompt_paragraphs():
    from oprim._parallel_llm import split_prompt

    long = "\n\n".join(f"第{i}段内容" * 50 for i in range(6))
    chunks = split_prompt(long, max_chunks=4)
    assert len(chunks) <= 4
    assert all(len(c) > 0 for c in chunks)


def test_parallel_dispatch_speedup_and_failure_isolation():
    """并行: N 段耗时 ≈ 最长段; 失败段标记不阻塞。"""
    from oprim._parallel_llm import dispatch_parallel

    async def slow_caller(chunk, idx):
        await asyncio.sleep(0.15)
        if idx == 2:
            return {"ok": False, "error": "boom"}
        return {"ok": True, "output": f"part{idx}"}

    long = "\n\n".join(f"段{i}内容" * 40 for i in range(5))

    async def run():
        t0 = time.time()
        r = await dispatch_parallel(long, slow_caller, max_parallel=4)
        return r, time.time() - t0

    r, elapsed = asyncio.run(run())
    assert r["parallel"] is True
    assert r["chunks"] == 4
    assert elapsed < 0.5                      # 并行 ≈ 单段 0.15s, 远小于串行 0.6s
    assert "未完成" in r["aggregated"]        # 失败段标记 (不阻塞聚合)
    assert "第 3 部分" in r["aggregated"]
    assert r["partial"].count(True) == 3


def test_parallel_short_prompt_passthrough():
    from oprim._parallel_llm import dispatch_parallel

    async def caller(chunk, idx):
        return {"ok": True, "output": chunk}

    r = asyncio.run(dispatch_parallel("短文本", caller))
    assert r["parallel"] is False and r["chunks"] == 1


# =========================================================================
# oskill.llm_router — 技能编排 + 审计
# =========================================================================

def test_llm_router_audit_and_route(tmp_path):
    from oskill.llm_router import LLMRouter

    audit = tmp_path / "router-audit.jsonl"
    router = LLMRouter(audit_path=str(audit))
    d = router.route([{"role": "user", "content": "hi"}])
    assert d["route"] == "quick" and d["alias"] == "veya1.1"
    assert audit.exists()
    assert "veya1.1" in audit.read_text()


def test_call_aliased_short_single_call():
    from oskill.llm_router import LLMRouter

    calls: list[dict] = []
    router = LLMRouter()

    async def caller(payload):
        calls.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {}}

    r = asyncio.run(router.call_aliased(
        [{"role": "user", "content": "hi"}], caller))
    assert len(calls) == 1
    assert calls[0]["provider"] == "deepseek"
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert r["route"] == "quick"


def test_call_aliased_long_parallel():
    from oskill.llm_router import LLMRouter

    calls: list[dict] = []
    router = LLMRouter()

    async def caller(payload):
        calls.append(payload)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": f"回复{len(calls)}"}}],
                "usage": {}}

    long = "\n\n".join("内容" * 3000 for _ in range(8))          # 24000 chars → long 档并行
    r = asyncio.run(router.call_aliased(
        [{"role": "user", "content": long}], caller))
    assert r["parallel"] is True
    assert len(calls) > 1
    assert "【第 1 部分】" in r["output"]


# =========================================================================
# veya/llm.py 别名接线 (mock provider_call)
# =========================================================================

def test_llm_call_veya11_alias_routes_to_deepseek(monkeypatch):
    """llm_call(model=veya1.1) → 路由到 deepseek-v4-flash (mock provider_call 断言)。"""
    from veya import llm as hllm

    seen: list[dict] = []

    async def fake_provider_call(client, provider, **kw):
        seen.append({"provider": provider, "model": kw["model"]})
        return {"choices": [{"message": {"content": "routed-ok"}}], "usage": {}}

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)
    monkeypatch.setattr("os.environ", {**__import__("os").environ,
                                       "DEEPSEEK_API_KEY": "sk-test"})

    result = asyncio.run(hllm.llm_call(
        [{"role": "user", "content": "你好"}],
        provider="veya1.1", model="veya1.1",
    ))
    assert seen and seen[0]["provider"] == "deepseek"
    assert seen[0]["model"] == "deepseek-v4-flash"
    assert result["choices"][0]["message"]["content"] == "routed-ok"


def test_llm_call_veya11_vision_routes_to_dashscope(monkeypatch):
    from veya import llm as hllm

    seen: list[dict] = []

    async def fake_provider_call(client, provider, **kw):
        seen.append({"provider": provider, "model": kw["model"]})
        return {"choices": [{"message": {"content": "vision-ok"}}], "usage": {}}

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)
    monkeypatch.setattr("os.environ", {**__import__("os").environ,
                                       "DASHSCOPE_API_KEY": "sk-test"})

    asyncio.run(hllm.llm_call(
        [{"role": "user", "content": [{"type": "image_url",
                                       "image_url": {"url": "data:image/png;base64,xx"}}]}],
        provider="veya1.1", model="veya1.1",
    ))
    assert seen and seen[0]["provider"] == "dashscope"
    assert seen[0]["model"] == "qwen3.7-flash"
