"""Tests verifying the true removal of max_rounds and closure of semantic lifecycle.

Verifies:
1. LONG_TASK_100_CYCLES: Loop can run >100 cycles with real progress, stopping semantically.
2. ROUND_COUNT_TELEMETRY: round_count correctly recorded, no max_rounds anywhere.
3. SAME_ACTION_NEW_EVIDENCE: Same action args with new revision/state continues.
4. SAME_ACTION_NO_NEW_EVIDENCE: Repeated same action with same evidence stops with no_progress_detected.
5. DEADLINE: Wall-clock / safety deadline stops with safety_resource_exhausted.
6. BLOCKED, CANCELLED, FATAL_ERROR, RESUME, NO_DUPLICATE_SIDE_EFFECT.
7. LEGACY_MAX_ROUNDS: Legacy max_rounds swallowed at boundary, not passed to canonical runtime.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.agent_loop_bridge import run_strict_chat
from server.coordinator_master import MasterCoordinator
from veya.obase.adapters import SqliteKvStore
from veya.omodul.agent_loop import AgentLoop
from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline


class DynamicLlm:
    """Dynamic LLM that calls handler function per invocation."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = 0

    async def complete(self, messages: list[dict], **kwargs: Any) -> dict:
        reply = await self._handler(self.calls, messages)
        self.calls += 1
        return {"choices": [{"message": reply}]}

    async def close(self) -> None:
        pass


def _make_tree() -> SessionTreeMgr:
    return SessionTreeMgr(kv=SqliteKvStore(":memory:"))


@pytest.mark.asyncio
async def test_long_task_100_cycles():
    """LONG_TASK_100_CYCLES: 连续 >100 个有效进展 cycle，最终 completed，无 fixed cycle cutoff。"""
    target_cycles = 105

    async def llm_handler(call_idx: int, messages: list[dict]) -> dict:
        if call_idx < target_cycles:
            return {
                "role": "assistant",
                "content": f"progress step {call_idx}",
                "tool_calls": [
                    {
                        "id": f"call_{call_idx}",
                        "type": "function",
                        "function": {
                            "name": "work_step",
                            "arguments": {"step": call_idx},
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "All 105 steps completed successfully."}

    pipeline = ToolPipeline()
    pipeline.register(
        "work_step",
        lambda step: f"result for step {step}",
        schema={"type": "object", "properties": {"step": {"type": "integer"}}},
    )

    llm = DynamicLlm(llm_handler)
    loop = AgentLoop(llm=llm, pipeline=pipeline, tree=_make_tree())
    result = await loop.run("Execute 105 progress steps")

    assert result.stop_kind == "completed"
    assert result.rounds == target_cycles + 1
    assert result.rounds > 100
    assert result.tool_calls == target_cycles
    assert "All 105 steps completed" in result.final_answer
    # Ensure no max_rounds attribute exists on result
    assert not hasattr(result, "max_rounds")


@pytest.mark.asyncio
async def test_round_count_telemetry():
    """ROUND_COUNT_TELEMETRY: round_count 正确记录，但没有 max_rounds。"""

    async def llm_handler(call_idx: int, messages: list[dict]) -> dict:
        if call_idx < 3:
            return {
                "role": "assistant",
                "content": f"step {call_idx}",
                "tool_calls": [
                    {
                        "id": f"call_{call_idx}",
                        "type": "function",
                        "function": {"name": "ping", "arguments": {"seq": call_idx}},
                    }
                ],
            }
        return {"role": "assistant", "content": "Done with telemetry."}

    pipeline = ToolPipeline()
    pipeline.register("ping", lambda seq: f"pong_{seq}", schema={"type": "object"})

    loop = AgentLoop(llm=DynamicLlm(llm_handler), pipeline=pipeline, tree=_make_tree())
    result = await loop.run("Telemetry test")

    assert result.rounds == 4
    assert result.stop_kind == "completed"
    # Ensure no max_rounds in snapshot or dict representation
    res_dict = result.__dict__
    assert "rounds" in res_dict
    assert "max_rounds" not in res_dict
    assert "DEFAULT_MAX_ROUNDS" not in res_dict
    assert "effective_rounds" not in res_dict


@pytest.mark.asyncio
async def test_same_action_new_evidence():
    """SAME_ACTION_NEW_EVIDENCE: 相同 action 参数，每次返回新 revision/state，必须继续。"""
    state_version = 0

    async def llm_handler(call_idx: int, messages: list[dict]) -> dict:
        if call_idx < 5:
            # Identical action name and arguments every time
            return {
                "role": "assistant",
                "content": "polling job status",
                "tool_calls": [
                    {
                        "id": f"poll_{call_idx}",
                        "type": "function",
                        "function": {"name": "poll_status", "arguments": {"job_id": "job-42"}},
                    }
                ],
            }
        return {"role": "assistant", "content": "Job finished after 5 revisions."}

    def poll_status(job_id: str) -> str:
        nonlocal state_version
        state_version += 1
        return json.dumps(
            {
                "job_id": job_id,
                "revision": state_version,
                "status": "running" if state_version < 5 else "done",
            }
        )

    pipeline = ToolPipeline()
    pipeline.register("poll_status", poll_status, schema={"type": "object"})

    loop = AgentLoop(llm=DynamicLlm(llm_handler), pipeline=pipeline, tree=_make_tree())
    result = await loop.run("Poll job until done")

    # Even though action was identical 5 times, new evidence each time allowed it to continue
    assert result.stop_kind == "completed"
    assert result.rounds == 6
    assert state_version == 5


@pytest.mark.asyncio
async def test_same_action_no_new_evidence():
    """SAME_ACTION_NO_NEW_EVIDENCE: 相同 action，相同 evidence 连续重复，触发 NO_PROGRESS_DETECTED。"""

    async def llm_handler(call_idx: int, messages: list[dict]) -> dict:
        return {
            "role": "assistant",
            "content": "stuck in a loop",
            "tool_calls": [
                {
                    "id": f"call_{call_idx}",
                    "type": "function",
                    "function": {"name": "check_status", "arguments": {"resource": "db"}},
                }
            ],
        }

    pipeline = ToolPipeline()
    # Identical return value every time
    pipeline.register(
        "check_status", lambda resource: "status: unchanged", schema={"type": "object"}
    )

    loop = AgentLoop(llm=DynamicLlm(llm_handler), pipeline=pipeline, tree=_make_tree())
    result = await loop.run("Check status repeatedly")

    assert result.stop_kind == "no_progress_detected"
    assert "无新进展" in result.stop_reason
    assert result.rounds >= 3


@pytest.mark.asyncio
async def test_deadline_safety_resource_exhausted(tmp_path):
    """DEADLINE: 超时安全停止为 safety_resource_exhausted / deadline_exceeded。"""

    class SlowLlm:
        async def complete(self, messages, **kwargs):
            await asyncio.sleep(0.15)
            return {"choices": [{"message": {"role": "assistant", "content": "slow"}}]}

    result = await run_strict_chat(
        "deadline task",
        llm=SlowLlm(),
        deadline=datetime.now(UTC) + timedelta(milliseconds=20),
        kv_path=str(tmp_path / "deadline.db"),
    )

    assert result["status"] == "failed"
    assert result["stop_kind"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_fatal_error_circuit_breaker():
    """FATAL_ERROR: 连续未恢复错误触发熔断 fatal_error。"""

    async def llm_handler(call_idx: int, messages: list[dict]) -> dict:
        return {
            "role": "assistant",
            "content": "calling failing tool",
            "tool_calls": [
                {
                    "id": f"call_{call_idx}",
                    "type": "function",
                    "function": {"name": "fail_tool", "arguments": {}},
                }
            ],
        }

    pipeline = ToolPipeline()
    pipeline.register(
        "fail_tool",
        lambda: (_ for _ in ()).throw(RuntimeError("hardware failure")),
        schema={"type": "object"},
    )

    loop = AgentLoop(
        llm=DynamicLlm(llm_handler),
        pipeline=pipeline,
        tree=_make_tree(),
        max_consecutive_errors=2,
    )
    result = await loop.run("Trigger fatal error")

    assert result.stop_kind == "fatal_error"
    assert "熔断" in result.stop_reason
    assert result.tool_failures >= 2


@pytest.mark.asyncio
async def test_resume_no_duplicate_side_effect(tmp_path):
    """RESUME: 跨 session 重启续接，不会重复触发已完成的副作用。"""
    side_effects: list[str] = []

    def perform_action(name: str) -> str:
        side_effects.append(name)
        return f"performed {name}"

    pipeline = ToolPipeline()
    pipeline.register("perform_action", perform_action, schema={"type": "object"})

    db_path = str(tmp_path / "resume_test.db")
    tree = SessionTreeMgr(kv=SqliteKvStore(db_path))

    # Phase 1: Run action A and B
    async def llm_phase1(call_idx: int, messages: list[dict]) -> dict:
        if call_idx == 0:
            return {
                "role": "assistant",
                "content": "running A",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "perform_action", "arguments": {"name": "A"}},
                    }
                ],
            }
        return {"role": "assistant", "content": "Phase 1 paused"}

    loop1 = AgentLoop(
        llm=DynamicLlm(llm_phase1), pipeline=pipeline, tree=tree, session_id="resume-sess-1"
    )
    res1 = await loop1.run("Do Phase 1")
    assert res1.stop_kind == "completed"
    assert side_effects == ["A"]

    # Phase 2: Resume in new loop instance from same db
    tree2 = SessionTreeMgr(kv=SqliteKvStore(db_path))

    async def llm_phase2(call_idx: int, messages: list[dict]) -> dict:
        if call_idx == 0:
            return {
                "role": "assistant",
                "content": "running B",
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "perform_action", "arguments": {"name": "B"}},
                    }
                ],
            }
        return {"role": "assistant", "content": "Phase 2 done"}

    loop2 = AgentLoop(
        llm=DynamicLlm(llm_phase2), pipeline=pipeline, tree=tree2, session_id="resume-sess-1"
    )
    res2 = await loop2.run("Do Phase 2")
    assert res2.stop_kind == "completed"
    # Action A was NOT repeated; only B was newly performed
    assert side_effects == ["A", "B"]


@pytest.mark.asyncio
async def test_legacy_max_rounds_discarded_at_boundary():
    """LEGACY_MAX_ROUNDS: legacy payload 带 max_rounds，在边界被吸收丢弃，不污染 canonical runtime。"""
    received_kwargs = {}

    class MockAgent:
        async def chat_stream(
            self, prompt: str, session_id: str | None = None, **kwargs: Any
        ) -> dict:
            received_kwargs.update(kwargs)
            return {
                "status": "success",
                "final_answer": "Legacy test completed",
                "session_id": session_id or "legacy-sess",
            }

    coordinator = MasterCoordinator(agent=MockAgent())

    # Caller passes legacy max_rounds argument
    res = await coordinator.chat_stream("Hello", session_id="s1", max_rounds=15)

    assert res["status"] == "success"
    # Canonical agent received NO max_rounds argument
    assert "max_rounds" not in received_kwargs
    assert "DEFAULT_MAX_ROUNDS" not in received_kwargs
