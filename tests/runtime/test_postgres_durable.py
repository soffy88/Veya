from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from runtime.execution.durable import (
    DurableExecutionError,
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.side_effects import SideEffectLedger

PG_DSN = os.environ.get("VEYA_EXECUTION_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not PG_DSN or not PG_DSN.startswith(("postgres://", "postgresql://")),
    reason="requires VEYA_EXECUTION_DATABASE_URL",
)


async def _repo() -> DurableExecutionRepository:
    repo = DurableExecutionRepository(dsn=PG_DSN, production=True)
    await repo.connect()
    return repo


@pytest.mark.asyncio
async def test_postgres_global_parallel_limit_and_continuous_refill():
    seed = await _repo()
    peers = [await _repo() for _ in range(3)]
    run_id = "test-pg-global-" + uuid.uuid4().hex[:12]
    try:
        await seed.create_goal_run(
            goal_run_id=run_id,
            status="running",
            budget={"max_parallel": 4},
            idempotency_key=run_id,
        )
        for index in range(6):
            await seed.enqueue_work_item(
                WorkItemSpec(
                    goal_run_id=run_id,
                    logical_key=f"parallel-{index}",
                    kind="agent_loop",
                    parallel=True,
                ),
                idempotency_key=f"{run_id}:{index}",
            )

        claims = [
            claim
            for claim in await asyncio.gather(
                *(
                    peers[index % len(peers)].claim_next(
                        f"pg-global-worker-{index}",
                        goal_run_id=run_id,
                        lease_ttl_s=30,
                    )
                    for index in range(8)
                )
            )
            if claim is not None
        ]
        assert len(claims) == 4
        assert len({claim.work_item_id for claim in claims}) == 4
        run_items = await seed.list_work_items(run_id)
        assert sum(item["state"] in {"leased", "running"} for item in run_items) == 4

        await seed.start(claims[0])
        await seed.complete(claims[0], {"summary": claims[0].logical_key})
        refill = await peers[0].claim_next(
            "pg-global-refill", goal_run_id=run_id, lease_ttl_s=30
        )
        assert refill is not None
        run_items = await seed.list_work_items(run_id)
        assert sum(item["state"] in {"leased", "running"} for item in run_items) == 4

        for claim in [*claims[1:], refill]:
            await seed.start(claim)
            await seed.complete(claim, {"summary": claim.logical_key})
        remaining = await seed.list_work_items(run_id)
        assert sum(item["state"] == "succeeded" for item in remaining) == 5
        assert sum(item["state"] == "ready" for item in remaining) == 1
    finally:
        await asyncio.gather(seed.close(), *(repo.close() for repo in peers))


@pytest.mark.asyncio
async def test_postgres_expiry_recovery_and_stale_fence_rejection():
    old_repo = await _repo()
    new_repo = await _repo()
    run_id = "test-pg-fence-" + uuid.uuid4().hex[:12]
    try:
        await old_repo.create_goal_run(
            goal_run_id=run_id, status="running", idempotency_key=run_id
        )
        item = await old_repo.enqueue_work_item(
            WorkItemSpec(
                goal_run_id=run_id,
                logical_key="fenced",
                kind="tool",
                parallel=True,
                max_attempts=2,
            ),
            idempotency_key=f"{run_id}:item",
        )
        old = await old_repo.claim_next("pg-old", goal_run_id=run_id, lease_ttl_s=1)
        assert old is not None
        await old_repo.start(old)
        # Leave enough margin above the one-second lease TTL for CI/database
        # scheduling jitter; the assertion is about expiry classification.
        await asyncio.sleep(1.5)
        report = await new_repo.reconcile(run_id)
        assert report.retry_safe == 1
        current = await new_repo.claim_next("pg-new", goal_run_id=run_id, lease_ttl_s=30)
        assert current is not None
        assert current.lease_token == old.lease_token + 1
        await new_repo.start(current)

        with pytest.raises(DurableExecutionError, match="heartbeat rejected"):
            await old_repo.heartbeat(old)
        with pytest.raises(DurableExecutionError, match="checkpoint rejected"):
            await old_repo.checkpoint(old, {"stale": True})
        with pytest.raises(DurableExecutionError, match="artifact claim"):
            await old_repo.register_artifact(
                goal_run_id=run_id,
                work_item_id=item["id"],
                content_uri=f"test-pg-fence/{run_id}/stale.txt",
                content_hash_value="sha256:" + "f" * 64,
                size_bytes=1,
                mime_type="text/plain",
                kind="evidence",
                claim=old,
            )
        with pytest.raises(DurableExecutionError, match="lease owner"):
            await old_repo.complete(old, {"stale": True})
        await new_repo.complete(current, {"summary": "new owner"})
        assert (await new_repo.complete(current, {"summary": "new owner"}))["status"] == "idempotent"
        assert (await new_repo.metrics())["fencing_rejected"] >= 4
        assert item["id"] == current.work_item_id
    finally:
        await old_repo.close()
        await new_repo.close()


@pytest.mark.asyncio
async def test_postgres_side_effect_probe_and_quarantine():
    repo = await _repo()
    ledger = SideEffectLedger(repo)
    run_id = "test-pg-effect-" + uuid.uuid4().hex[:12]
    try:
        await repo.create_goal_run(
            goal_run_id=run_id, status="running", idempotency_key=run_id
        )
        item = await repo.enqueue_work_item(
            WorkItemSpec(
                goal_run_id=run_id,
                logical_key="effect",
                kind="tool",
                side_effect_policy="probe_required",
            ),
            idempotency_key=f"{run_id}:item",
        )
        key = build_operation_key(run_id, item["id"], "publish")
        await repo.declare_side_effect(
            goal_run_id=run_id,
            work_item_id=item["id"],
            operation_key=key,
            operation_type="publish",
            target_ref="test-provider",
            request={"value": 1},
            capability="status_probe",
        )
        await repo.update_side_effect(key, state="unknown")
        calls = {"count": 0}
        result = await ledger.execute(
            goal_run_id=run_id,
            work_item_id=item["id"],
            operation_key=key,
            operation_type="publish",
            target_ref="test-provider",
            request={"value": 1},
            capability="status_probe",
            probe=lambda: {"status": "committed", "result": "already-applied"},
            provider=lambda: calls.__setitem__("count", calls["count"] + 1),
        )
        assert result == "already-applied"
        assert calls["count"] == 0

        quarantine_item = await repo.enqueue_work_item(
            WorkItemSpec(
                goal_run_id=run_id,
                logical_key="quarantine",
                kind="tool",
                max_attempts=2,
                side_effect_policy="probe_required",
            ),
            idempotency_key=f"{run_id}:quarantine",
        )
        quarantine_claim = await repo.claim_next(
            "pg-effect-quarantine",
            goal_run_id=run_id,
            logical_key="quarantine",
            lease_ttl_s=1,
        )
        assert quarantine_claim is not None
        await repo.start(quarantine_claim)
        second_key = build_operation_key(run_id, quarantine_item["id"], "publish-unknown")
        await repo.declare_side_effect(
            goal_run_id=run_id,
            work_item_id=quarantine_item["id"],
            operation_key=second_key,
            operation_type="publish",
            target_ref="test-provider",
            request={"value": 2},
            capability="status_probe",
            claim=quarantine_claim,
        )
        await repo.update_side_effect(second_key, state="started", claim=quarantine_claim)
        # Keep margin over the one-second lease TTL for database scheduling
        # jitter; this assertion is about reconciliation after expiry.
        await asyncio.sleep(1.5)
        report = await repo.reconcile(run_id)
        assert report.quarantined == 1
        row = next(
            work_item
            for work_item in await repo.list_work_items(run_id)
            if work_item["logical_key"] == "quarantine"
        )
        assert row["state"] == "quarantined_unknown"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_postgres_finalization_resume_from_checkpoint():
    first = await _repo()
    second = await _repo()
    run_id = "test-pg-finalize-" + uuid.uuid4().hex[:12]
    try:
        await first.create_goal_run(
            goal_run_id=run_id, status="running", idempotency_key=run_id
        )
        await first.enqueue_work_item(
            WorkItemSpec(goal_run_id=run_id, logical_key="child", kind="agent_loop"),
            idempotency_key=f"{run_id}:child",
        )
        child = await first.claim_next("pg-child", goal_run_id=run_id, lease_ttl_s=30)
        assert child is not None
        await first.start(child)
        await first.complete(child, {"summary": "child durable"})
        snapshot = await first.create_fanin_snapshot(run_id)
        await first.ensure_finalization_item(run_id, snapshot_hash=snapshot["manifest_hash"])
        stale_finalizer = await first.resume_finalization(
            run_id, worker_id="pg-finalizer-old", lease_ttl_s=1
        )
        assert stale_finalizer is not None
        await first.start(stale_finalizer)
        await first.checkpoint_finalization(
            stale_finalizer,
            snapshot_hash=snapshot["manifest_hash"],
            stage="acceptance",
            included_child_sequence=snapshot["version"],
        )
        await asyncio.sleep(1.2)
        report = await second.reconcile(run_id)
        assert report.retry_safe == 1
        resumed = await second.resume_finalization(
            run_id, worker_id="pg-finalizer-new", lease_ttl_s=30
        )
        assert resumed is not None
        assert resumed.lease_token == stale_finalizer.lease_token + 1
        await second.start(resumed)
        committed = await second.complete_finalization(
            resumed,
            {"answer": "resumed", "incomplete_work": []},
            final_status="completed",
            snapshot_hash=snapshot["manifest_hash"],
            resumed=True,
        )
        assert committed["status"] == "committed"
        goal = await second.get_goal_run(run_id)
        assert goal is not None and goal["status"] == "completed"
        assert len(await second.list_attempts(resumed.work_item_id)) == 2
    finally:
        await first.close()
        await second.close()
