"""G7/G8/G9 E2E: 协调器真实执行路径 + 惰性初始化 + test_gate 守卫。

覆盖:
- Engine.run_turn 真实实现(无 API key 时 stub 回落,不崩溃)
- coordinator.handle 端到端(简单意图 → execute 分队 → 结构化结果)
- test_gate 在 VEYA_SKIP_TEST_GATE=1 时跳过(不递归启动 pytest)
- G9 惰性初始化:轻量子系统延迟到首次访问
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("VEYA_SKIP_TEST_GATE", "1")


@pytest.mark.asyncio
async def test_engine_run_turn_stub_fallback():
    """run_turn 无 API key 时走 stub 回落,返回结构化 dict 不崩溃。"""
    from server.assembly import assemble_main_agent

    engine = assemble_main_agent(persona="build")
    result = await engine.run_turn([{"role": "user", "content": "hi"}])
    assert isinstance(result, dict)
    assert "content" in result and "cost_usd" in result
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_coordinator_handle_end_to_end():
    """简单意图 → execute 分队 → 结构化结果(status=success)。"""
    from server.coordinator import Coordinator

    c = Coordinator()
    result = await c.handle({"text": "greet the user", "persona": "build"})
    assert result["status"] == "success"
    assert result["squads"][0]["role"] == "execute"
    assert result["squads"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_coordinator_handle_complex_dag():
    """复杂意图 → research/plan/execute DAG 三队串行。"""
    from server.coordinator import Coordinator

    c = Coordinator()
    long_text = "重构整个模块的架构并编写完整测试,涉及多个文件的依赖关系分析" * 3
    result = await c.handle({"text": long_text, "persona": "build"})
    roles = [s["role"] for s in result["squads"]]
    assert roles == ["research", "plan", "execute"]


def test_lazy_init_does_not_construct_heavy_subsystems():
    """G9: Coordinator() 构造后轻子系统未实例化(惰性),首次访问才构造。"""
    import server.coordinator as sc

    c = sc.Coordinator()
    # cached_property 未访问前不应有实例缓存
    assert "_code_graph" not in c.__dict__
    assert "_three_d_graph" not in c.__dict__
    # 访问后缓存
    _ = c.ast_analyzer
    assert "ast_analyzer" in c.__dict__


def test_test_gate_skipped_via_env():
    """VEYA_SKIP_TEST_GATE=1 时 test_gate 直接 pass 不 spawn pytest。"""
    from hooks.builtin.test_gate import test_gate
    from hooks.types import HookInput

    os.environ["VEYA_SKIP_TEST_GATE"] = "1"
    out = asyncio.run(test_gate(HookInput(point="pre_result", persona="build", cwd=".")))
    assert out.decision == "pass"


def test_semantic_search_query_delegates():
    """改名后的 semantic_search_query 委托惰性 semantic_search 引擎。"""
    from server.coordinator import Coordinator

    c = Coordinator()
    results = asyncio.run(c.semantic_search_query("def hello", top_k=2))
    assert isinstance(results, list)
