"""P3-B: explicit Bot A → Bot B semantic delegation.

The delegation runtime transports only an explicit request/result. Bot B's
operation is intentionally a callback for its own GoalRun; the parent runtime
does not receive or expose Bot A's durable objects and never accepts B's
self-report.
"""

from __future__ import annotations

import pytest

from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.models import (
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    DelegateRequest,
    DelegateResult,
    Evidence,
    reconcile_delegate_results,
)
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard

BOT_A = "bot-a"
BOT_B = "bot-b"
GOAL_A = "goal-a"
GOAL_B = "goal-b"


def _request() -> DelegateRequest:
    return DelegateRequest(
        delegate_id="a-to-b-1",
        parent_task_id="task-a",
        parent_trace_id=GOAL_A,
        objective="produce a semantic analysis",
        context_ref="explicit-context-ref",
        capability_scope=["semantic.analysis"],
        evidence_refs=["evidence-ref-1"],
        source_bot_id=BOT_A,
        target_bot_id=BOT_B,
        target_goal_run_id=GOAL_B,
        bot_id=BOT_B,
    )


@pytest.mark.asyncio
async def test_cross_bot_delegate_roundtrip_isolated_and_provenanced():
    runtime = DelegateRuntime(
        SpawnGuard(SpawnBudget(max_depth=2)), goal_run_id=GOAL_A, bot_id=BOT_A
    )
    calls: list[str] = []

    async def bot_b_semantic_work(_cancel):
        calls.append("bot-b")
        # The operation receives only its cancellation handle. It cannot
        # access Bot A's context/computer/checkpoint/ledger from the runtime.
        return DelegateResult(
            delegate_id="a-to-b-1",
            status="complete",
            stop_reason="completed",
            summary="semantic result from B",
            evidence=[
                Evidence(
                    id="b-e1",
                    kind="analysis",
                    source="bot-b",
                    content="result",
                    producer=BOT_B,
                    sha256="sha:b-e1",
                )
            ],
            source_bot_id=BOT_A,
            target_bot_id=BOT_B,
            goal_run_id=GOAL_B,
        )

    result = await runtime.run_cross_bot(
        _request(),
        bot_b_semantic_work,
        source_bot_id=BOT_A,
        target_bot_id=BOT_B,
        target_goal_run_id=GOAL_B,
    )

    assert calls == ["bot-b"]
    assert result.source_bot_id == BOT_A
    assert result.target_bot_id == BOT_B
    assert result.goal_run_id == GOAL_B
    assert result.evidence_refs == ["sha:b-e1"]
    # Bot A reconciles output only; this is not an acceptance verdict.
    assert reconcile_delegate_results([result])["overall_status"] == "success"
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0


@pytest.mark.asyncio
async def test_cross_bot_duplicate_and_restart_resume_do_not_repeat_work():
    request = _request()
    calls: list[str] = []

    async def operation(_cancel):
        calls.append("physical-through-b-goalrun")
        return {
            "status": "complete",
            "stop_reason": "completed",
            "summary": "B completed through its own GoalRun",
            "evidence_refs": ["b-artifact-ref"],
        }

    runtime_a = DelegateRuntime(
        SpawnGuard(SpawnBudget()), goal_run_id=GOAL_A, bot_id=BOT_A
    )
    first = await runtime_a.run_cross_bot(
        request,
        operation,
        source_bot_id=BOT_A,
        target_bot_id=BOT_B,
        target_goal_run_id=GOAL_B,
    )
    assert calls == ["physical-through-b-goalrun"]

    # Simulate Supervisor restart by constructing a fresh runtime and
    # restoring the completed result from the existing GoalRun projection.
    runtime_b = DelegateRuntime(
        SpawnGuard(SpawnBudget()), goal_run_id=GOAL_A, bot_id=BOT_A
    )
    runtime_b.restore_completed([first])
    resumed = await runtime_b.run_cross_bot(
        request,
        operation,
        source_bot_id=BOT_A,
        target_bot_id=BOT_B,
        target_goal_run_id=GOAL_B,
    )
    assert resumed.delegate_id == first.delegate_id
    assert calls == ["physical-through-b-goalrun"]
    assert resumed.goal_run_id == GOAL_B
    assert resumed.source_bot_id == BOT_A
    assert resumed.target_bot_id == BOT_B


@pytest.mark.asyncio
async def test_cross_bot_request_requires_explicit_target_boundary():
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id=GOAL_A, bot_id=BOT_A)
    request = _request()
    request.target_goal_run_id = None
    with pytest.raises(ValueError, match="target GoalRun"):
        await runtime.run_cross_bot(
            request,
            lambda _cancel: None,  # type: ignore[arg-type]
            source_bot_id=BOT_A,
            target_bot_id=BOT_B,
            target_goal_run_id=GOAL_B,
        )
