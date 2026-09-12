from __future__ import annotations

import pytest

from runtime.execution.long_running import (
    BudgetExhausted,
    DuplicateFailedAction,
    LongRunBudget,
    LongRunCheckpointStore,
    LongRunningHarness,
    LongRunState,
    ProgressObservation,
)


def harness(tmp_path, **budget):
    state = LongRunState(goal_run_id="goal-1", computer_id="computer-1", plan=["step-1"])
    return LongRunningHarness(
        state,
        LongRunBudget(**budget),
        checkpoint_store=LongRunCheckpointStore(tmp_path / "goal-1"),
        stall_threshold=2,
    )


def test_duplicate_failed_action_replans_and_does_not_execute_again(tmp_path):
    run = harness(tmp_path)
    run.before_action("write", {"path": "a"}, {"computer": "computer-1"})
    run.record_action_failure(
        "write", {"path": "a"}, "permission denied", {"computer": "computer-1"}
    )
    with pytest.raises(DuplicateFailedAction):
        run.before_action("write", {"path": "a"}, {"computer": "computer-1"})
    assert run.state.replans == 1
    assert run.state.tool_calls == 1
    assert run.state.status == "recovering"


def test_no_progress_stall_recovers(tmp_path):
    run = harness(tmp_path)
    observation = ProgressObservation(state_hash="same", plan_step="step-1")
    assert run.record_progress(observation)
    assert not run.record_progress(observation)
    assert not run.record_progress(observation)
    assert run.state.replans == 1
    assert run.state.status == "recovering"


@pytest.mark.asyncio
async def test_provider_timeout_falls_back_without_new_goalrun(tmp_path):
    run = harness(tmp_path)
    calls = []

    async def request(provider):
        calls.append(provider)
        if provider == "primary":
            raise TimeoutError()
        return {"provider": provider, "answer": "continued"}

    result = await run.provider_call(request, ["primary", "fallback"])
    assert result["provider"] == "fallback"
    assert calls == ["primary", "fallback"]
    assert run.state.goal_run_id == "goal-1"
    assert run.state.status == "running"


def test_checkpoint_resume_preserves_goalrun_and_computer(tmp_path):
    run = harness(tmp_path)
    run.state.current_step = "step-1"
    run.state.provider_state_refs = ["provider-ref-1"]
    run.before_action("write", {"path": "a"}, {"computer": "computer-1"})
    run.record_action_failure("write", {"path": "a"}, "failed", {"computer": "computer-1"})
    run.record_progress(ProgressObservation(artifacts=["draft.md"], plan_step="step-1"))
    restored = LongRunningHarness.resume(run.checkpoint_store, run.budget, stall_threshold=2)
    assert restored.state.goal_run_id == "goal-1"
    assert restored.state.computer_id == "computer-1"
    assert restored.state.current_step == "step-1"
    assert restored.state.observations[-1]["artifacts"] == ["draft.md"]
    assert restored.state.provider_state_refs == ["provider-ref-1"]
    with pytest.raises(DuplicateFailedAction):
        restored.before_action("write", {"path": "a"}, {"computer": "computer-1"})


def test_budget_exhaustion_is_suspended_not_success(tmp_path):
    run = harness(tmp_path, max_tool_calls=1)
    run.before_action("read", {}, {})
    with pytest.raises(BudgetExhausted):
        run.before_action("read", {"again": True}, {})
    assert run.state.status == "suspended"
    assert run.state.status != "completed"


def test_verification_fail_replans_then_pass_is_only_completion(tmp_path):
    run = harness(tmp_path)
    assert run.apply_verification("FAIL", evidence={"test": "red"}) == "recovering"
    assert run.state.status != "completed"
    assert run.apply_verification("PASS", evidence={"test": "green"}) == "completed"


def test_accelerated_30_minute_synthetic_run_has_bounded_state(tmp_path):
    run = harness(tmp_path, max_wall_s=1800, max_tool_calls=240, max_replans=20)
    for index in range(180):
        run.before_action("step", {"index": index}, {"computer": run.state.computer_id})
        run.record_progress(
            ProgressObservation(
                artifacts=[f"artifact-{index}"], state_hash=f"state-{index}", plan_step="step-1"
            )
        )
    assert run.state.tool_calls == 180
    assert run.state.no_progress_rounds == 0
    assert run.state.goal_run_id == "goal-1"
    assert run.state.status != "completed"  # verification has not passed
