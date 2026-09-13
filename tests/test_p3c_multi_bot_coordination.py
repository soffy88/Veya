"""P3-C: Bot A owns fan-in and conflict resolution for B/C results."""

from __future__ import annotations

import asyncio

import pytest

from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.fanin import reconcile_multi_bot_results
from runtime.execution.models import Assertion, DelegateRequest, DelegateResult, Evidence
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
from server.goal_run.models import FanInState

BOT_A = "bot-a"
BOT_B = "bot-b"
BOT_C = "bot-c"


def _request(delegate_id: str, target_bot_id: str, target_goal_run_id: str) -> DelegateRequest:
    return DelegateRequest(
        delegate_id=delegate_id,
        parent_task_id="task-a",
        parent_trace_id="goal-a",
        objective="independent semantic proposal",
        context_ref="explicit-shared-ref",
        source_bot_id=BOT_A,
        target_bot_id=target_bot_id,
        target_goal_run_id=target_goal_run_id,
        bot_id=target_bot_id,
    )


@pytest.mark.asyncio
async def test_parallel_bot_results_fanin_and_owner_resolves_conflicts():
    runtime = DelegateRuntime(
        SpawnGuard(SpawnBudget(max_parallel=2)), goal_run_id="goal-a", bot_id=BOT_A
    )

    async def bot_b(_cancel):
        await asyncio.sleep(0)
        return DelegateResult(
            delegate_id="d-b",
            status="complete",
            stop_reason="completed",
            summary="B proposal",
            assertions=[Assertion("claim-1", "use plan B", producer=BOT_B)],
            evidence=[Evidence("ev-1", "analysis", BOT_B, "B", BOT_B)],
            proposed_actions=[{"logical_operation": "publish", "route": "B"}],
            side_effect_intents=[{"operation_key": "publish:goal-a", "route": "B"}],
            source_bot_id=BOT_A,
            target_bot_id=BOT_B,
            goal_run_id="goal-b",
        )

    async def bot_c(_cancel):
        return DelegateResult(
            delegate_id="d-c",
            status="complete",
            stop_reason="completed",
            summary="C proposal",
            assertions=[Assertion("claim-1", "use plan C", producer=BOT_C)],
            evidence=[Evidence("ev-1", "analysis", BOT_C, "C", BOT_C)],
            proposed_actions=[{"logical_operation": "publish", "route": "C"}],
            side_effect_intents=[{"operation_key": "publish:goal-a", "route": "C"}],
            source_bot_id=BOT_A,
            target_bot_id=BOT_C,
            goal_run_id="goal-c",
        )

    results = await asyncio.gather(
        runtime.run_cross_bot(
            _request("d-b", BOT_B, "goal-b"),
            bot_b,
            source_bot_id=BOT_A,
            target_bot_id=BOT_B,
            target_goal_run_id="goal-b",
        ),
        runtime.run_cross_bot(
            _request("d-c", BOT_C, "goal-c"),
            bot_c,
            source_bot_id=BOT_A,
            target_bot_id=BOT_C,
            target_goal_run_id="goal-c",
        ),
    )
    report = reconcile_multi_bot_results(
        results,
        owner_bot_id=BOT_A,
        owner_resolution={"owner_bot_id": BOT_A, "selected_route": "B"},
    )
    assert report["conflict_detected"] is True
    assert {item["kind"] for item in report["conflicts"]} == {
        "assertion",
        "action",
        "evidence",
        "side_effect",
    }
    assert report["resolved"] is True
    assert report["resolved_by_bot_id"] == BOT_A
    assert report["acceptance_authority"] == 0
    # Conflict data is preserved; resolution is not majority vote.
    assert len(report["conflicts"]) == 4


def test_coordination_projection_survives_restart_without_new_authority():
    state = FanInState(
        fanin_id="fanin-a",
        expected_delegate_ids=["d-b", "d-c"],
        completed_delegate_ids=["d-b", "d-c"],
        conflict_refs=["assertion:claim-1", "side_effect:publish:goal-a"],
        resolved_by_bot_id=BOT_A,
        bot_id=BOT_A,
    )
    restored = FanInState.from_dict(state.to_dict())
    assert restored.expected_delegate_ids == ["d-b", "d-c"]
    assert restored.conflict_refs == state.conflict_refs
    assert restored.resolved_by_bot_id == BOT_A


def test_conflict_resolution_rejects_non_owner_and_never_votes():
    result_b = DelegateResult(
        delegate_id="d-b",
        status="complete",
        stop_reason="completed",
        proposed_actions=[{"logical_operation": "publish", "route": "B"}],
        source_bot_id=BOT_A,
        target_bot_id=BOT_B,
        goal_run_id="goal-b",
    )
    result_c = DelegateResult(
        delegate_id="d-c",
        status="complete",
        stop_reason="completed",
        proposed_actions=[{"logical_operation": "publish", "route": "C"}],
        source_bot_id=BOT_A,
        target_bot_id=BOT_C,
        goal_run_id="goal-c",
    )
    with pytest.raises(ValueError, match="requesting MasterAgent"):
        reconcile_multi_bot_results(
            [result_b, result_c],
            owner_bot_id=BOT_A,
            owner_resolution={"owner_bot_id": BOT_C, "selected_route": "C"},
        )
