"""Veya Swarm 测试 — 子探员 / Map-Reduce 并发 / SSE 播报 / 主脑召唤。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.agents.sub_agent import VeyaSubAgent
from server.coordinator_master import MASTER_SYSTEM_PROMPT, MasterCoordinator
from server.memory_bank import VeyaMemoryBank
from server.swarm_manager import SwarmOrchestrator

# ---------------------------------------------------------------------------
# 1. 轻量子探员 (Sub-Agent Worker)
# ---------------------------------------------------------------------------


def test_sub_agent_system_prompt_mask():
    agent = VeyaSubAgent(
        role="FastAPI Backend Dev",
        context="我们要写一个全栈监控系统",
        api_key="sk-x",
        llm_fn=lambda *a, **k: None,
    )
    prompt = agent.get_system_prompt()
    assert "acting as the 'FastAPI Backend Dev'" in prompt
    assert "PROJECT CONTEXT:" in prompt
    assert "我们要写一个全栈监控系统" in prompt
    assert "Do not worry about other parts of the project" in prompt
    assert "No conversational filler" in prompt


@pytest.mark.asyncio
async def test_sub_agent_executes_task():
    captured = {}

    async def fake_llm(messages, **kwargs):
        captured["messages"] = list(messages)
        captured["temperature"] = kwargs.get("temperature")
        return {
            "choices": [{"message": {"role": "assistant", "content": "def monitor(): ..."}}],
            "usage": {},
        }

    agent = VeyaSubAgent(
        role="Backend", context="监控系统", api_key="sk-x", llm_fn=fake_llm, temperature=0.2
    )
    result = await agent.execute("写一个监控接口")
    assert result == "def monitor(): ..."
    # 系统消息 = 角色面具; 用户消息 = 任务指令
    assert "acting as the 'Backend'" in captured["messages"][0]["content"]
    assert "YOUR SPECIFIC TASK" in captured["messages"][1]["content"]
    # 蜂群需要确定性: 低 temperature
    assert captured["temperature"] == 0.2


@pytest.mark.asyncio
async def test_sub_agent_failure_returns_error_string():
    async def exploding(messages, **kwargs):
        raise RuntimeError("LLM down")

    agent = VeyaSubAgent(role="DB Architect", context="x", api_key="sk-x", llm_fn=exploding, max_retries=1)
    result = await agent.execute("建表")
    assert result.startswith("Error executing task for DB Architect")


# ---------------------------------------------------------------------------
# 2. 蜂群引擎 (Swarm Orchestrator)
# ---------------------------------------------------------------------------


class _FakeSubAgent:
    """记录并发时序的假探员。"""

    def __init__(self, role, context, delay=0.0, **kwargs):
        self.role = role
        self.context = context
        self.delay = delay
        if not hasattr(_FakeSubAgent, "instances"):
            _FakeSubAgent.instances = []
        _FakeSubAgent.instances.append(self)

    def get_system_prompt(self):
        return f"mask:{self.role}"

    async def execute(self, instruction):
        await asyncio.sleep(self.delay)  # 模拟真实并发耗时
        return f"OUTPUT[{self.role}]: {instruction[:20]}"


def _make_orchestrator(fake_llm=None, delay: float = 0.0):
    return SwarmOrchestrator(
        master_api_key="sk-x",
        llm_fn=fake_llm,
        sub_agent_factory=lambda role, context: _FakeSubAgent(role=role, context=context, delay=delay),
        notify_delay=0.0,  # 测试不等待错开通知
    )


@pytest.mark.asyncio
async def test_swarm_map_reduce_concurrency():
    """Map-Reduce: 3 个探员并发执行, 产物全部进入规约 Prompt, 主脑拼接。"""
    _FakeSubAgent.instances = []
    start = asyncio.get_event_loop().time()
    master_calls = []

    async def fake_master_llm(messages, **kwargs):
        master_calls.append(messages)
        return {
            "choices": [{"message": {"role": "assistant", "content": "FINAL ARTIFACT"}}],
            "usage": {},
        }

    orch = _make_orchestrator(fake_llm=fake_master_llm, delay=0.2)
    result = await orch.run_swarm(
        "全栈监控系统",
        [
            {"role": "DB Architect", "instruction": "设计数据库 schema"},
            {"role": "FastAPI Backend", "instruction": "写 REST API"},
            {"role": "Svelte Frontend", "instruction": "写前端页面"},
        ],
    )
    elapsed = asyncio.get_event_loop().time() - start

    assert result == "FINAL ARTIFACT"
    # 3 个探员全部实例化
    assert len(_FakeSubAgent.instances) == 3
    # 并发性验证: 3 × 0.2s 串行需要 0.6s, 并发应显著更短
    assert elapsed < 0.5, f"expected concurrent execution, took {elapsed:.2f}s"
    # 规约 Prompt 包含所有子任务产物
    prompt = master_calls[0][0]["content"]
    assert "[GOAL]: 全栈监控系统" in prompt
    assert "[Output from DB Architect]" in prompt
    assert "OUTPUT[DB Architect]" in prompt
    assert "[Output from Svelte Frontend]" in prompt
    assert "Review all outputs for consistency" in prompt


@pytest.mark.asyncio
async def test_swarm_notifications_sequence():
    """SSE 播报序列: Initiated → Assigned×N → Completed×N → Execution Complete → Accomplished。"""
    _FakeSubAgent.instances = []
    events = []

    async def fake_master_llm(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}], "usage": {}}

    orch = _make_orchestrator(fake_llm=fake_master_llm, delay=0.0)

    def capture(e):
        events.append(e)

    from server.events import _on_step_ctx

    token = _on_step_ctx.set(capture)
    try:
        await orch.run_swarm(
            "目标",
            [
                {"role": "Frontend", "instruction": "t1"},
                {"role": "Backend", "instruction": "t2"},
            ],
        )
    finally:
        _on_step_ctx.reset(token)

    titles = [e.get("title") for e in events if e.get("type") == "swarm"]
    assert "🐝 Swarm Initiated" in titles
    assert titles.count("Agent Assigned") == 2
    assert titles.count("Task Completed") == 2
    assert "🧩 Swarm Execution Complete" in titles
    assert "🚀 Swarm Mission Accomplished" in titles
    # 顺序正确: 先 Initiated 后 Accomplished
    assert titles.index("🐝 Swarm Initiated") < titles.index("🚀 Swarm Mission Accomplished")


@pytest.mark.asyncio
async def test_swarm_empty_tasks():
    orch = _make_orchestrator()
    result = await orch.run_swarm("目标", [])
    assert "aborted" in result


@pytest.mark.asyncio
async def test_swarm_synthesis_failure_keeps_raw_outputs():
    """主脑规约失败 → 返回未修改的原始产物(不丢蜂群劳动成果)。"""
    _FakeSubAgent.instances = []

    async def exploding(messages, **kwargs):
        raise RuntimeError("master died")

    orch = _make_orchestrator(fake_llm=exploding, delay=0.0)
    result = await orch.run_swarm(
        "目标", [{"role": "Worker", "instruction": "干活"}]
    )
    assert "MASTER SYNTHESIS FAILED" in result
    assert "OUTPUT[Worker]" in result  # 原始产物保留


# ---------------------------------------------------------------------------
# 3. 主脑武器化 (Coordinator Schema 注入)
# ---------------------------------------------------------------------------


def test_system_prompt_has_swarm_rules():
    assert "# SWARM (CRITICAL)" in MASTER_SYSTEM_PROMPT
    assert "system_spawn_swarm" in MASTER_SYSTEM_PROMPT
    assert "Map-Reduce" in MASTER_SYSTEM_PROMPT


def test_system_schemas_include_swarm(tmp_path):
    coord = MasterCoordinator(memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"))
    names = {s["function"]["name"] for s in coord.get_system_schemas()}
    assert "system_spawn_swarm" in names
    schema = next(s for s in coord.get_system_schemas() if s["function"]["name"] == "system_spawn_swarm")
    params = schema["function"]["parameters"]
    assert params["required"] == ["overarching_goal", "sub_tasks"]
    assert params["properties"]["sub_tasks"]["type"] == "array"


@pytest.mark.asyncio
async def test_handle_tool_call_spawns_swarm(tmp_path):
    """主脑拦截 system_spawn_swarm → 蜂群执行 → 规约结果回喂。"""
    fake_swarm = AsyncMock()
    fake_swarm.run_swarm.return_value = "FINAL: 完整全栈项目"

    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"),
        swarm_engine=fake_swarm,
    )
    out = await coord.handle_tool_call(
        "system_spawn_swarm",
        {
            "overarching_goal": "全栈监控",
            "sub_tasks": [{"role": "Backend", "instruction": "写 API"}],
        },
    )
    assert "Swarm Synthesis Complete" in out
    assert "FINAL: 完整全栈项目" in out
    fake_swarm.run_swarm.assert_awaited_once_with(
        overarching_goal="全栈监控",
        sub_tasks=[{"role": "Backend", "instruction": "写 API"}],
    )


@pytest.mark.asyncio
async def test_full_loop_llm_spawns_swarm(tmp_path):
    """完整闭环: 模型决定召唤蜂群 → system_spawn_swarm → 规约产物作为最终回答。"""
    fake_swarm = AsyncMock()
    fake_swarm.run_swarm.return_value = "SYNTHESIZED: 前端+后端+数据库 完整产物"

    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "system_spawn_swarm",
                                        "arguments": json.dumps(
                                            {
                                                "overarching_goal": "全栈监控系统",
                                                "sub_tasks": [
                                                    {"role": "Frontend", "instruction": "页面"},
                                                    {"role": "Backend", "instruction": "API"},
                                                ],
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "蜂群已完成, 产物如下。"}}],
            "usage": {},
        }

    coord = MasterCoordinator(
        memory_bank=VeyaMemoryBank(storage_path=tmp_path / "m.json"),
        swarm_engine=fake_swarm,
        llm_fn=fake_llm,
        max_rounds=3,
    )
    result = await coord.chat_stream("帮我写一个全栈监控系统", session_id="swarm1")

    assert result["status"] == "success"
    assert result["tool_calls"][0]["tool"] == "system_spawn_swarm"
    # 蜂群规约结果回喂给主脑
    assert "SYNTHESIZED" in calls[1][-1]["content"]
