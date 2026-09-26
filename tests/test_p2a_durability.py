"""P2-A durability: Delegate/FanIn/Playbook/Routine persist in one GoalRun.

No second scheduler, no nested GoalRun, no second execution or acceptance
authority. Physical work always funnels through
CanonicalActionRequest → GoalRun → ActionGateway; subagent self-reports are
never treated as verified success.
"""

from __future__ import annotations

import pytest

from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.durable import (
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.fanin import fan_in
from runtime.execution.models import (
    DELEGATE_RESULT_ACCEPTANCE_AUTHORITY,
    PARALLEL_EXECUTION_AUTHORITY_COUNT,
    PLAYBOOK_ACCEPTANCE_AUTHORITY,
    ROUTINE_ACCEPTANCE_AUTHORITY,
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    SUBAGENT_ACCEPTANCE_AUTHORITY,
    SUBAGENT_EXECUTION_AUTHORITY,
    SUBAGENT_SELF_REPORT_AUTHORITY,
    WORKER_SELF_REPORT_AUTHORITY,
    DelegateRequest,
    DelegateResult,
    Evidence,
    reconcile_delegate_results,
)
from runtime.execution.side_effects import SideEffectLedger
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
from server.goal_run.models import (
    GOALRUN_DURABLE_AUTHORITY,
    P2_DURABLE_SCHEDULER_COUNT,
    PERSISTENCE_PROJECTION_FIELDS,
    DelegateState,
    FanInState,
    GoalRunState,
    PlaybookState,
    RoutineState,
)
from server.goal_run.store import load_goal_run, save_goal_run


def _seed_goal_run() -> GoalRunState:
    state = GoalRunState(goal_id="goal-p2a", goal_text="p2a durability")
    state.delegate_states["d1"] = DelegateState(
        delegate_id="d1",
        parent_goal_run_id="goal-p2a",
        status="complete",
        request_ref="d1",
        result_ref="d1",
        evidence_refs=["sha:e1"],
    )
    state.fanin_states["f1"] = FanInState(
        fanin_id="f1",
        expected_delegate_ids=["d1", "d2"],
        completed_delegate_ids=["d1"],
        reconciled_result_ref="sha:r1",
    )
    state.playbook_id = "p1"
    state.playbook_steps = {"s1": {"status": "done"}, "s2": {"status": "active"}}
    state.current_playbook_step = "s2"
    state.playbook_states["p1"] = PlaybookState(
        playbook_id="p1",
        active_step="s2",
        completed_steps=["s1"],
        step_result_refs={"s1": "sha:s1"},
        evidence_refs=["sha:e1"],
    )
    state.routine_states["r1"] = RoutineState(
        routine_id="r1",
        trigger_metadata={"topic": "schedule.trigger"},
        started_count=1,
        canonical_goal_run_id="goal-p2a",
    )
    return state


def test_delegate_fanin_playbook_evidence_preserved(tmp_path):
    state = _seed_goal_run()
    save_goal_run(state, str(tmp_path))
    restored = load_goal_run(str(tmp_path), "goal-p2a")
    assert restored is not None
    assert restored.delegate_states["d1"].evidence_refs == ["sha:e1"]
    assert restored.fanin_states["f1"].reconciled_result_ref == "sha:r1"
    assert restored.playbook_states["p1"].completed_steps == ["s1"]
    assert restored.playbook_states["p1"].evidence_refs == ["sha:e1"]
    assert restored.routine_states["r1"].canonical_goal_run_id == "goal-p2a"
    assert P2_DURABLE_SCHEDULER_COUNT == 0
    assert GOALRUN_DURABLE_AUTHORITY == 1
    assert (
        frozenset({"delegate_states", "fanin_states", "playbook_states", "routine_states"})
        == PERSISTENCE_PROJECTION_FIELDS
    )


def test_playbook_two_step_same_goalrun(tmp_path):
    state = GoalRunState(goal_id="goal-pb", goal_text="two steps, one run")
    state.playbook_id = "p1"
    state.playbook_steps = {"s1": {"status": "pending"}, "s2": {"status": "pending"}}
    # Step 1 completes in the SAME GoalRun — no nested run is created.
    state.current_playbook_step = "s1"
    state.playbook_states["p1"] = PlaybookState(
        playbook_id="p1", active_step="s2", completed_steps=["s1"]
    )
    state.playbook_steps["s1"]["status"] = "done"
    # Step 2 completes in the SAME GoalRun.
    state.current_playbook_step = "s2"
    done = state.playbook_states["p1"]
    done.completed_steps.append("s2")
    done.active_step = None
    state.playbook_steps["s2"]["status"] = "done"
    save_goal_run(state, str(tmp_path))
    restored = load_goal_run(str(tmp_path), "goal-pb")
    assert restored is not None
    assert restored.goal_id == "goal-pb"
    assert restored.playbook_states["p1"].completed_steps == ["s1", "s2"]
    assert restored.current_playbook_step == "s2"


def test_playbook_restart_resume(tmp_path):
    state = GoalRunState(goal_id="goal-resume", goal_text="restart mid playbook")
    state.playbook_id = "p1"
    state.playbook_steps = {"s1": {"status": "done"}, "s2": {"status": "active"}}
    state.current_playbook_step = "s2"
    state.playbook_states["p1"] = PlaybookState(
        playbook_id="p1", active_step="s2", completed_steps=["s1"]
    )
    save_goal_run(state, str(tmp_path))
    # A new supervisor process loads the SAME GoalRun and resumes step 2.
    resumed = load_goal_run(str(tmp_path), "goal-resume")
    assert resumed is not None
    assert resumed.goal_id == "goal-resume"
    assert resumed.current_playbook_step == "s2"
    resumed.playbook_states["p1"].completed_steps.append("s2")
    resumed.playbook_states["p1"].active_step = None
    save_goal_run(resumed, str(tmp_path))
    final = load_goal_run(str(tmp_path), "goal-resume")
    assert final is not None
    assert final.playbook_states["p1"].completed_steps == ["s1", "s2"]


async def _run_delegate(runtime: DelegateRuntime, delegate_id: str, summary: str):
    request = DelegateRequest(
        delegate_id=delegate_id,
        parent_task_id="goal-par",
        parent_trace_id="goal-par",
        objective=f"objective-{delegate_id}",
    )

    async def operation(_cancel):
        return DelegateResult(
            delegate_id=delegate_id,
            status="complete",
            stop_reason="completed",
            summary=summary,
            evidence=[
                Evidence(
                    id=f"e-{delegate_id}",
                    kind="note",
                    source="worker",
                    content=f"evidence-{delegate_id}",
                    producer="worker",
                    sha256=f"sha:{delegate_id}",
                )
            ],
        )

    return await runtime.run(request, operation)


async def test_parallel_delegation_restart_resume(tmp_path):
    projections: dict[str, dict] = {}
    runtime = DelegateRuntime(
        SpawnGuard(SpawnBudget()),
        goal_run_id="goal-par",
        on_delegate_state=lambda item: projections.update({item["delegate_id"]: item}),
    )
    first = await _run_delegate(runtime, "d1", "one")
    second = await _run_delegate(runtime, "d2", "two")
    assert first.status == "complete" and second.status == "complete"

    state = GoalRunState(goal_id="goal-par", goal_text="parallel restart")
    for delegate_id, item in projections.items():
        state.delegate_states[delegate_id] = DelegateState.from_dict(
            {"parent_goal_run_id": "goal-par", **item}
        )
    save_goal_run(state, str(tmp_path))
    # Restart: a fresh runtime reloads persisted delegate states and reconciles.
    restored = load_goal_run(str(tmp_path), "goal-par")
    assert restored is not None
    assert set(restored.delegate_states) == {"d1", "d2"}
    assert all(item.parent_goal_run_id == "goal-par" for item in restored.delegate_states.values())
    reconciled = reconcile_delegate_results([first, second])
    assert reconciled["overall_status"] == "success"
    assert reconciled["complete_count"] == 2
    batch = fan_in([first, second])
    assert batch.complete_count == 2
    assert {item.sha256 for item in batch.evidence} == {"sha:d1", "sha:d2"}


async def test_no_duplicate_delegate_execution():
    calls: list[str] = []
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-dedup")
    request = DelegateRequest(
        delegate_id="d1",
        parent_task_id="goal-dedup",
        parent_trace_id="goal-dedup",
        objective="once",
    )

    async def operation(_cancel):
        calls.append("ran")
        return DelegateResult(
            delegate_id="d1", status="complete", stop_reason="completed", summary="ok"
        )

    first = await runtime.run(request, operation)
    second = await runtime.run(request, operation)
    assert first.status == "complete"
    assert second.summary == "ok"
    assert calls == ["ran"]


async def test_no_duplicate_side_effects(tmp_path):
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "ledger.sqlite3")
    await repo.connect()
    try:
        await repo.create_goal_run(goal_run_id="run-dedup", idempotency_key="run-dedup")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-dedup", logical_key="publish", kind="tool")
        )
        operation_key = build_operation_key("run-dedup", item["id"], "publish")
        calls = 0

        async def provider():
            nonlocal calls
            calls += 1
            return "published"

        ledger = SideEffectLedger(repo)
        first = await ledger.execute(
            goal_run_id="run-dedup",
            work_item_id=item["id"],
            operation_key=operation_key,
            operation_type="publish",
            target_ref="provider:item",
            request={"value": 1},
            provider=provider,
        )
        second = await ledger.execute(
            goal_run_id="run-dedup",
            work_item_id=item["id"],
            operation_key=operation_key,
            operation_type="publish",
            target_ref="provider:item",
            request={"value": 1},
            provider=provider,
        )
        assert first == "published"
        assert second == "published"
        assert calls == 1
    finally:
        await repo.close()


async def test_delegate_runtime_same_goalrun_and_canonical_path():
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-same")
    foreign = DelegateRequest(
        delegate_id="dx",
        parent_task_id="goal-same",
        parent_trace_id="goal-other",
        objective="cross-run",
    )

    async def operation(_cancel):
        return DelegateResult(
            delegate_id="dx", status="complete", stop_reason="completed", summary="x"
        )

    with pytest.raises(ValueError, match="different GoalRun"):
        await runtime.run(foreign, operation)
    mine = DelegateRequest(
        delegate_id="dx",
        parent_task_id="goal-same",
        parent_trace_id="goal-same",
        objective="mine",
    )
    with pytest.raises(AssertionError, match="SUBAGENT_DIRECT_PHYSICAL_EXECUTION"):
        await runtime.run(mine, operation, physical_action_count=1)


def test_authority_zeros_and_self_report_excluded():
    assert SUBAGENT_EXECUTION_AUTHORITY == 0
    assert SUBAGENT_ACCEPTANCE_AUTHORITY == 0
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0
    assert DELEGATE_RESULT_ACCEPTANCE_AUTHORITY == 0
    assert PLAYBOOK_ACCEPTANCE_AUTHORITY == 0
    assert ROUTINE_ACCEPTANCE_AUTHORITY == 0
    assert SUBAGENT_SELF_REPORT_AUTHORITY == 0
    assert WORKER_SELF_REPORT_AUTHORITY == 0
    assert PARALLEL_EXECUTION_AUTHORITY_COUNT == 1
    assert P2_DURABLE_SCHEDULER_COUNT == 0

    real = DelegateResult(
        delegate_id="ok", status="complete", stop_reason="completed", summary="ok"
    )
    claimed = DelegateResult(
        delegate_id="self", status="complete", stop_reason="completed", summary="self"
    )
    claimed.self_report = True  # type: ignore[attr-defined]
    reconciled = reconcile_delegate_results([claimed])
    assert reconciled["overall_status"] == "blocked"
    assert reconciled["complete_count"] == 0
    reconciled = reconcile_delegate_results([claimed, real])
    assert reconciled["overall_status"] == "success"
    assert reconciled["complete_count"] == 1
