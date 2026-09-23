"""Production lifecycle tests for RemoteToolAdapter startup recovery."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType
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
