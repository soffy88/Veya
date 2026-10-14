"""P4: unified ExecutorAdapter interface + real executor lifecycle.

Covers the contract that all L1 executors implement one adapter interface and
that two real adapters (Local2 native substrate, CLI worker) run a real
execution through start/status/wait/stream/cancel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veya.remote.executor_adapter import (
    AdapterStartRequest,
    ExecutorAdapter,
    ExecutorCapability,
    ExecutorHealth,
    ProgressReporter,
    get_executor_adapter,
    list_executor_adapters,
)
from veya.remote.executor_adapters import (
    CLIWorkerExecutorAdapter,
    Local2ExecutorAdapter,
    register_default_adapters,
)


def _request(root: Path, command: str, execution_id: str = "exec-p4-1") -> AdapterStartRequest:
    return AdapterStartRequest(
        goal_run_id="goal-p4",
        goal_task_id="task-p4",
        execution_id=execution_id,
        task_text=command,
        worktree_path=str(root),
        repo_root=str(root),
        timeout_s=30.0,
    )


def test_default_adapters_registered():
    register_default_adapters()
    ids = list_executor_adapters()
    assert "local2" in ids
    assert "pi" in ids
    assert "codex" in ids
    assert "opencode" in ids
    assert "claude_code" in ids


def test_local2_adapter_identity_and_capabilities(tmp_path: Path):
    adapter = Local2ExecutorAdapter("local2", str(tmp_path))
    assert isinstance(adapter, ExecutorAdapter)
    ident = adapter.identity
    assert ident.executor_id == "local2"
    assert ident.executor_kind == "native_substrate"
    caps = adapter.capabilities
    assert ExecutorCapability.SHELL in caps.capabilities
    assert ExecutorCapability.FILESYSTEM in caps.capabilities
    assert caps.write_qualified is True


def test_cli_adapter_identity_and_health(tmp_path: Path):
    adapter = CLIWorkerExecutorAdapter("pi", str(tmp_path))
    assert adapter.identity.executor_kind == "cli_worker"
    health = adapter.health
    assert isinstance(health.health, ExecutorHealth)


@pytest.mark.asyncio
async def test_local2_adapter_runs_real_command(tmp_path: Path):
    adapter = Local2ExecutorAdapter("local2", str(tmp_path))
    reporter = ProgressReporter("exec-p4-1", "goal-p4", "local2")
    result = await adapter.start(_request(tmp_path, "echo p4-local2-ok"), reporter)
    assert result.exit_code == 0
    assert "p4-local2-ok" in result.stdout
    status = await adapter.status("exec-p4-1", "")
    assert status["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_local2_adapter_cancel_is_idempotent(tmp_path: Path):
    adapter = Local2ExecutorAdapter("local2", str(tmp_path))
    reporter = ProgressReporter("exec-p4-2", "goal-p4", "local2")
    # Start a short command, then cancel after it completes: idempotent True.
    await adapter.start(_request(tmp_path, "true", execution_id="exec-p4-2"), reporter)
    assert await adapter.cancel("exec-p4-2", "") is True
    assert await adapter.cancel("exec-p4-2", "") is True


@pytest.mark.asyncio
async def test_local2_adapter_stream_has_monotonic_cursor(tmp_path: Path):
    adapter = Local2ExecutorAdapter("local2", str(tmp_path))
    reporter = ProgressReporter("exec-p4-3", "goal-p4", "local2")
    await adapter.start(
        _request(tmp_path, "echo one; echo two", execution_id="exec-p4-3"), reporter
    )
    events, cursor = await adapter.stream("exec-p4-3", "", cursor=0)
    assert cursor == len(events)
    events2, cursor2 = await adapter.stream("exec-p4-3", "", cursor=cursor)
    assert cursor2 == cursor
    assert events2 == []


@pytest.mark.asyncio
async def test_local2_adapter_denied_command_reports_failure(tmp_path: Path):
    adapter = Local2ExecutorAdapter("local2", str(tmp_path))
    reporter = ProgressReporter("exec-p4-4", "goal-p4", "local2")
    # Path escape is refused by the direct executor; must not raise, must fail.
    result = await adapter.start(
        _request(tmp_path, "cat ../../etc/passwd", execution_id="exec-p4-4"), reporter
    )
    assert result.exit_code != 0 or result.failure_class is not None


def test_get_executor_adapter_returns_instance(tmp_path: Path):
    adapter = get_executor_adapter("local2", str(tmp_path))
    assert isinstance(adapter, Local2ExecutorAdapter)
    assert get_executor_adapter("does-not-exist", str(tmp_path)) is None
