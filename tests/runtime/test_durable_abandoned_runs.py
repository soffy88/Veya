"""Durable fix 2026-10-14: no GoalRun stays non-terminal forever.

Prod had 24 GoalRuns stuck ``running``/``finalizing`` with 33 ``retry_wait``
items nobody would ever reclaim:

1. the verifier's non-PASS branch returned without settling the finalizer
   claim (18 runs ``finalizing``);
2. a leaf was never heart-beaten, so a slow/approval-waiting leaf lost its
   lease while still running, and a dead process left its run orphaned (6).

Offline: SQLite repository, no model.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from runtime.execution.durable import DurableExecutionRepository, WorkItemSpec
from runtime.execution.reconciler import Reconciler


async def _repo(path) -> DurableExecutionRepository:
    repo = DurableExecutionRepository(sqlite_path=path)
    await repo.connect()
    return repo


async def _goal_with_leaf(repo, goal_id: str):
    await repo.create_goal_run(
        goal_run_id=goal_id, idempotency_key=f"k-{goal_id}", status="running"
    )
    await repo.enqueue_work_item(
        WorkItemSpec(goal_run_id=goal_id, logical_key="t1", kind="goal_leaf", max_attempts=3),
        idempotency_key=f"{goal_id}:leaf",
    )


@pytest.mark.asyncio
async def test_orphaned_retry_wait_run_is_terminated(tmp_path):
    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-orphan")
        claim = await repo.claim_next("dead-worker", lease_ttl_s=1.0, goal_run_id="g-orphan")
        await repo.start(claim)
        # The worker's process dies: the reconciler classifies the expired lease.
        await repo.reconcile(now=time.time() + 5)
        items = await repo.list_work_items("g-orphan")
        assert items[0]["state"] == "retry_wait"

        # Fresh runs are left alone.
        assert await repo.close_abandoned_goal_runs(stale_after_s=3600) == []

        closed = await repo.close_abandoned_goal_runs(stale_after_s=3600, now=time.time() + 7200)
        assert closed == ["g-orphan"]
        goal = await repo.get_goal_run("g-orphan")
        assert goal["status"] == "failed"
        items = await repo.list_work_items("g-orphan")
        assert items[0]["state"] == "cancelled"
        # Idempotent.
        assert (
            await repo.close_abandoned_goal_runs(stale_after_s=3600, now=time.time() + 7200) == []
        )
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_run_with_live_lease_is_not_terminated(tmp_path):
    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-live")
        claim = await repo.claim_next("live-worker", lease_ttl_s=100000.0, goal_run_id="g-live")
        await repo.start(claim)
        later = time.time() + 7200
        assert await repo.close_abandoned_goal_runs(stale_after_s=3600, now=later) == []
        assert (await repo.get_goal_run("g-live"))["status"] == "running"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconciler_runs_the_abandoned_sweep_only_when_enabled(tmp_path):
    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-old")
        await repo.close_abandoned_goal_runs(stale_after_s=0)  # disabled: no-op
        assert (await repo.get_goal_run("g-old"))["status"] == "running"
        off = Reconciler(repo)
        await off.run_once()
        assert (await repo.get_goal_run("g-old"))["status"] == "running"
        on = Reconciler(repo, abandon_after_s=0.001)
        await asyncio.sleep(0.01)
        await on.run_once()
        assert on.last_abandoned == ["g-old"]
        assert (await repo.get_goal_run("g-old"))["status"] == "failed"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_leaf_heartbeat_keeps_the_lease_alive(tmp_path, monkeypatch):
    from server.goal_run import runner

    monkeypatch.setattr(runner, "_leaf_lease_ttl_s", lambda: 3.0)
    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-hb")
        claim = await repo.claim_next("w", lease_ttl_s=3.0, goal_run_id="g-hb")
        await repo.start(claim)
        first = (await repo.list_work_items("g-hb"))[0]["lease_expires_at"]
        async with runner._durable_lease_heartbeat(repo, claim):
            await asyncio.sleep(1.3)
        second = (await repo.list_work_items("g-hb"))[0]["lease_expires_at"]
        assert second > first
        result = await repo.complete(claim, {"ok": True})
        assert result["status"] == "committed"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_blocked_verdict_settles_the_durable_finalizer(tmp_path):
    from server.goal_run import runner
    from server.goal_run.models import GoalRunState, GoalStatus

    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-fin")
        leaf = await repo.claim_next("w", goal_run_id="g-fin")
        await repo.start(leaf)
        await repo.complete(leaf, {"ok": True})
        snapshot = await repo.create_fanin_snapshot("g-fin")
        await repo.ensure_finalization_item("g-fin", snapshot_hash=snapshot["manifest_hash"])
        claim = await repo.resume_finalization("g-fin", worker_id="w")
        await repo.start(claim)
        assert (await repo.get_goal_run("g-fin"))["status"] == "finalizing"

        state = GoalRunState(goal_id="g-fin", goal_text="x", status=GoalStatus.blocked)
        state.last_stop_reason = "Missing required evidence"
        await runner._release_durable_finalization(repo, claim, snapshot, state, summary="nope")

        goal = await repo.get_goal_run("g-fin")
        assert goal["status"] == "blocked"
        fin = [i for i in await repo.list_work_items("g-fin") if i["kind"] == "finalize"]
        assert fin and fin[0]["state"] == "succeeded"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_fail_verdict_releases_the_finalizer_claim(tmp_path):
    from server.goal_run import runner
    from server.goal_run.models import GoalRunState, GoalStatus

    repo = await _repo(tmp_path / "x.sqlite3")
    try:
        await _goal_with_leaf(repo, "g-re")
        leaf = await repo.claim_next("w", goal_run_id="g-re")
        await repo.start(leaf)
        await repo.complete(leaf, {"ok": True})
        snapshot = await repo.create_fanin_snapshot("g-re")
        await repo.ensure_finalization_item("g-re", snapshot_hash=snapshot["manifest_hash"])
        claim = await repo.resume_finalization("g-re", worker_id="w")
        await repo.start(claim)
        state = GoalRunState(goal_id="g-re", goal_text="x", status=GoalStatus.recovering)
        await runner._release_durable_finalization(repo, claim, snapshot, state, summary="fail")
        fin = [i for i in await repo.list_work_items("g-re") if i["kind"] == "finalize"]
        assert fin[0]["state"] == "retry_wait"
        assert fin[0]["lease_owner"] in (None, "")
    finally:
        await repo.close()
