from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import cancel_goal, project_run_goal
from server.goal_run.verify import VerifyResult
from runtime.execution.runtime import DurableExecutionRuntime, DurableRuntimeConfig


@pytest_asyncio.fixture
async def durable_runtime(tmp_path, monkeypatch):
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
    monkeypatch.setattr("runtime.execution.runtime._default_runtime", runtime)
    try:
        yield runtime
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_goal_run_continuous_scheduler_refills_ready_parallel_task(tmp_path, monkeypatch, durable_runtime):
    started: list[str] = []

    async def fake_leaf(*, instruction: str, **_kwargs):
        started.append(instruction)
        await asyncio.sleep(0.5 if instruction == "A" else 0.01)
        return LeafResult(status="completed", summary=f"done {instruction}")

    async def fake_verify(*_args, **_kwargs):
        return VerifyResult(passed=True, summary="accepted")

    async def fake_review(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", fake_verify)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", fake_review)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="continuous scheduling",
        tasks=[
            {"id": "A", "instruction": "A", "acceptance": ["ok"], "parallel": True},
            {"id": "B", "instruction": "B", "acceptance": ["ok"], "parallel": True},
            {"id": "C", "instruction": "C", "acceptance": ["ok"], "depends_on": ["A"], "parallel": True},
            {"id": "D", "instruction": "D", "acceptance": ["ok"], "depends_on": ["B"], "parallel": True},
        ],
    )

    assert result.status == GoalStatus.completed
    assert started[:3] == ["A", "B", "D"]


@pytest.mark.asyncio
async def test_goal_run_failed_leaf_preserves_sibling_and_returns_partial(tmp_path, monkeypatch, durable_runtime):
    async def fake_leaf(*, instruction: str, **_kwargs):
        if instruction.startswith("failed branch"):
            return LeafResult(
                status="blocked",
                summary="evidence before crash",
                block_reason="worker crashed",
                artifacts=["outputs/partial.txt"],
                evidence=[{"id": "e1", "kind": "log", "source": "worker", "content": "evidence", "producer": "worker"}],
                unfinished_work=["finish failed branch"],
            )
        return LeafResult(status="completed", summary="sibling completed")

    async def fake_verify(*_args, **_kwargs):
        return VerifyResult(passed=True, summary="accepted")

    async def fake_review(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", fake_verify)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", fake_review)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="best effort fan in",
        tasks=[
            {"id": "A", "instruction": "failed branch", "acceptance": ["ok"], "parallel": True},
            {"id": "B", "instruction": "sibling branch", "acceptance": ["ok"], "parallel": True},
        ],
    )

    assert result.status == GoalStatus.partial_completed
    assert "unfinished" in (result.summary or "").lower()
    assert any("partial.txt" in path for path in (result.artifacts or []))


@pytest.mark.asyncio
async def test_goal_run_cancel_stops_new_work_and_finalizes(tmp_path, monkeypatch, durable_runtime):
    started = asyncio.Event()

    async def fake_leaf(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(10)
        return LeafResult(status="completed", summary="late")

    async def fake_review(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", fake_review)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    running = asyncio.create_task(
        project_run_goal(
            project_root=str(tmp_path),
            goal="cancel me",
            tasks=[{"id": "A", "instruction": "A", "acceptance": ["ok"], "parallel": True}],
        )
    )
    await started.wait()
    goal_id = next((tmp_path / ".veya-project" / "goal-runs").iterdir()).name
    requested = await cancel_goal(str(tmp_path), goal_id)
    result = await running

    assert requested.status == GoalStatus.finalizing
    assert result.status == GoalStatus.partial_completed
    assert result.goal_counts["cancelled"] == 1
