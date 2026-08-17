"""master_agent.chat_stream 多工具并发执行契约 (2026-08-17)。

一轮 ReAct 返回多个 tool_call 时:
- 整批都是 parallel_safe (纯只读) → 并发执行 (asyncio.gather);
- 只要一个有副作用 → 整批顺序执行 (写不与依赖它的读并发);
- 追加给模型的 tool 结果消息顺序恒等于模型原始调用顺序;
- 单个 tool_call 走原顺序路径 (行为不变)。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from veya.platform import oservi as _load_oservi

_oservi = _load_oservi()
MasterAgent = _oservi.MasterAgent


class _RecordingTools:
    """记录并发度的假工具面: 每个工具进入时 active+1, 记录峰值, sleep 后 -1。"""

    def __init__(self, parallel_safe: set[str]) -> None:
        self._parallel_safe = parallel_safe
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []

    def get_all_schemas(self) -> list[dict]:
        return []

    def list_tools(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def has(self, name: str) -> bool:
        return True

    def is_parallel_safe(self, name: str) -> bool:
        return name in self._parallel_safe

    async def execute(self, name: str, kwargs: dict) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.03)
            self.order.append(name)
            return f"{name}-done"
        finally:
            self.active -= 1


class _NullMemory:
    def inject_subconscious(self) -> str:
        return ""


class _NullSkillHub:
    def get_all_schemas(self) -> list[dict]:
        return []

    def list_skills(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def reload_skills(self) -> dict:
        return {"loaded": 0, "skipped": 0}

    async def execute(self, name: str, kwargs: dict) -> str:
        return ""


def _tc(name: str) -> dict:
    return {"id": f"call_{name}", "function": {"name": name, "arguments": "{}"}}


def _make_agent(tools: _RecordingTools, llm) -> Any:
    return MasterAgent(
        llm_caller=llm,
        tools=tools,
        skill_hub=_NullSkillHub(),
        memory=_NullMemory(),
        swarm=None,
        vault=None,
        system_prompt="sys",
        max_rounds=3,
    )


def _two_round_llm(first_tool_calls: list[dict]):
    """第一轮返回给定 tool_calls, 第二轮直接给 final answer 收尾。"""
    calls = {"n": 0}

    async def llm(messages: list[dict], **_kwargs: Any) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": first_tool_calls}}]}
        return {"choices": [{"message": {"content": "done", "tool_calls": []}}]}

    return llm


@pytest.mark.asyncio
async def test_all_readonly_batch_runs_concurrently():
    tools = _RecordingTools(parallel_safe={"grep", "list_files", "read_file_ast"})
    agent = _make_agent(
        tools, _two_round_llm([_tc("grep"), _tc("list_files"), _tc("read_file_ast")])
    )
    result = await agent.chat_stream("go", session_id="s1")
    assert result["status"] == "success"
    assert tools.max_active >= 2, "read-only batch should overlap in execution"


@pytest.mark.asyncio
async def test_batch_with_sideeffect_tool_runs_sequentially():
    # write_file 不在 parallel_safe → 整批顺序, 峰值并发必须是 1
    tools = _RecordingTools(parallel_safe={"grep", "read_file_ast"})
    agent = _make_agent(
        tools, _two_round_llm([_tc("grep"), _tc("write_file"), _tc("read_file_ast")])
    )
    result = await agent.chat_stream("go", session_id="s2")
    assert result["status"] == "success"
    assert tools.max_active == 1, "a side-effect tool must force the whole batch serial"


@pytest.mark.asyncio
async def test_result_order_matches_call_order_when_parallel():
    # 让 grep 比 list_files 完成得晚, 验证追加顺序仍按原始调用顺序 (grep 在前)
    class _OrderTools(_RecordingTools):
        async def execute(self, name: str, kwargs: dict) -> str:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.05 if name == "grep" else 0.01)
                return f"{name}-done"
            finally:
                self.active -= 1

    tools = _OrderTools(parallel_safe={"grep", "list_files"})
    seen: dict[str, list[str]] = {}

    calls = {"n": 0}

    async def llm(messages: list[dict], **_kwargs: Any) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [
                    {"message": {"content": "", "tool_calls": [_tc("grep"), _tc("list_files")]}}
                ]
            }
        # 第二轮: 捕获主库追加的 tool 消息顺序
        seen["tool_msgs"] = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        return {"choices": [{"message": {"content": "done", "tool_calls": []}}]}

    agent = _make_agent(tools, llm)
    await agent.chat_stream("go", session_id="s3")
    # grep 先被调用 → 结果消息必须排在 list_files 之前, 即便 grep 完成得更晚
    assert seen["tool_msgs"] == ["call_grep", "call_list_files"]


@pytest.mark.asyncio
async def test_single_tool_call_unaffected():
    tools = _RecordingTools(parallel_safe={"grep"})
    agent = _make_agent(tools, _two_round_llm([_tc("grep")]))
    result = await agent.chat_stream("go", session_id="s4")
    assert result["status"] == "success"
    assert tools.order == ["grep"]
    assert tools.max_active == 1
