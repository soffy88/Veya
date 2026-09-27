from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from veya.remote.execution import (
    DurableJobManager,
    ExecutionError,
    ExecutionStatus,
    ExecutionStore,
)
from veya.remote.execution_contract import probe_runtime_capability_manifest
from veya.remote.executor_health import resolve_executor
from veya.remote.models import RemotePermissions, RemoteSession
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


def _session(session_id: str, principal: str, token: str, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        principal=principal,
        token_id=token,
        active_workspace=str(root),
        explicit_workspace=str(root),
    )


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


async def _sleep_runner(reporter: object) -> str:
    await asyncio.sleep(30)
    return "done"


def test_worker_declared_shell_matches_injected_tools() -> None:
    assert probe_runtime_capability_manifest("pi").supports_shell is False
    assert probe_runtime_capability_manifest("hicode").supports_shell is True


def test_dispatch_rejects_missing_required_shell() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        resolve_executor(
            requested="pi",
            explicit_pin=True,
            required_capabilities={"supports_shell_effect": True},
        )


async def test_l0_shell_failure_is_structured(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    now = time.time()
    session = RemoteSession(
        session_id="s-shell",
        principal="owner",
        token_id="t-shell",
        workspaces=(str(tmp_path),),
        active_workspace=str(tmp_path),
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        created_at=now,
        expires_at=now + 3600,
    )
    result = await RemoteToolAdapter(None)._call_impl(
        session,
        "shell.exec",
        {
            "workspace": str(tmp_path),
            "command": "/definitely/missing/veya-command",
            "wait": True,
        },
    )
    assert result.ok is False
    assert result.error_code.value == "EXECUTION_FAILED"
    assert isinstance(result.result, dict)
    assert result.result["failure_class"] == "COMMAND_SPAWN_FAILED"


async def test_l0_shell_exec_smoke_uses_isolated_cwd(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    now = time.time()
    session = RemoteSession(
        session_id="s-shell",
        principal="owner",
        token_id="t-shell",
        workspaces=(str(tmp_path),),
        active_workspace=str(tmp_path),
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        created_at=now,
        expires_at=now + 3600,
    )
    result = await RemoteToolAdapter(None)._call_impl(
        session,
        "shell.exec",
        {"workspace": str(tmp_path), "command": "git rev-parse HEAD", "wait": True},
    )
    assert result.ok is True
    assert result.result["exit_code"] == 0
    assert (
        result.result["stdout_tail"].strip()
        == subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
        ).strip()
    )
    assert ".veya/worktrees/" in result.result["cwd"]


async def test_cross_session_authorized_cancel_and_denials(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    owner = _session("owner-session", "owner", "owner-token", tmp_path)
    other_session = _session("other-session", "owner", "other-token", tmp_path)
    intruder = _session("intruder-session", "intruder", "intruder-token", tmp_path)
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    record = manager.submit(
        session=owner,
        tool="shell.exec",
        veya_tool="shell.exec",
        binding=_binding(tmp_path),
        runner=_sleep_runner,
    )
    cancelled = await manager.cancel(
        record.execution_id,
        token_id=other_session.token_id,
        session_id=other_session.session_id,
        principal=other_session.principal,
        workspace_realpath=str(tmp_path),
    )
    assert cancelled.status == ExecutionStatus.CANCELLED
    repeated = await manager.cancel(
        record.execution_id,
        token_id=other_session.token_id,
        session_id=other_session.session_id,
        principal=other_session.principal,
        workspace_realpath=str(tmp_path),
    )
    assert repeated.status == ExecutionStatus.CANCELLED

    record2 = manager.submit(
        session=owner,
        tool="shell.exec",
        veya_tool="shell.exec",
        binding=_binding(tmp_path),
        runner=_sleep_runner,
    )
    with pytest.raises(ExecutionError, match="authorized"):
        await manager.cancel(
            record2.execution_id,
            token_id=intruder.token_id,
            session_id=intruder.session_id,
            principal=intruder.principal,
            workspace_realpath=str(tmp_path),
        )
    with pytest.raises(ExecutionError, match="workspace"):
        await manager.cancel(
            record2.execution_id,
            token_id=other_session.token_id,
            session_id=other_session.session_id,
            principal=other_session.principal,
            workspace_realpath=str(other_workspace),
        )
    await manager.cancel(
        record2.execution_id,
        token_id=owner.token_id,
        session_id=owner.session_id,
        principal=owner.principal,
        workspace_realpath=str(tmp_path),
    )


async def test_terminal_child_reconciles_parent_after_restart(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    owner = _session("owner-session", "owner", "owner-token", tmp_path)
    parent = manager.create_parent(
        session=owner,
        tool="worker.dispatch",
        binding=_binding(tmp_path),
        failure_mode="collect_all",
    )
    child = manager.submit(
        session=owner,
        tool="worker.dispatch",
        veya_tool="direct_pi",
        binding=_binding(tmp_path),
        runner=_sleep_runner,
        parent_execution_id=parent.execution_id,
        worker_type="PI",
    )
    manager.attach_child(parent.execution_id, child.execution_id)
    child.failure_class = "HICODE_PROVIDER_ERROR"
    child.failure_detail = "provider unavailable"
    child.worker_alive = False
    manager._tasks.pop(child.execution_id, None)
    manager._persist(child)

    restarted = DurableJobManager(store)
    report = restarted.reconcile_unfinished()
    aggregate = restarted.aggregate(restarted.lookup(parent.execution_id))
    assert report["children"] == 1
    assert aggregate["status"] == "FAILED"
    assert aggregate["phase"] == "FAILED"
    assert restarted.lookup(child.execution_id).status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_orphaned_worker_pid_reconciles_without_stale_heartbeat_false_failure(
    tmp_path: Path,
) -> None:
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store, heartbeat_timeout_s=1.0)
    owner = _session("owner-session", "owner", "owner-token", tmp_path)
    parent = manager.create_parent(
        session=owner,
        tool="worker.dispatch",
        binding=_binding(tmp_path),
        failure_mode="collect_all",
    )
    child = manager.submit(
        session=owner,
        tool="worker.dispatch",
        veya_tool="hicode_run",
        binding=_binding(tmp_path),
        runner=_sleep_runner,
        parent_execution_id=parent.execution_id,
        worker_type="HICODE",
    )
    manager.attach_child(parent.execution_id, child.execution_id)
    child.phase = "EXECUTING"
    child.status = "RUNNING"
    child.worker_alive = True
    child.worker_pid = 2**31 - 1
    child.heartbeat_at = time.time() - 10
    task = manager._tasks.pop(child.execution_id, None)
    manager._persist(child)

    restarted = DurableJobManager(store, heartbeat_timeout_s=1.0)
    report = restarted.reconcile_unfinished()
    aggregate = restarted.aggregate(restarted.lookup(parent.execution_id))
    assert report["children"] == 1
    assert aggregate["status"] == "FAILED"
    assert restarted.lookup(child.execution_id).failure_class == "STALE_WORKER_FAILURE"
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
