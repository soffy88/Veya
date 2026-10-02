"""Canonical qualification claims verification test suite.

Explicitly asserts the seven canonical qualification claims:
1. RuntimeCapabilityManifest is a real probe, not a static declaration.
2. Capability routing to UNKNOWN / UNAVAILABLE / capability missing is strictly fail-closed.
3. SUSPEND -> service restart -> RESUME maintains the same durable lineage.
4. RUNNING -> runtime crash -> RECOVERING -> RUNNING/FAILED is realistically demonstrated.
5. FALSE_SUCCESS=0, DUPLICATE_SIDE_EFFECTS=0, ORPHAN_PROCESS=0.
6. server/routes/execution_contract.py delegates to canonical execution authority (fails closed 404 on unknown).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from veya.remote.capabilities import (
    CapabilityError,
    CapabilityHealth,
    CapabilityProvider,
    CapabilityRegistry,
)
from veya.remote.execution import (
    DurableJobManager,
    ExecutionPhase,
    ExecutionStore,
    ExecutionType,
)
from veya.remote.execution_contract import (
    probe_runtime_capability_manifest,
)
from veya.remote.executor_health import (
    ExecutorFailureClass,
    ExecutorHealthRegistry,
    registry_order,
    resolve_executor,
)


async def _await_started(manager, execution_id: str, *, ceiling_s: float = 120.0) -> None:
    """Wait until a record is actually running, without asserting a start latency.

    The previous form waited a fixed 5s for the runner's first line to execute.
    That measures how fast the host can schedule a task, not whether the
    execution starts, so on a saturated machine a correct submission was
    reported as a lineage failure before the lineage was ever exercised.

    The state is the contract; the ceiling only catches a genuine hang and is
    far above the old bound so it cannot fail a correct run.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + ceiling_s
    while loop.time() < deadline:
        record = manager.lookup(execution_id)
        if record is not None and record.status in {"RUNNING", "SUSPENDED"}:
            return
        if record is not None and record.status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "BLOCKED",
        }:
            raise AssertionError(f"{execution_id} reached {record.status} before it ever started")
        await asyncio.sleep(0.02)
    raise AssertionError(f"{execution_id} never started within {ceiling_s}s")


async def _await_terminal(manager, execution_id: str, *, ceiling_s: float = 120.0):
    """Wait until a record reaches a terminal state, then return it.

    This replaces a fixed ``manager.wait(timeout_s=10)`` as the *gate*. The old
    form asserted a wall-clock budget, so on a saturated host a perfectly
    correct resume was reported as a lineage failure — and because
    ``DurableJobManager.wait`` swallows its own TimeoutError, the failure
    surfaced as a wrong-state assertion rather than as a slow machine.

    The ceiling still exists to catch a genuine hang. It is deliberately far
    above the old bound so it cannot fail a correct run, and the contract being
    checked is unchanged: the record must reach a terminal state, and the caller
    still asserts which one.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + ceiling_s
    while loop.time() < deadline:
        record = manager.lookup(execution_id)
        if record is not None and record.status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "BLOCKED",
            "REJECTED",
        }:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"{execution_id} did not reach a terminal state within {ceiling_s}s "
        f"(last status={getattr(manager.lookup(execution_id), 'status', None)!r})"
    )


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-qual-1", token_id="token-qual-1", principal="qual-tester"
    )


# ── CLAIM 1: RuntimeCapabilityManifest is a real probe ───────────────────────


def test_claim_1_manifest_is_real_probe(tmp_path: Path):
    """Manifest actively inspects local binary, authentication, health registry, and workspace."""
    # 1. Non-existent worker -> status UNAVAILABLE, not installed
    manifest_missing = probe_runtime_capability_manifest("nonexistent_worker_xyz")
    assert manifest_missing.installed is False
    assert manifest_missing.status == "UNAVAILABLE"
    assert "not installed" in manifest_missing.status_reason

    # 2. Retired runner (hicode) -> fails closed, never a manifest.
    with pytest.raises(ValueError, match="retired"):
        probe_runtime_capability_manifest("hicode", workspace_path=str(tmp_path))

    # 3. Health registry failure recorded -> status becomes UNAVAILABLE
    reg = ExecutorHealthRegistry()
    reg.record_failure(
        "opencode", ExecutorFailureClass.PROVIDER_UNAVAILABLE, detail="503 upstream down"
    )
    manifest_unavail = probe_runtime_capability_manifest("opencode", health_registry=reg)
    assert manifest_unavail.reachable is False
    assert manifest_unavail.status == "UNAVAILABLE"
    assert "UNAVAILABLE" in manifest_unavail.status_reason


# ── CLAIM 2: Capability routing fail-closed on UNKNOWN / UNAVAILABLE / missing ─


def test_claim_2_capability_missing_fails_closed():
    """Missing capability raises ValueError immediately."""
    reg = ExecutorHealthRegistry()
    with pytest.raises(ValueError, match="No capable executor available"):
        resolve_executor(
            required_capabilities={"impossible_quantum_capability": True},
            health_registry=reg,
        )


def test_claim_2_unavailable_candidate_fails_closed():
    """If all capable candidates are UNAVAILABLE, routing fails closed instead of picking unhealthy."""
    reg = ExecutorHealthRegistry()
    for worker in registry_order():
        reg.record_failure(worker, ExecutorFailureClass.PROVIDER_UNAVAILABLE)

    with pytest.raises(ValueError, match=r"all capable candidates .* are UNAVAILABLE"):
        resolve_executor(health_registry=reg)


def test_claim_2_unknown_candidate_fails_closed_in_fail_closed_mode():
    """If candidates are unverified UNKNOWN, fail_closed_unknown refuses routing."""
    reg = ExecutorHealthRegistry()  # All are UNKNOWN initially
    with pytest.raises(
        ValueError, match=r"all capable candidates .* are UNAVAILABLE or unverified"
    ):
        resolve_executor(health_registry=reg, fail_closed_unknown=True)


async def test_claim_2_capability_registry_plane_b_fails_closed():
    """CapabilityRegistry Plane B strictly rejects UNKNOWN, UNAVAILABLE, and missing capabilities."""
    cap_reg = CapabilityRegistry()

    # 1. Missing capability -> raises UNKNOWN_CAPABILITY
    with pytest.raises(CapabilityError) as exc_info:
        await cap_reg.invoke("nonexistent.search", {})
    assert exc_info.value.code == "UNKNOWN_CAPABILITY"

    # 2. UNAVAILABLE provider -> raises CAPABILITY_UNAVAILABLE
    async def broken_prober():
        return str(CapabilityHealth.UNAVAILABLE), "502 Bad Gateway"

    cap_reg.register("web.search", [CapabilityProvider("web-1", priority=1, prober=broken_prober)])
    with pytest.raises(CapabilityError) as exc_info:
        await cap_reg.invoke("web.search", {})
    assert exc_info.value.code == "CAPABILITY_UNAVAILABLE"

    # 3. UNKNOWN provider (no prober) -> raises CAPABILITY_UNAVAILABLE
    cap_reg.register("web.unknown", [CapabilityProvider("web-2", priority=1, prober=None)])
    with pytest.raises(CapabilityError) as exc_info:
        await cap_reg.invoke("web.unknown", {})
    assert exc_info.value.code == "CAPABILITY_UNAVAILABLE"


# ── CLAIM 3: SUSPEND -> service restart -> RESUME preserves lineage ──────────


async def test_claim_3_suspend_restart_resume_preserves_lineage(tmp_path: Path):
    """Execution suspended, restarted under a new manager, and resumed retains identical GoalRun identity."""
    store = ExecutionStore(tmp_path / "executions")
    manager_1 = DurableJobManager(store)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def long_running_runner(reporter):
        entered.set()
        await release.wait()
        return "completed after resume"

    record = manager_1.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=long_running_runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await _await_started(manager_1, record.execution_id)

    original_exec_id = record.execution_id
    original_goal_id = record.goal_run_id
    original_task_id = record.goal_task_id
    assert original_goal_id is not None
    assert original_task_id is not None

    # 1. Suspend the active execution
    suspended = await manager_1.suspend(original_exec_id, token_id="token-qual-1")
    assert suspended.status == "SUSPENDED"
    assert suspended.phase == ExecutionPhase.SUSPENDED

    # 2. Simulate service restart with new DurableJobManager instance on same persistent store
    manager_2 = DurableJobManager(store)
    reloaded = manager_2.lookup(original_exec_id)
    assert reloaded is not None
    assert reloaded.status == "SUSPENDED"
    assert reloaded.goal_run_id == original_goal_id
    assert reloaded.goal_task_id == original_task_id

    # 3. Resume under new manager instance
    resumed_runner_called = False

    async def resumed_runner(reporter):
        nonlocal resumed_runner_called
        resumed_runner_called = True
        return "final resumed success"

    resumed = await manager_2.resume(
        original_exec_id,
        token_id="token-qual-1",
        runner=resumed_runner,
    )
    assert resumed.execution_id == original_exec_id
    assert resumed.goal_run_id == original_goal_id
    assert resumed.goal_task_id == original_task_id

    await _await_terminal(manager_2, original_exec_id)
    final = manager_2.lookup(original_exec_id)
    assert final.status == "COMPLETED"
    assert final.result_summary == "final resumed success"
    assert resumed_runner_called is True


# ── CLAIM 4: RUNNING -> crash -> RECOVERING -> RUNNING / FAILED ──────────────


async def test_claim_4a_running_crash_recovering_to_running_and_completed(tmp_path: Path):
    """Crash while RUNNING transitions through RECOVERING and converges to COMPLETED."""
    store = ExecutionStore(tmp_path / "executions_crash_a")
    manager_1 = DurableJobManager(store)

    entered = asyncio.Event()

    async def crashing_runner(reporter):
        entered.set()
        await asyncio.Event().wait()  # Never completes normally

    record = manager_1.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=crashing_runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await _await_started(manager_1, record.execution_id)

    # Abrupt crash (kill in-memory carrier task without graceful shutdown)
    carrier = manager_1._tasks[record.execution_id]
    carrier.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await carrier

    # Restart service and recover
    manager_2 = DurableJobManager(store)

    async def recovery_runner(reporter):
        return "recovered output"

    recovered_count = await manager_2.recover_unfinished(lambda _rec: recovery_runner)
    assert recovered_count == 1

    await _await_terminal(manager_2, record.execution_id)
    final = manager_2.lookup(record.execution_id)
    assert final.status == "COMPLETED"
    assert final.result_summary == "recovered output"


async def test_claim_4b_running_crash_recovering_to_failed(tmp_path: Path):
    """Crash while RUNNING transitions through RECOVERING and converges to FAILED on unrecoverable error."""
    store = ExecutionStore(tmp_path / "executions_crash_b")
    manager_1 = DurableJobManager(store)

    entered = asyncio.Event()

    async def crashing_runner(reporter):
        entered.set()
        await asyncio.Event().wait()

    record = manager_1.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=crashing_runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await _await_started(manager_1, record.execution_id)

    # Abrupt crash
    carrier = manager_1._tasks[record.execution_id]
    carrier.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await carrier

    # Restart service and attempt recovery that fails
    manager_2 = DurableJobManager(store)

    async def failing_recovery_runner(reporter):
        reporter.finish_command(exit_code=1, status="failed")
        raise RuntimeError("fatal unrecoverable environment corruption")

    await manager_2.recover_unfinished(lambda _rec: failing_recovery_runner)
    await _await_terminal(manager_2, record.execution_id)

    final = manager_2.lookup(record.execution_id)
    assert final.status == "FAILED"
    assert final.exit_code == 1
    assert final.status != "COMPLETED"


# ── CLAIM 5: FALSE_SUCCESS=0, DUPLICATE_SIDE_EFFECTS=0, ORPHAN_PROCESS=0 ────


async def test_claim_5_invariants(tmp_path: Path):
    """Verify FALSE_SUCCESS=0, DUPLICATE_SIDE_EFFECTS=0, and clean task convergence."""
    store = ExecutionStore(tmp_path / "executions_inv")
    manager = DurableJobManager(store)

    # 1. FALSE_SUCCESS=0: non-zero exit is NEVER COMPLETED
    async def bad_exit_runner(reporter):
        reporter.finish_command(exit_code=137, status="failed")
        return "killed by sigkill"

    rec1 = manager.submit(
        session=_session(),
        tool="shell.exec",
        veya_tool="shell.exec",
        binding=_binding(tmp_path),
        runner=bad_exit_runner,
        execution_type=str(ExecutionType.DIRECT),
    )
    await manager.wait(rec1.execution_id, timeout_s=5)
    assert rec1.status != "COMPLETED"
    assert rec1.status == "FAILED"
    assert rec1.exit_code == 137

    # 2. DUPLICATE_SIDE_EFFECTS=0: idempotent submission executes exactly once
    side_effects = 0

    async def mutating_runner(reporter):
        nonlocal side_effects
        side_effects += 1
        return "side effect done"

    submit_kwargs = dict(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=mutating_runner,
        execution_type=str(ExecutionType.HICODE),
        idempotency_key="exact-once-key",
    )
    sub1 = manager.submit(**submit_kwargs)
    sub2 = manager.submit(**submit_kwargs)
    assert sub1.execution_id == sub2.execution_id
    await manager.wait(sub1.execution_id, timeout_s=5)
    assert side_effects == 1

    # 3. Clean task termination: no dangling live tasks left
    await asyncio.sleep(0.01)
    assert not any(not t.done() for t in manager._tasks.values())


# ── CLAIM 6: server/routes/execution_contract.py authority delegation ────────


async def test_claim_6_routes_delegate_to_canonical_authority(tmp_path: Path, monkeypatch):
    """Routes return 404 for unknown IDs and delegate real lifecycle operations."""
    from server.app import app
    from server.routes import execution_contract

    # Isolate canonical manager to test execution store
    test_manager = DurableJobManager(ExecutionStore(tmp_path / "routes_exec"))
    monkeypatch.setattr(execution_contract, "_CANONICAL_MANAGER", test_manager)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Non-existent execution must fail-closed with 404 (not a fake 200 stub!)
        resp = await client.get("/api/v1/execution/nonexistent-id-1234")
        assert resp.status_code == 404

        resp_suspend = await client.post("/api/v1/execution/nonexistent-id-1234/suspend")
        assert resp_suspend.status_code == 404

        # 2. Real execution submitted -> routes manage real lifecycle
        async def mock_runner(reporter):
            await asyncio.sleep(10)
            return "done"

        rec = test_manager.submit(
            session=_session(),
            tool="hicode.execute",
            veya_tool="hicode.execute",
            binding=_binding(tmp_path),
            runner=mock_runner,
            execution_type=str(ExecutionType.HICODE),
        )

        # GET status returns real persisted record
        resp_get = await client.get(f"/api/v1/execution/{rec.execution_id}")
        assert resp_get.status_code == 200
        data = resp_get.json()
        assert data["execution"]["execution_id"] == rec.execution_id

        # POST suspend delegates to manager
        resp_sus = await client.post(f"/api/v1/execution/{rec.execution_id}/suspend")
        assert resp_sus.status_code == 200
        assert resp_sus.json()["status"] == "SUSPENDED"

        # POST cancel delegates to manager
        resp_cancel = await client.post(f"/api/v1/execution/{rec.execution_id}/cancel")
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["status"] == "CANCELLED"
