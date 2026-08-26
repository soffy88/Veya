from __future__ import annotations

import asyncio

import pytest

from runtime.execution.durable import (
    DurableExecutionError,
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.side_effects import SideEffectLedger
from runtime.execution.worker import WorkerHost


async def _repo(path) -> DurableExecutionRepository:
    repo = DurableExecutionRepository(sqlite_path=path)
    await repo.connect()
    return repo


@pytest.mark.asyncio
async def test_enqueue_claim_dependencies_and_idempotent_completion(tmp_path):
    repo = await _repo(tmp_path / "execution.sqlite3")
    try:
        run = await repo.create_goal_run(goal_run_id="run-1", idempotency_key="run-key")
        assert run["id"] == "run-1"
        first = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-1", logical_key="A", kind="agent_loop", payload={"q": "a"}, max_attempts=2),
            idempotency_key="item-a",
        )
        duplicate = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-1", logical_key="A", kind="agent_loop", payload={"q": "different"}),
            idempotency_key="item-a",
        )
        assert duplicate["id"] == first["id"]
        await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-1", logical_key="B", kind="hicode", depends_on=["A"]),
        )

        claim = await repo.claim_next("worker-a")
        assert claim is not None and claim.logical_key == "A"
        await repo.start(claim)
        await repo.heartbeat(claim, {"stage": "working"})
        checkpoint_id = await repo.checkpoint(claim, {"stage": "read"})
        assert checkpoint_id
        result = await repo.complete(claim, {"summary": "A done"})
        assert result["status"] == "committed"
        assert (await repo.complete(claim, {"summary": "A done"}))['status'] == "idempotent"

        dependent = await repo.claim_next("worker-b")
        assert dependent is not None and dependent.logical_key == "B"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cross_process_claim_storm_has_one_owner_per_item(tmp_path):
    path = tmp_path / "storm.sqlite3"
    seed = await _repo(path)
    await seed.create_goal_run(goal_run_id="run-storm", idempotency_key="run-storm")
    for index in range(8):
        await seed.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-storm", logical_key=f"w-{index}", kind="agent_loop", parallel=True),
        )
    await seed.close()

    workers = [await _repo(path) for _ in range(4)]
    try:
        claims = await asyncio.gather(*(repo.claim_next(f"worker-{index}") for index, repo in enumerate(workers)))
        claimed_ids = [claim.work_item_id for claim in claims if claim is not None]
        assert len(claimed_ids) == len(set(claimed_ids)) == 4
        assert all(claim.lease_token == 1 for claim in claims if claim is not None)
    finally:
        await asyncio.gather(*(repo.close() for repo in workers))


@pytest.mark.asyncio
async def test_durable_claim_enforces_per_goal_parallel_limit_and_serial_isolation(tmp_path):
    repo = await _repo(tmp_path / "claim-budget.sqlite3")
    try:
        await repo.create_goal_run(
            goal_run_id="run-claim-budget",
            idempotency_key="run-claim-budget",
            budget={"max_parallel": 2},
        )
        for logical_key in ("A", "B", "C"):
            await repo.enqueue_work_item(
                WorkItemSpec(
                    goal_run_id="run-claim-budget",
                    logical_key=logical_key,
                    kind="agent_loop",
                    parallel=True,
                )
            )
        first = await repo.claim_next("worker-a")
        second = await repo.claim_next("worker-b")
        assert first is not None and second is not None
        assert await repo.claim_next("worker-c") is None
        await repo.start(first)
        await repo.complete(first, {"summary": "A"})
        third = await repo.claim_next("worker-c")
        assert third is not None

        await repo.create_goal_run(goal_run_id="run-serial", idempotency_key="run-serial", budget={"max_parallel": 4})
        await repo.enqueue_work_item(WorkItemSpec(goal_run_id="run-serial", logical_key="parallel", kind="agent_loop", parallel=True))
        await repo.enqueue_work_item(WorkItemSpec(goal_run_id="run-serial", logical_key="serial", kind="hicode", parallel=False))
        running_parallel = await repo.claim_next("worker-parallel")
        assert running_parallel is not None
        assert await repo.claim_next("worker-serial") is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_expired_lease_is_retried_but_stale_worker_is_fenced(tmp_path):
    path = tmp_path / "recovery.sqlite3"
    repo = await _repo(path)
    try:
        await repo.create_goal_run(goal_run_id="run-recovery", idempotency_key="run-recovery")
        await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-recovery", logical_key="safe", kind="hicode", max_attempts=2),
        )
        old = await repo.claim_next("old-worker", lease_ttl_s=1)
        assert old is not None
        report = await repo.reconcile("run-recovery", now=old.lease_expires_at + 1)
        assert report.retry_safe == 1
        with pytest.raises(DurableExecutionError, match="heartbeat rejected"):
            await repo.heartbeat(old)
        new = await repo.claim_next("new-worker")
        assert new is not None and new.lease_token == 2
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_unknown_side_effect_is_quarantined_and_key_conflicts(tmp_path):
    repo = await _repo(tmp_path / "effects.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-effects", idempotency_key="run-effects")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-effects", logical_key="publish", kind="tool", side_effect_policy="manual_on_unknown", max_attempts=2),
        )
        key = build_operation_key("run-effects", item["id"], "publish")
        await repo.declare_side_effect(
            goal_run_id="run-effects", work_item_id=item["id"], operation_key=key,
            operation_type="publish", target_ref="provider:item", request={"value": 1}, capability="manual_only",
        )
        with pytest.raises(DurableExecutionError, match="different request hash"):
            await repo.declare_side_effect(
                goal_run_id="run-effects", work_item_id=item["id"], operation_key=key,
                operation_type="publish", target_ref="provider:item", request={"value": 2}, capability="manual_only",
            )
        claim = await repo.claim_next("worker-effects", lease_ttl_s=1)
        assert claim is not None
        await repo.start(claim)
        await repo.update_side_effect(key, state="started", provider_request_id="provider-1")
        await repo.fail(claim, {"message": "response lost"}, classification="unknown")
        # The failure itself releases the lease; recovery remains explicit in
        # the durable work-item state and is not silently requeued.
        events = await repo.list_events("run-effects")
        assert any(event["event_type"] == "work_item.unknown" for event in events)
        goal = await repo.get_goal_run("run-effects")
        assert goal is not None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_fanin_snapshot_and_outbox_replay_are_durable(tmp_path):
    repo = await _repo(tmp_path / "outbox.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-outbox", idempotency_key="run-outbox")
        await repo.enqueue_work_item(WorkItemSpec(goal_run_id="run-outbox", logical_key="A", kind="agent_loop"))
        snapshot = await repo.create_fanin_snapshot("run-outbox")
        assert snapshot["manifest_hash"].startswith("sha256:")
        outbox = await repo.list_outbox()
        assert outbox
        delivered: list[dict] = []

        async def publish(event):
            delivered.append(event)

        result = await repo.publish_outbox(publish)
        assert result["published"] == len(outbox)
        assert delivered and all(event["event_version"] == 1 for event in delivered)
        assert await repo.list_outbox() == []
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_finalization_is_a_durable_idempotent_work_item(tmp_path):
    repo = await _repo(tmp_path / "finalization.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-final", idempotency_key="run-final")
        snapshot = await repo.create_fanin_snapshot("run-final")
        item = await repo.ensure_finalization_item("run-final", snapshot_hash=snapshot["manifest_hash"])
        assert item["kind"] == "finalize"
        claim = await repo.resume_finalization("run-final", worker_id="finalizer")
        assert claim is not None
        await repo.start(claim)
        await repo.checkpoint_finalization(
            claim,
            snapshot_hash=snapshot["manifest_hash"],
            stage="acceptance",
            included_child_sequence=snapshot["version"],
        )
        committed = await repo.complete_finalization(
            claim,
            {"answer": "durable answer", "incomplete_work": []},
            final_status="completed",
            snapshot_hash=snapshot["manifest_hash"],
        )
        assert committed["status"] == "committed"
        assert await repo.resume_finalization("run-final", worker_id="new-finalizer") is None
        goal = await repo.get_goal_run("run-final")
        assert goal is not None and goal["status"] == "completed"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_finalization_crash_resumes_from_checkpoint_without_child_rerun(tmp_path):
    repo = await _repo(tmp_path / "finalization-recovery.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-final-recovery", idempotency_key="run-final-recovery")
        snapshot = await repo.create_fanin_snapshot("run-final-recovery")
        await repo.ensure_finalization_item("run-final-recovery", snapshot_hash=snapshot["manifest_hash"])
        old = await repo.resume_finalization("run-final-recovery", worker_id="finalizer-old", lease_ttl_s=1)
        assert old is not None
        await repo.start(old)
        await repo.checkpoint_finalization(
            old,
            snapshot_hash=snapshot["manifest_hash"],
            stage="acceptance",
            included_child_sequence=snapshot["version"],
        )

        report = await repo.reconcile("run-final-recovery", now=old.lease_expires_at + 1)
        assert report.retry_safe == 1
        resumed = await repo.resume_finalization(
            "run-final-recovery",
            worker_id="finalizer-new",
            lease_ttl_s=30,
        )
        assert resumed is not None
        assert resumed.lease_token == old.lease_token + 1
        await repo.start(resumed)
        committed = await repo.complete_finalization(
            resumed,
            {"answer": "resumed answer", "incomplete_work": []},
            final_status="completed",
            snapshot_hash=snapshot["manifest_hash"],
            resumed=True,
        )
        assert committed["status"] == "committed"
        events = await repo.list_events("run-final-recovery")
        assert any(event["event_type"] == "finalization.completed" for event in events)
        goal = await repo.get_goal_run("run-final-recovery")
        assert goal is not None and goal["status"] == "completed"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_side_effect_ledger_probes_unknown_before_replay(tmp_path):
    repo = await _repo(tmp_path / "probe.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-probe", idempotency_key="run-probe")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-probe", logical_key="publish", kind="tool", side_effect_policy="probe_required"),
        )
        operation_key = build_operation_key("run-probe", item["id"], "publish")
        await repo.declare_side_effect(
            goal_run_id="run-probe", work_item_id=item["id"], operation_key=operation_key,
            operation_type="publish", target_ref="provider:item", request={"value": 1}, capability="status_probe",
        )
        await repo.update_side_effect(operation_key, state="unknown")
        calls = 0

        async def provider():
            nonlocal calls
            calls += 1
            return "published"

        async def probe():
            return {"status": "committed", "result": "already-published", "provider_request_id": "p-1"}

        result = await SideEffectLedger(repo).execute(
            goal_run_id="run-probe", work_item_id=item["id"], operation_key=operation_key,
            operation_type="publish", target_ref="provider:item", request={"value": 1},
            provider=provider, capability="status_probe", probe=probe,
        )
        assert result == "already-published"
        assert calls == 0
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_reconcile_committed_provider_evidence_does_not_replay_side_effect(tmp_path):
    repo = await _repo(tmp_path / "provider-recovery.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-provider-recovery", idempotency_key="run-provider-recovery")
        item = await repo.enqueue_work_item(
            WorkItemSpec(
                goal_run_id="run-provider-recovery",
                logical_key="publish",
                kind="tool",
                side_effect_policy="probe_required",
            )
        )
        operation_key = build_operation_key("run-provider-recovery", item["id"], "publish")
        await repo.declare_side_effect(
            goal_run_id="run-provider-recovery",
            work_item_id=item["id"],
            operation_key=operation_key,
            operation_type="publish",
            target_ref="provider:item",
            request={"value": 1},
            capability="status_probe",
        )
        claim = await repo.claim_next("provider-worker", lease_ttl_s=1)
        assert claim is not None
        await repo.start(claim)
        await repo.update_side_effect(
            operation_key,
            state="unknown",
            provider_request_id="provider-1",
            probe_result={"status": "committed", "result": "published"},
        )

        report = await repo.reconcile("run-provider-recovery", now=claim.lease_expires_at + 1)
        assert report.completed_from_evidence == 1
        assert report.decisions[0]["decision"] == "COMPLETED_FROM_EVIDENCE"
        recovered = await repo.get_goal_run("run-provider-recovery")
        assert recovered is not None
        attempts = await repo.list_attempts(item["id"])
        assert len(attempts) == 1
        assert attempts[0]["state"] == "succeeded"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_worker_host_owns_claim_start_heartbeat_and_complete(tmp_path):
    repo = await _repo(tmp_path / "worker.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-worker", idempotency_key="run-worker")
        await repo.enqueue_work_item(WorkItemSpec(goal_run_id="run-worker", logical_key="A", kind="agent_loop"))
        seen: list[str] = []

        async def callback(claim, _cancel):
            seen.append(claim.logical_key)
            return {"summary": "worker completed", "prompt_tokens": 2, "completion_tokens": 3}

        host = WorkerHost(
            repo,
            worker_id="worker-host",
            callbacks={"agent_loop": callback},
            lease_ttl_s=2,
            heartbeat_interval_s=0.25,
        )
        await host.start()
        assert await host.run_once()
        assert seen == ["A"]
        events = await repo.list_events("run-worker")
        assert any(event["event_type"] == "work_item.started" for event in events)
        assert any(event["event_type"] == "work_item.succeeded" for event in events)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_artifact_registration_is_content_immutable(tmp_path):
    repo = await _repo(tmp_path / "artifacts.sqlite3")
    try:
        await repo.create_goal_run(goal_run_id="run-artifacts", idempotency_key="run-artifacts")
        first = await repo.register_artifact(
            goal_run_id="run-artifacts", content_uri="outputs/report.md", content_hash_value="sha256:one",
            size_bytes=3, mime_type="text/markdown", kind="output", visibility="user_visible",
        )
        same = await repo.register_artifact(
            goal_run_id="run-artifacts", content_uri="outputs/report.md", content_hash_value="sha256:one",
            size_bytes=3, mime_type="text/markdown", kind="output", visibility="user_visible",
        )
        assert same["id"] == first["id"]
        with pytest.raises(DurableExecutionError, match="immutable URI"):
            await repo.register_artifact(
                goal_run_id="run-artifacts", content_uri="outputs/report.md", content_hash_value="sha256:two",
                size_bytes=3, mime_type="text/markdown", kind="output",
            )
    finally:
        await repo.close()
