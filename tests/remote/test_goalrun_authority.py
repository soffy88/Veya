"""RemoteExecution authority-closure tests.

These tests assert identity and projection semantics, not a second remote
execution state machine.  The provider runner is intentionally tiny so the
tests can exercise GoalRun admission and restart recovery deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType


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
    return SimpleNamespace(session_id="session-1", token_id="token-1", principal="test")


async def _wait_terminal(manager: DurableJobManager, execution_id: str) -> None:
    await manager.wait(execution_id, timeout_s=10)


async def test_remote_execution_projects_goalrun_and_rejects_nonzero_exit(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "remote"))

    async def runner(reporter):
        reporter.finish_command(exit_code=2, status="failed")
        return "provider failed"

    record = manager.submit(
        session=_session(),
        tool="shell.exec",
        veya_tool="shell.exec",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
    )
    await _wait_terminal(manager, record.execution_id)

    assert record.goal_run_id
    assert record.goal_task_id == f"remote:{record.execution_id}"
    assert record.status == "FAILED"
    assert record.exit_code == 2
    assert record.status != "COMPLETED"


async def test_remote_double_submit_is_idempotent(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "remote"))
    calls = 0

    async def runner(reporter):
        nonlocal calls
        calls += 1
        return "ok"

    kwargs = dict(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=runner,
        execution_type=str(ExecutionType.HICODE),
        idempotency_key="remote-submit-once",
    )
    first = manager.submit(**kwargs)
    second = manager.submit(**kwargs)
    assert second.execution_id == first.execution_id
    await _wait_terminal(manager, first.execution_id)
    assert calls == 1


async def test_remote_restart_reuses_goalrun_and_task_identity(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "remote")
    manager_a = DurableJobManager(store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def interrupted_runner(reporter):
        entered.set()
        await release.wait()
        return "ok"

    first = manager_a.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=interrupted_runner,
        execution_type=str(ExecutionType.HICODE),
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    goal_id = first.goal_run_id
    task_id = first.goal_task_id
    assert goal_id and task_id

    # Simulate a process loss: drop only the local carrier.  The persisted
    # mapping and GoalRun taskgraph remain intact.
    carrier = manager_a._tasks[first.execution_id]
    carrier.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await carrier

    manager_b = DurableJobManager(store)

    async def recovered_runner(reporter):
        return "recovered"

    recovered = await manager_b.recover_unfinished(lambda _record: recovered_runner)
    assert recovered == 1
    await _wait_terminal(manager_b, first.execution_id)
    projected = manager_b.status(first.execution_id, token_id="token-1")
    assert projected.goal_run_id == goal_id
    assert projected.goal_task_id == task_id
    assert projected.status == "COMPLETED"


async def test_remote_parallel_admission_allocates_distinct_goalrun_ids(tmp_path: Path) -> None:
    """Fast isolated providers must not collide on second-resolution IDs."""
    manager = DurableJobManager(ExecutionStore(None))

    async def runner(_reporter):
        return "ok"

    records = [
        manager.submit(
            session=_session(),
            tool="hicode.execute",
            veya_tool="hicode.execute",
            binding=_binding(tmp_path),
            runner=runner,
            execution_type=str(ExecutionType.HICODE),
        )
        for _ in range(2)
    ]
    await asyncio.gather(*(manager.wait(record.execution_id, timeout_s=10) for record in records))

    assert records[0].goal_run_id
    assert records[1].goal_run_id
    assert records[0].goal_run_id != records[1].goal_run_id
    assert {record.status for record in records} == {"COMPLETED"}


async def test_goalrun_completes_under_llm_isolation_fixture(tmp_path: Path) -> None:
    """Remote completion must not require the developer's global LLM config."""
    manager = DurableJobManager(ExecutionStore(None))

    async def provider(_reporter):
        return "deterministic provider result"

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=provider,
        execution_type=str(ExecutionType.HICODE),
    )
    await manager.wait(record.execution_id, timeout_s=10)

    assert record.goal_run_id
    assert record.status == "COMPLETED"
    assert record.result_summary == "deterministic provider result"


async def test_hicode_recovery_paused_is_blocked_not_completed(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "remote"))

    async def provider(_reporter):
        return "✅ hicode 执行完成 @ /tmp/worktree\nrecovery_paused: no deliverable produced yet"

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=provider,
        execution_type=str(ExecutionType.HICODE),
    )
    await _wait_terminal(manager, record.execution_id)

    assert record.status == "BLOCKED"
    assert record.failure_class == "execution_blocked"
    assert record.failure_source == "hicode_runtime"
    assert record.provider_error_code == "HICODE_RECOVERY_PAUSED"
    assert record.status != "COMPLETED"


async def test_hicode_success_body_can_mention_recovery_paused(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "remote-success"))

    async def provider(_reporter):
        return "✅ hicode 执行完成 @ /tmp/worktree\nSuccess: sentinel\nrecovery_paused: quoted text only"

    record = manager.submit(
        session=_session(),
        tool="hicode.execute",
        veya_tool="hicode.execute",
        binding=_binding(tmp_path),
        runner=provider,
        execution_type=str(ExecutionType.HICODE),
    )
    await _wait_terminal(manager, record.execution_id)

    assert record.status == "COMPLETED"
    assert record.failure_class is None
    assert record.provider_error_code is None
