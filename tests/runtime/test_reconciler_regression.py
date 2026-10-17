"""Regression tests for Durable Execution Reconciler fixes (P1-2).

Ensures:
1. Abandoned GoalRuns and orphaned work items are closed according to business semantics
   and reflected in ReconciliationReport.scanned / decisions (not 0).
2. Stale worker_registry entries with expired heartbeats and no active leases are pruned.
3. Work items that have exhausted max_attempts transition to failed rather than staying
   in retry_wait forever.
4. Clean idle queues report scanned: 0 as expected.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from runtime.execution.durable import DurableExecutionRepository, WorkItemSpec
from runtime.execution.reconciler import Reconciler


async def _make_repo(tmp_path) -> DurableExecutionRepository:
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "reconciler_test.sqlite3")
    await repo.connect()
    return repo


@pytest.mark.asyncio
async def test_reconciler_scans_and_reports_abandoned_goal_runs(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        goal_id = "goal_abandoned_test"
        await repo.create_goal_run(goal_run_id=goal_id, idempotency_key=f"k-{goal_id}", status="running")
        await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id=goal_id, logical_key="t1", kind="goal_leaf", max_attempts=3),
            idempotency_key=f"{goal_id}:leaf",
        )
        claim = await repo.claim_next("worker_lost", lease_ttl_s=1.0, goal_run_id=goal_id)
        await repo.start(claim)

        # Lease expires after 2s
        await repo.reconcile(now=time.time() + 5.0)
        items = await repo.list_work_items(goal_id)
        assert items[0]["state"] == "retry_wait"

        # Backdate goal_run and work_item updated_at so they exceed abandon_after_s
        past = time.time() - 10.0
        def backdate_goal(conn):
            conn.execute("UPDATE goal_runs SET updated_at=?", (past,))
            conn.execute("UPDATE work_items SET updated_at=?", (past,))
        await asyncio.to_thread(repo._sqlite_tx, backdate_goal)

        # Reconciler with abandon_after_s enabled runs sweep
        reconciler = Reconciler(repo, abandon_after_s=0.001)
        await asyncio.sleep(0.01)
        report = await reconciler.run_once()

        assert report.scanned >= 1
        assert report.abandoned_closed == 1
        assert any(d.get("decision") == "ABANDONED_CLOSED" for d in report.decisions)

        goal = await repo.get_goal_run(goal_id)
        assert goal["status"] == "failed"

        items = await repo.list_work_items(goal_id)
        assert items[0]["state"] == "cancelled"
        assert "abandoned" in items[0]["error_json"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconciler_prunes_stale_workers_without_leases(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        now = time.time()
        # Worker 1: Stale heartbeat, no active lease -> should be pruned
        await repo.register_worker("w_stale_orphan", process_id="pid_1")

        # Worker 2: Stale heartbeat, but holds active lease -> must NOT be pruned
        goal_id = "goal_active_worker"
        await repo.create_goal_run(goal_run_id=goal_id, idempotency_key=f"k-{goal_id}", status="running")
        await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id=goal_id, logical_key="t2", kind="goal_leaf", max_attempts=3),
            idempotency_key=f"{goal_id}:leaf",
        )
        await repo.register_worker("w_stale_leased", process_id="pid_2")
        claim = await repo.claim_next("w_stale_leased", lease_ttl_s=10000.0, goal_run_id=goal_id)
        await repo.start(claim)

        # Worker 3: Recent heartbeat -> must NOT be pruned
        await repo.register_worker("w_recent", process_id="pid_3")

        # Manually backdate Worker 1 and Worker 2 last_seen_at in SQLite
        def backdate(conn):
            conn.execute("UPDATE worker_registry SET last_seen_at = ? WHERE worker_id IN ('w_stale_orphan', 'w_stale_leased')", (now - 600,))
        await asyncio.to_thread(repo._sqlite_tx, backdate)

        reconciler = Reconciler(repo, worker_ttl_s=300.0)
        report = await reconciler.run_once()

        assert "w_stale_orphan" in reconciler.last_pruned_workers
        assert "w_stale_leased" not in reconciler.last_pruned_workers
        assert "w_recent" not in reconciler.last_pruned_workers
        assert report.workers_pruned == 1
        assert report.scanned >= 1

        # Verify directly in DB
        def check_workers(conn):
            return {r["worker_id"] for r in conn.execute("SELECT worker_id FROM worker_registry").fetchall()}
        remaining = await asyncio.to_thread(repo._sqlite_tx, check_workers)
        assert "w_stale_orphan" not in remaining
        assert "w_stale_leased" in remaining
        assert "w_recent" in remaining
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconciler_exhausted_retry_wait_moves_to_failed(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        goal_id = "goal_exhausted_test"
        await repo.create_goal_run(goal_run_id=goal_id, idempotency_key=f"k-{goal_id}", status="running")
        await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id=goal_id, logical_key="t_ex", kind="goal_leaf", max_attempts=1),
            idempotency_key=f"{goal_id}:leaf",
        )
        claim = await repo.claim_next("w1", lease_ttl_s=1.0, goal_run_id=goal_id)
        await repo.start(claim)

        # Expire the lease. Since max_attempts=1 and attempt_count=1, reconcile must fail it
        report = await repo.reconcile(now=time.time() + 5.0)
        assert report.scanned == 1
        assert any(d.get("decision") == "MAX_ATTEMPTS_EXCEEDED" for d in report.decisions)

        items = await repo.list_work_items(goal_id)
        assert items[0]["state"] == "failed"
        assert items[0]["recovery_state"] == "classified"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconciler_idle_queue_clean_scanned_zero(tmp_path):
    repo = await _make_repo(tmp_path)
    try:
        reconciler = Reconciler(repo, abandon_after_s=300.0, worker_ttl_s=300.0)
        report = await reconciler.run_once()
        assert report.scanned == 0
        assert report.abandoned_closed == 0
        assert report.workers_pruned == 0
        assert len(report.decisions) == 0
    finally:
        await repo.close()
