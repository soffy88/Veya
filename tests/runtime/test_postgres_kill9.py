from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from multiprocessing import get_context

import pytest

from runtime.execution.durable import DurableExecutionRepository, WorkItemSpec


PG_DSN = os.environ.get("VEYA_EXECUTION_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not PG_DSN or not PG_DSN.startswith(("postgres://", "postgresql://")),
    reason="requires VEYA_EXECUTION_DATABASE_URL",
)


def _worker(dsn: str, run_id: str, logical_key: str | None, duration_s: float) -> None:
    async def execute() -> None:
        repo = DurableExecutionRepository(dsn=dsn, production=True)
        await repo.connect()
        worker_id = f"kill9/{logical_key or 'replacement'}/{os.getpid()}/{uuid.uuid4().hex[:8]}"
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                claim = await repo.claim_next(
                    worker_id,
                    goal_run_id=run_id,
                    logical_key=logical_key,
                    kinds={"agent_loop"},
                    lease_ttl_s=2,
                )
                if claim is None:
                    await asyncio.sleep(0.1)
                    continue
                await repo.start(claim)
                end = time.monotonic() + duration_s
                while time.monotonic() < end:
                    await asyncio.sleep(0.2)
                    await repo.heartbeat(claim, {"pid": os.getpid()}, lease_ttl_s=2)
                await repo.complete(claim, {"summary": claim.logical_key})
                if logical_key is not None:
                    return
                deadline = time.monotonic() + 1
        finally:
            await repo.close()

    asyncio.run(execute())


async def _repo() -> DurableExecutionRepository:
    repo = DurableExecutionRepository(dsn=PG_DSN, production=True)
    await repo.connect()
    return repo


@pytest.mark.asyncio
async def test_postgres_kill9_preserves_completed_and_recovers_dangling_child():
    repo = await _repo()
    ctx = get_context("fork")
    processes = []
    run_id = "test-pg-kill9-" + uuid.uuid4().hex[:12]
    try:
        await repo.create_goal_run(
            goal_run_id=run_id,
            status="running",
            budget={"max_parallel": 4},
            idempotency_key=run_id,
        )
        for logical_key in ("crash", "survivor", "queued"):
            await repo.enqueue_work_item(
                WorkItemSpec(
                    goal_run_id=run_id,
                    logical_key=logical_key,
                    kind="agent_loop",
                    parallel=True,
                    max_attempts=2,
                ),
                idempotency_key=f"{run_id}:{logical_key}",
            )
        target = ctx.Process(target=_worker, args=(PG_DSN, run_id, "crash", 20))
        survivor = ctx.Process(target=_worker, args=(PG_DSN, run_id, "survivor", 1.2))
        processes = [target, survivor]
        for process in processes:
            process.start()

        for _ in range(80):
            rows = {row["logical_key"]: row for row in await repo.list_work_items(run_id)}
            if rows["crash"]["state"] == "running" and rows["survivor"]["state"] == "running":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("workers did not reach running state")

        target_pid = target.pid
        os.kill(target_pid, signal.SIGKILL)
        target.join(timeout=3)
        # Use a deterministic database-time horizon rather than making the
        # assertion depend on scheduler jitter around the 2s lease TTL.
        # Let the non-crashed sibling commit before the deterministic scan;
        # only the SIGKILL-owned lease should be recovered.
        await asyncio.sleep(1.5)
        recovery = await repo.reconcile(run_id, now=time.time() + 5)
        assert recovery.retry_safe == 1

        replacement = ctx.Process(target=_worker, args=(PG_DSN, run_id, None, 0.1))
        replacement.start()
        processes.append(replacement)
        for _ in range(100):
            rows = await repo.list_work_items(run_id)
            if all(row["state"] == "succeeded" for row in rows):
                break
            await asyncio.sleep(0.15)
        else:
            pytest.fail("recovered workload did not finish")
        replacement.join(timeout=3)
        survivor.join(timeout=3)

        rows = {row["logical_key"]: row for row in await repo.list_work_items(run_id)}
        attempts = {key: len(await repo.list_attempts(row["id"])) for key, row in rows.items()}
        assert attempts == {"crash": 2, "survivor": 1, "queued": 1}
        events = await repo.list_events(run_id)
        assert sum(event["event_type"] == "work_item.succeeded" for event in events) == 3
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        await repo.close()
