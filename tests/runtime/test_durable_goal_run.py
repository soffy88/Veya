from __future__ import annotations

import pytest

from runtime.execution.runtime import DurableExecutionRuntime, DurableRuntimeConfig
from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.verify import VerifyResult


@pytest.mark.asyncio
async def test_goal_run_write_through_claims_and_resumable_finalizer(tmp_path, monkeypatch):
    runtime = DurableExecutionRuntime(
        DurableRuntimeConfig(
            enabled=True,
            sqlite_path=str(tmp_path / "durable.sqlite3"),
            queue_read=True,
            queue_claim=True,
            side_effect_ledger=True,
            reconciler_enabled=False,
            finalization_resume=True,
            event_outbox=True,
        )
    )
    import runtime.execution.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_default_runtime", runtime)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    async def fake_leaf(*, instruction: str, **_kwargs):
        return LeafResult(status="completed", summary=f"done {instruction}")

    async def fake_verify(*_args, **_kwargs):
        return VerifyResult(passed=True, summary="accepted")

    async def fake_review(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", fake_verify)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", fake_review)
    try:
        result = await project_run_goal(
            project_root=str(tmp_path),
            goal="durable goal",
            tasks=[
                {"id": "A", "instruction": "A", "acceptance": ["ok"], "parallel": True},
                {"id": "B", "instruction": "B", "acceptance": ["ok"], "parallel": True},
            ],
        )
        assert result.status == GoalStatus.completed
        durable_goal = await runtime.repository.get_goal_run(result.goal_id)
        assert durable_goal is not None and durable_goal["status"] == "completed"
        events = await runtime.repository.list_events(result.goal_id)
        assert any(event["event_type"] == "work_item.claimed" for event in events)
        assert any(event["event_type"] == "finalization.completed" for event in events)
    finally:
        await runtime.close()
