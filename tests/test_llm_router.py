"""LLM 智能路由门禁 — veya1.1 别名 / 7 档分类 / 长文并行快速回答。"""

from __future__ import annotations

import asyncio
import json
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

    d = route_decision(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                ],
            }
        ]
    )
    assert d["route"] == "vision"
    assert d["provider"] == "dashscope"
    assert d["model"] == "qwen3.7-flash"


def test_route_text_to_opencode_flash():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "你好"}])
    assert d["route"] == "quick"  # <300 tokens
    assert d["provider"] == "opencode"
    assert d["model"] == "opencode-go/deepseek-v4-flash"


def test_route_long():
    from oprim._llm_router import route_decision

    long_text = "长文" * 15000  # 30000 chars → ~8500 tokens > 6000 阈值
    d = route_decision([{"role": "user", "content": long_text}])
    assert d["route"] == "long"


def test_route_tool_and_code():
    from oprim._llm_router import route_decision

    d = route_decision(
        [{"role": "user", "content": "调用工具"}],
        tools=[{"type": "function", "function": {"name": "x"}}],
    )
    assert d["route"] == "tool"
    d2 = route_decision([{"role": "user", "content": "def foo():\n    return 1"}])
    assert d2["route"] == "code"


def test_route_fallback_on_bad_matrix(tmp_path):
    from oprim._llm_router import load_matrix, route_decision

    bad = tmp_path / "llm-router.json"
    bad.write_text("{not json")
    m = load_matrix(str(bad))
    assert m["fallback"]["model"] == "opencode-go/deepseek-v4-flash"  # 损坏 → 默认
    d = route_decision([{"role": "user", "content": "x"}], matrix=m)
    assert d["provider"] == "opencode"


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
    assert m2["routes"]["text"]["model"] == "gpt-y"  # mtime 热重载


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
    assert elapsed < 0.5  # 并行 ≈ 单段 0.15s, 远小于串行 0.6s
    assert "未完成" in r["aggregated"]  # 失败段标记 (不阻塞聚合)
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
        return {
            "choices": [{"message": {"role": "assistant", "content": "正常回复内容"}}],
            "usage": {},
        }

    r = asyncio.run(router.call_aliased([{"role": "user", "content": "hi"}], caller))
    assert len(calls) == 1
    assert calls[0]["provider"] == "opencode"
    assert calls[0]["model"] == "opencode-go/deepseek-v4-flash"
    assert r["route"] == "quick"


def test_call_aliased_long_parallel():
    from oskill.llm_router import LLMRouter

    calls: list[dict] = []
    router = LLMRouter()

    async def caller(payload):
        calls.append(payload)
        return {
            "choices": [{"message": {"role": "assistant", "content": f"回复{len(calls)}"}}],
            "usage": {},
        }

    long = "\n\n".join("内容" * 3000 for _ in range(8))  # 24000 chars → long 档并行
    r = asyncio.run(router.call_aliased([{"role": "user", "content": long}], caller))
    assert r["parallel"] is True
    assert len(calls) > 1
    assert "【第 1 部分】" in r["output"]


# =========================================================================
# veya/llm.py 别名接线 (mock provider_call)
# =========================================================================


def test_llm_call_veya11_alias_routes_to_opencode(monkeypatch):
    """llm_call(model=veya1.1) → opencode 档 key 直连 (mock provider_call)。"""
    from veya import llm as hllm

    seen: list[dict] = []

    async def fake_provider_call(client, provider, **kw):
        seen.append({"provider": provider, "model": kw["model"], "endpoint": kw.get("endpoint")})
        return {
            "choices": [{"message": {"role": "assistant", "content": "routed-ok"}}],
            "usage": {},
        }

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)
    monkeypatch.setattr("os.environ", {**__import__("os").environ, "OPENCODE_API_KEY": "sk-test"})

    result = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你好"}],
            provider="veya1.1",
            model="veya1.1",
        )
    )
    assert seen and seen[0]["provider"] == "opencode-go"
    assert seen[0]["model"] == "deepseek-v4-flash"  # 裸 ID (API 不接受前缀)
    assert seen[0]["endpoint"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert result["choices"][0]["message"]["content"] == "routed-ok"


def test_llm_call_veya11_opencode_failure_returns_error(monkeypatch):
    """opencode-go 调用异常 → 结构化错误消息 (不裸抛)。"""
    from veya import llm as hllm

    async def boom(client, provider, **kw):
        raise RuntimeError("网络抖动")

    async def _no_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(hllm, "provider_call", boom)
    monkeypatch.setattr(hllm.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("os.environ", {**__import__("os").environ, "OPENCODE_API_KEY": "sk-test"})

    result = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你好"}],
            provider="veya1.1",
            model="veya1.1",
        )
    )
    content = result["choices"][0]["message"]["content"]
    assert "opencode-go 调用失败" in content


def test_llm_call_veya11_direct_opencode(monkeypatch):
    """用户要求 (2026-08): veya1.1 直接用 opencode-go API — 不做多模态路由。"""
    from veya import llm as hllm

    seen: list[dict] = []

    async def fake_provider_call(client, provider, **kw):
        seen.append({"provider": provider, "model": kw["model"]})
        return {"choices": [{"message": {"content": "opencode-ok"}}], "usage": {}}

    monkeypatch.setattr(hllm, "provider_call", fake_provider_call)
    monkeypatch.setattr("os.environ", {**__import__("os").environ, "OPENCODE_API_KEY": "sk-test"})

    resp = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你是谁你能做什么"}],
            provider="veya1.1",
            model="veya1.1",
        )
    )
    # 直连 opencode-go (不再路由到 dashscope/其他模型)
    assert seen and seen[0]["provider"] == "opencode-go"
    assert seen[0]["model"] == "deepseek-v4-flash"
    assert ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") == "opencode-ok"


# =========================================================================
# 分层路由 v2 — 动态成本阈值 / Frontier 档 / 质量闸门 / traces 分析
# =========================================================================


def test_route_frontier_on_security_keywords():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "对这段代码做安全审计, 找 RCE 漏洞"}])
    assert d["route"] == "frontier"
    assert d["provider"] == "openai"
    assert d["model"] == "gpt-5.6-luna"


def test_route_high_priority_complex_goes_frontier():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "证明费马小定理"}], priority="high")
    assert d["route"] == "frontier"  # high + reason → frontier


def test_route_low_priority_locks_cheap():
    from oprim._llm_router import route_decision

    d = route_decision([{"role": "user", "content": "def foo():\n    return 1"}], priority="low")
    assert d["route"] == "text"  # low 价值 → 锁廉价档


def test_route_budget_cap_downgrades():
    from oprim._llm_router import route_decision

    d = route_decision(
        [{"role": "user", "content": "证明欧拉公式"}], priority="normal", budget=0.001
    )
    assert d["route"] == "text"  # reason 成本超预算 → 降档


def test_quality_gate_detects_low_quality():
    from oprim._quality_gate import quality_check

    assert quality_check({"choices": [{"message": {"content": ""}}]})["ok"] is False
    assert (
        quality_check({"choices": [{"message": {"content": "LLM provider not configured"}}]})["ok"]
        is False
    )
    assert quality_check({"choices": [{"message": {"content": "好的回答内容"}}]})["ok"] is True


def test_call_aliased_gate_upgrade_retry():
    """质量闸门: 低质量 → 升级重试 1 次 (flash → reasoner)。"""
    from oskill.llm_router import LLMRouter

    calls: list[dict] = []
    router = LLMRouter()

    async def caller(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [{"message": {"role": "assistant", "content": "shim response"}}],
                "usage": {},
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "高质量回答"}}],
            "usage": {},
        }

    r = asyncio.run(router.call_aliased([{"role": "user", "content": "你好"}], caller))
    assert len(calls) == 2  # 升级重试 1 次
    assert calls[0]["model"] == "opencode-go/deepseek-v4-flash"
    assert calls[1]["model"] == "gpt-5.6-luna"
    assert r["gate"]["reason"] == "upgraded"


def test_call_aliased_gate_pass_no_retry():
    from oskill.llm_router import LLMRouter

    calls: list[dict] = []
    router = LLMRouter()

    async def caller(payload):
        calls.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "正常回答"}}], "usage": {}}

    r = asyncio.run(router.call_aliased([{"role": "user", "content": "你好"}], caller))
    assert len(calls) == 1
    assert r["gate"]["ok"] is True


def test_trace_analysis_identifies_fixed_flow(tmp_path):
    """traces 分析: 合成审计日志 → 固定流程候选识别。"""
    import subprocess

    audit = tmp_path / "llm-router.jsonl"
    lines = [
        '{"route": "text", "provider": "deepseek", "model": "deepseek-v4-flash", "ts": 1}'
    ] * 6 + [
        '{"action": "gate_upgrade", "route": "text", "ts": 2}',
        '{"route": "vision", "provider": "dashscope", "model": "qwen3.7-flash", "ts": 3}',
    ]
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "scripts/analyze_router_traces.py", str(audit), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["entries"] == 8  # 6 路由 + 1 动作 + 1 vision
    assert data["gate_upgrades"] == 1
    cand = data["fixed_flow_candidates"]
    assert any(c["route"] == "text" and "deepseek" in c["provider_model"] for c in cand)


def test_dispatch_long_planner_chain():
    """长程任务深度规划链: 强模型规划 → flash 并行执行 → 强模型聚合。"""
    import importlib

    mod = importlib.import_module("oskill.llm_router")
    router = mod.LLMRouter()

    calls: list[dict] = []

    async def caller(payload):
        calls.append(payload)
        if "gpt-5.6-luna" in payload["model"]:
            content = (
                "1. 分析需求\n2. 设计方案\n"
                if "任务拆解" in payload["messages"][0]["content"]
                else "综合回答: 方案已确认"
            )
        else:
            content = f"执行片段 {len(calls)}"
        return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}

    async def planner(text, phase):
        res = await caller(
            {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "messages": [
                    {
                        "role": "user",
                        "content": ("任务拆解规划:\n" if phase == "plan" else "综合:\n") + text,
                    }
                ],
                "tools": None,
            }
        )
        content = res["choices"][0]["message"]["content"]
        return {"ok": bool(content), "output": content}

    async def chunk_caller(chunk, idx):
        res = await caller(
            {
                "provider": "opencode",
                "model": "opencode-go/deepseek-v4-flash",
                "messages": [{"role": "user", "content": chunk}],
                "tools": None,
            }
        )
        content = res["choices"][0]["message"]["content"]
        return {"ok": bool(content), "output": content}

    long = "\n\n".join("内容" * 3000 for _ in range(8))
    r = asyncio.run(router.dispatch_long(long, chunk_caller, planner=planner, max_parallel=4))
    assert r["parallel"] is True
    assert r.get("planner") is True  # 深度规划链
    assert len(r.get("plan", [])) >= 2  # 强模型规划产出任务列表
    assert "综合回答" in r["output"]  # 强模型聚合结果
    strong = [c for c in calls if "gpt-5.6-luna" in c["model"]]
    flash = [c for c in calls if "deepseek-v4-flash" in c["model"]]
    assert len(strong) >= 2 and len(flash) >= 1


def test_get_provider_config_user_config_fallback(monkeypatch):
    """无显式参数时 model 兜底读 ~/.veya/config.json llm 段 (主脑默认 veya1.1)。"""
    from veya import llm as hllm

    monkeypatch.setattr(
        hllm, "_user_llm_config", lambda: {"provider": "veya1.1", "model": "veya1.1"}
    )
    monkeypatch.delenv("VEYA_LLM_MODEL", raising=False)
    monkeypatch.delenv("VEYA_LLM_PROVIDER", raising=False)
    p, m = hllm.get_provider_config()
    assert m == "veya1.1"  # config.json 兜底生效


def test_get_provider_config_no_user_config(monkeypatch):
    """无 config.json → 回落 env/默认 (不崩)。"""
    from veya import llm as hllm

    monkeypatch.setattr(hllm, "_user_llm_config", lambda: {})
    monkeypatch.delenv("VEYA_LLM_MODEL", raising=False)
    monkeypatch.delenv("VEYA_LLM_PROVIDER", raising=False)
    p, m = hllm.get_provider_config()
    assert p == hllm._DEFAULT_PROVIDER and m  # 默认档位正常


def test_llm_call_veya11_opencode_none_content_retries_and_errors(monkeypatch):
    """opencode-go 返回字面量 'None'/空 (网关抖动) → 换模型重试 → 全失败给明确错误。

    回归: 用户"不回复"根因 — 网关对部分输入 75% 返回 'None', 链路原静默 success。
    """
    from veya import llm as hllm

    calls: list[str] = []

    async def flaky_provider_call(client, provider, **kw):
        calls.append(kw["model"])
        # 所有模型都返回无效内容
        return {"choices": [{"message": {"role": "assistant", "content": "None"}}], "usage": {}}

    async def _no_sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(hllm, "provider_call", flaky_provider_call)
    monkeypatch.setattr(hllm.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("os.environ", {**__import__("os").environ, "OPENCODE_API_KEY": "sk-test"})

    result = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "以动画形式生成草船借箭的2分钟视频"}],
            provider="veya1.1",
            model="veya1.1",
        )
    )
    # 网关抖动: 候选模型 (deepseek/kimi-k2.7-code) 整轮重试 5 轮仍无效 →
    # gpt-5.6-luna 兜底也重试 4 次仍无效 → 结构化错误
    assert calls == ["deepseek-v4-flash", "kimi-k2.7-code"] * 5 + ["gpt-5.6-luna"] * 4
    content = result["choices"][0]["message"]["content"]
    assert "opencode-go 调用失败" in content
    assert "无效内容" in content
    assert result.get("error") is True


def test_llm_call_veya11_opencode_none_then_good_returns_good(monkeypatch):
    """首个模型返回 'None' → 换备用模型返回正常内容 → 采用正常内容。"""
    from veya import llm as hllm

    calls: list[str] = []

    async def flaky_provider_call(client, provider, **kw):
        calls.append(kw["model"])
        if kw["model"] == "deepseek-v4-flash":
            return {"choices": [{"message": {"role": "assistant", "content": "None"}}], "usage": {}}
        return {
            "choices": [{"message": {"role": "assistant", "content": "备用模型正常回复"}}],
            "usage": {},
        }

    monkeypatch.setattr(hllm, "provider_call", flaky_provider_call)
    monkeypatch.setattr("os.environ", {**__import__("os").environ, "OPENCODE_API_KEY": "sk-test"})

    result = asyncio.run(
        hllm.llm_call(
            [{"role": "user", "content": "你好"}],
            provider="veya1.1",
            model="veya1.1",
        )
    )
    assert calls == ["deepseek-v4-flash", "kimi-k2.7-code"]
    assert result["choices"][0]["message"]["content"] == "备用模型正常回复"
    assert not result.get("error")
