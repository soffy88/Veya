from __future__ import annotations

import asyncio

import pytest

from runtime.execution.durable import ClaimEnvelope, DurableExecutionRepository


@pytest.mark.asyncio
async def test_lease_fencing_prevents_stale_owner_mutations(tmp_path):
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "fencing.sqlite3")
    await repo.connect()
    try:
        await repo.create_goal_run(goal_run_id="run-fence", idempotency_key="run-fence")
        item = await repo.enqueue_work_item(
            {
                "goal_run_id": "run-fence",
                "logical_key": "task-1",
                "kind": "agent_loop",
                "max_attempts": 2,
            }
        )

        old_claim = await repo.claim_next("worker-1", lease_ttl_s=1)
        assert old_claim is not None
        assert old_claim.lease_token == 1

        # Simulate lease expiration & reconciler reassignment
        report = await repo.reconcile("run-fence", now=old_claim.lease_expires_at + 1)
        assert report.retry_safe == 1

        new_claim = await repo.claim_next("worker-2", lease_ttl_s=30)
        assert new_claim is not None
        assert new_claim.lease_token == 2

        # Stale worker-1 attempts start/heartbeat/complete -> all fenced out
        with pytest.raises(Exception, match="claim is no longer current|fencing token is no longer current"):
            await repo.start(old_claim)

        with pytest.raises(Exception, match="heartbeat rejected|heartbeat is no longer current"):
            await repo.heartbeat(old_claim)

        with pytest.raises(Exception, match="claim is no longer current|fencing token is no longer current"):
            await repo.complete(old_claim, {"summary": "stale work"})

        # New worker-2 completes successfully
        await repo.start(new_claim)
        res = await repo.complete(new_claim, {"summary": "fresh work"})
        assert res["status"] == "committed"

        events = await repo.list_events("run-fence")
        fenced_events = [e for e in events if e["event_type"] == "work_item.fenced_out"]
        assert len(fenced_events) >= 3
    finally:
        await repo.close()
