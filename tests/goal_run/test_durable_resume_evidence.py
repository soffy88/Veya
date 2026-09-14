from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runtime.execution.durable import (
    DurableExecutionError,
    DurableExecutionRepository,
    WorkItemSpec,
)
from server.goal_run.models import GoalRunState, TaskNode, TaskStatus
from server.goal_run.runner import (
    _claim_durable_goal_task,
    _prepare_durable_goal,
)

PG_DSN = os.environ.get("VEYA_EXECUTION_DATABASE_URL")


class _Repository:
    async def register_worker(self, **_kwargs):
        return None

    async def create_goal_run(self, **_kwargs):
        return None

    async def enqueue_work_item(self, *_args, **_kwargs):
        return None

    async def list_work_items(self, _goal_run_id):
        return [
            {
                "logical_key": "task-1",
                "state": "succeeded",
                "result_json": json.dumps(
                    {
                        "status": "completed",
                        "delegate_result": {"summary": "read-only result"},
                        "canonical_action": {
                            "request": {
                                "action_id": "action-1",
                                "goal_run_id": "goal-1",
                                "task_id": "task-1",
                            },
                            "status": "completed",
                        },
                    }
                ),
            }
        ]


@pytest.mark.asyncio
async def test_prepare_durable_goal_restores_canonical_action(monkeypatch):
    repository = _Repository()
    runtime = SimpleNamespace(
        config=SimpleNamespace(enabled=True, queue_claim=True),
        _started=True,
        repository=repository,
    )

    monkeypatch.setattr("runtime.execution.runtime.get_durable_runtime", lambda: runtime)

    state = GoalRunState(goal_id="goal-1", goal_text="resume")
    state.tasks["task-1"] = TaskNode(
        id="task-1",
        title="read",
        instruction="read",
        acceptance=[],
        depends_on=[],
        assignee="hicode",
        status=TaskStatus.running,
    )

    result = await _prepare_durable_goal(state)

    assert result is not None
    assert result[0] is repository
    restored = state.budget["last_canonical_action"]
    assert restored["request"]["action_id"] == "action-1"
    assert restored["request"]["goal_run_id"] == state.goal_id


@pytest.mark.asyncio
async def test_claim_durable_goal_task_waits_for_existing_retry_item(monkeypatch):
    claims = 0
    listed = 0

    class _RetryRepository:
        async def claim_next(self, *_args, **_kwargs):
            nonlocal claims
            claims += 1
            return "claim-1" if claims == 2 else None

        async def list_work_items(self, _goal_run_id):
            nonlocal listed
            listed += 1
            return [
                {
                    "logical_key": "task-1",
                    "kind": "goal_leaf",
                    "state": "retry_wait",
                    "next_ready_at": time.time() + 0.05,
                }
            ]

    sleeps: list[float] = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    claim = await _claim_durable_goal_task(
        _RetryRepository(),
        "worker-1",
        GoalRunState(goal_id="goal-1", goal_text="retry"),
        SimpleNamespace(id="task-1"),
    )

    assert claim == "claim-1"
    assert claims == 2
    assert listed == 1
    assert sleeps and 0.05 <= sleeps[0] <= 1.0


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PG_DSN or not PG_DSN.startswith(("postgres://", "postgresql://")),
    reason="requires an isolated PostgreSQL qualification database",
)
async def test_postgres_stale_finalizer_fence_rejected():
    old_repo = DurableExecutionRepository(dsn=PG_DSN, production=True)
    new_repo = DurableExecutionRepository(dsn=PG_DSN, production=True)
    run_id = f"qualification-stale-finalizer-{uuid4().hex[:12]}"
    try:
        await old_repo.connect()
        await new_repo.connect()
        await old_repo.create_goal_run(
            goal_run_id=run_id,
            status="running",
            budget={"max_parallel": 1},
            idempotency_key=run_id,
        )
        await old_repo.enqueue_work_item(
            WorkItemSpec(goal_run_id=run_id, logical_key="child", kind="agent_loop"),
            idempotency_key=f"{run_id}:child",
        )
        child = await old_repo.claim_next("qualification-child", goal_run_id=run_id)
        assert child is not None
        await old_repo.start(child)
        await old_repo.complete(child, {"summary": "durable child"})
        snapshot = await old_repo.create_fanin_snapshot(run_id)
        await old_repo.ensure_finalization_item(run_id, snapshot_hash=snapshot["manifest_hash"])

        stale = await old_repo.resume_finalization(
            run_id, worker_id="qualification-finalizer-old", lease_ttl_s=1
        )
        assert stale is not None
        await old_repo.start(stale)
        await old_repo.checkpoint_finalization(
            stale,
            snapshot_hash=snapshot["manifest_hash"],
            stage="acceptance",
            included_child_sequence=snapshot["version"],
        )
        await asyncio.sleep(1.5)
        report = await new_repo.reconcile(run_id)
        assert report.retry_safe == 1

        current = await new_repo.resume_finalization(
            run_id, worker_id="qualification-finalizer-new", lease_ttl_s=30
        )
        assert current is not None
        assert current.lease_token == stale.lease_token + 1
        await new_repo.start(current)

        with pytest.raises(
            DurableExecutionError, match="finalization lease is no longer current"
        ):
            await old_repo.complete_finalization(
                stale,
                {"answer": "stale"},
                final_status="completed",
                snapshot_hash=snapshot["manifest_hash"],
                resumed=True,
            )

        committed = await new_repo.complete_finalization(
            current,
            {"answer": "current", "incomplete_work": []},
            final_status="completed",
            snapshot_hash=snapshot["manifest_hash"],
            resumed=True,
        )
        assert committed["status"] == "committed"
    finally:
        await old_repo.close()
        await new_repo.close()
