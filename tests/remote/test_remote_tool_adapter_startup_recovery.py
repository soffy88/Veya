"""Production lifecycle tests for RemoteToolAdapter startup recovery."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from veya.remote.auth import RemoteAuth
from veya.remote.execution import (
    DurableJobManager,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStore,
    ExecutionType,
)
from veya.remote.models import RemotePermissions
from veya.remote.session import RemoteSessionManager, canonical_workspace
from veya.remote.tool_adapter import RemoteToolAdapter


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
    return SimpleNamespace(session_id="session-stable", token_id="token-stable", principal="p")


async def _remote_session(tmp_path: Path) -> Any:
    """A real bound session so the control plane (status/cancel) is exercised."""
    auth = RemoteAuth()
    token, _secret = auth.issue(
        "recovery-tester",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(tmp_path)],
    )
    sessions = RemoteSessionManager(ttl_s=3600, max_sessions=4)
    session = sessions.create(token, workspace=str(tmp_path))
    session.explicit_workspace = canonical_workspace(tmp_path)
    return session


def _record_session(session: Any) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session.session_id,
        token_id=session.token_id,
        principal=session.principal,
    )


def _stale_goalrun_record(session: Any, tmp_path: Path) -> ExecutionRecord:
    """Non-terminal projection with a GoalRun identity but no provider closure."""
    now = time.time()
    return ExecutionRecord(
        execution_id="direct_stale_recovery",
        task_id="task_stale_recovery",
        session_id=session.session_id,
        token_id=session.token_id,
        principal=session.principal,
        tool="hicode.execute",
        veya_tool="hicode.execute",
        requested_workspace=str(tmp_path),
        requested_realpath=canonical_workspace(tmp_path),
        resolved_repo_root=str(tmp_path),
        repo_identity=f"path:{tmp_path}",
        status=str(ExecutionStatus.RUNNING),
        phase=str(ExecutionPhase.EDITING),
        created_at=now - 3600,
        heartbeat_at=now - 3600,
        goal_run_id="goal_stale_recovery",
        goal_task_id="remote:direct_stale_recovery",
    )


async def test_remote_tool_adapter_startup_recovers_existing_identity(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "remote")
    manager_a = DurableJobManager(store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def interrupted(reporter):
        entered.set()
        await release.wait()
        return "ok"

    old = manager_a.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=interrupted,
        execution_type=str(ExecutionType.HICODE),
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    goal_id, task_id, session_id = old.goal_run_id, old.goal_task_id, old.session_id
    assert goal_id and task_id

    carrier = manager_a._tasks[old.execution_id]
    carrier.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await carrier

    calls = 0

    async def recovered(reporter):
        nonlocal calls
        calls += 1
        return "recovered"

    adapter = RemoteToolAdapter(
        execution_store=store,
        recovery_runner_factory=lambda _record: recovered,
    )
    first = await adapter.initialize()
    second = await adapter.initialize()
    assert first == second
    assert first["started"] is True
    assert first["recovered"] == 1
    await adapter.jobs.wait(old.execution_id, timeout_s=10)

    projected = adapter.jobs.status(old.execution_id, token_id="token-stable")
    assert projected.goal_run_id == goal_id
    assert projected.goal_task_id == task_id
    assert projected.session_id == session_id
    assert projected.status == "COMPLETED"
    assert calls == 1


async def test_startup_does_not_resume_terminal_or_cancelled_jobs(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "remote")
    manager = DurableJobManager(store)
    calls = 0

    async def runner(reporter):
        nonlocal calls
        calls += 1
        return "ok"

    complete = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(complete.execution_id, timeout_s=10)

    cancelled = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.cancel(cancelled.execution_id, token_id="token-stable")
    before = calls

    adapter = RemoteToolAdapter(
        execution_store=store,
        recovery_runner_factory=lambda _record: runner,
    )
    report = await adapter.initialize()
    assert report["recovered"] == 0
    assert calls == before


# ── degraded startup + operator control plane ──────────────────────────
async def test_startup_degrades_and_control_plane_converges_stale_projections(
    tmp_path: Path,
) -> None:
    """No factory + stale projections must degrade, not take the data plane down.

    The two real-world shapes are covered: an orphan L1 parent (no canonical
    GoalRun) and a GoalRun-bearing projection whose provider closure cannot be
    replayed.  Both stay visible with their original identity, and both must
    be convergable through the explicit control plane.
    """
    session = await _remote_session(tmp_path)
    store = ExecutionStore(tmp_path / "remote")

    manager = DurableJobManager(store, heartbeat_timeout_s=60.0)
    parent = manager.create_parent(
        session=_record_session(session),
        tool="worker_dispatch",
        binding=_binding(tmp_path),
    )
    parent.heartbeat_at = time.time() - 3600
    manager._persist(parent)

    stale = _stale_goalrun_record(session, tmp_path)
    store.save(stale)

    adapter = RemoteToolAdapter(execution_store=store)
    report = await adapter.initialize()
    assert report["started"] is True
    assert report["recovery_degraded"] is True
    assert report["pending_recovery"] == 2
    assert report["recovered"] == 0
    assert adapter.jobs.unfinished_count() == 2

    # Restart/second initialize is idempotent and keeps the degraded report.
    assert await adapter.initialize() == report

    status = await adapter.call(session, "process.status", {"execution_id": parent.execution_id})
    assert status.ok is True, status.result
    assert status.result["execution_id"] == parent.execution_id

    cancel_parent = await adapter.call(
        session, "process.cancel", {"execution_id": parent.execution_id}
    )
    assert cancel_parent.ok is True, cancel_parent.result
    assert cancel_parent.result["status"] == "CANCELLED"

    cancel_stale = await adapter.call(
        session, "process.cancel", {"execution_id": stale.execution_id}
    )
    assert cancel_stale.ok is True, cancel_stale.result
    assert cancel_stale.result["status"] == "CANCELLED"

    assert adapter.jobs.unfinished_count() == 0


def test_persist_failure_is_recorded_not_swallowed(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "remote")
    manager = DurableJobManager(store)
    record = manager.create_parent(
        session=SimpleNamespace(session_id="s", token_id="t", principal="p"),
        tool="worker_dispatch",
        binding=_binding(tmp_path),
    )

    def boom(_record: ExecutionRecord) -> None:
        raise OSError("disk full")

    store.save = boom  # type: ignore[method-assign]
    manager._persist(record)

    assert manager.persistence_failures
    failure = manager.persistence_failures[-1]
    assert failure["execution_id"] == record.execution_id
    assert "disk full" in failure["error"]
