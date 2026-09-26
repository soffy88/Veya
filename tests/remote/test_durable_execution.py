"""P0-O/P/Q/R: durable submit + poll lifecycle, heartbeat, cancel, reconnect."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionPhase, ExecutionStore, report_progress
from veya.remote.mcp_server import create_gateway

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


@pytest.fixture(autouse=True)
def _hicode_sandbox(tmp_path, monkeypatch):
    """Let the canonical Hicode resolver accept the test workspace."""

    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))


class BlockingExecutor:
    """Blocks the canonical hicode call until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append((name, kwargs))
        if name == "hicode_run":
            self.started.set()
            await self.release.wait()
            return "summary: harmless task done"
        return json.dumps({"status": "ok", "tool": name})


class ProgressExecutor:
    """Publishes a real monotonic lifecycle and pauses mid-flight."""

    def __init__(self, *, final_phase: ExecutionPhase = ExecutionPhase.FINALIZING) -> None:
        self.release = asyncio.Event()
        self.at_testing = asyncio.Event()
        self.final_phase = final_phase

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "hicode_run":
            report_progress(ExecutionPhase.PLANNING, message="planning")
            report_progress(ExecutionPhase.EDITING, message="editing")
            report_progress(ExecutionPhase.TESTING, message="running targeted pytest")
            self.at_testing.set()
            await self.release.wait()
            report_progress(self.final_phase, message="finalizing")
            return "done"
        return json.dumps({"status": "ok"})


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return tmp_path


def issue(auth: RemoteAuth, workspace: Path, principal: str = "tester") -> str:
    _record, secret = auth.issue(principal, permissions=PERMS, workspaces=[str(workspace)])
    return secret


def make_gateway(
    workspace: Path,
    executor: Any,
    store: ExecutionStore,
    *,
    auth: RemoteAuth | None = None,
    secret: str | None = None,
    heartbeat_interval_s: float = 5.0,
):
    auth = auth or RemoteAuth()
    if secret is None:
        secret = issue(auth, workspace)
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        executor,
        redact=audit.redact,
        execution_store=store,
        heartbeat_interval_s=heartbeat_interval_s,
        heartbeat_timeout_s=5.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, adapter


async def rpc(gateway, method, params, *, secret=None, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}" if secret else None,
        session_header=session,
    )


async def initialize(gateway, secret) -> str:
    response = await rpc(gateway, "initialize", {}, secret=secret)
    return response["result"]["sessionId"]


async def submit_hicode(gateway, secret, session, task: str, **extra) -> dict[str, Any]:
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "hicode.execute", "arguments": {"task": task, **extra}},
        secret=secret,
        session=session,
    )
    return response["result"]["structuredContent"]


async def status_of(gateway, secret, session, execution_id: str) -> dict[str, Any]:
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "process.status", "arguments": {"execution_id": execution_id}},
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def wait_terminal(gateway, secret, session, execution_id: str, timeout: float = 3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await status_of(gateway, secret, session, execution_id)
        if result["phase"] in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"execution did not reach a terminal phase: {execution_id}")


# ── P0-C: immediate submit ─────────────────────────────────────────────
async def test_submit_returns_execution_id_immediately(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = BlockingExecutor()
    gateway, secret, _adapter = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    started = time.monotonic()
    envelope = await submit_hicode(gateway, secret, session, "harmless no-op")
    elapsed = time.monotonic() - started
    assert envelope["ok"] is True, envelope
    assert envelope["result"]["accepted"] is True
    assert envelope["execution_id"]
    assert envelope["result"]["status"] in {"QUEUED", "RUNNING"}
    assert elapsed < 2.0, f"submit took {elapsed:.2f}s"
    executor.release.set()
    await wait_terminal(gateway, secret, session, envelope["execution_id"])


async def test_client_wait_timeout_does_not_cancel(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = BlockingExecutor()
    gateway, secret, _adapter = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "slow task", wait_timeout_s=0.05)
    execution_id = envelope["execution_id"]
    mid = await status_of(gateway, secret, session, execution_id)
    assert mid["status"] in {"RUNNING", "QUEUED", "WORKSPACE_VALIDATION"}
    assert mid["phase"] not in {"CANCELLED", "FAILED"}
    executor.release.set()
    final = await wait_terminal(gateway, secret, session, execution_id)
    assert final["phase"] == "COMPLETED"


# ── P0-H: reconnect / persistence across gateway instances ─────────────
async def test_execution_survives_disconnect_and_reconnect(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    store = ExecutionStore(tmp_path / "executions")
    auth = RemoteAuth()
    secret = issue(auth, workspace)
    executor = BlockingExecutor()
    gateway_a, _, _ = make_gateway(
        workspace, executor, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_a = await initialize(gateway_a, secret)
    envelope = await submit_hicode(gateway_a, secret, session_a, "long task")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.started.wait(), timeout=2.0)

    gateway_b, _, _ = make_gateway(
        workspace, executor, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_b = await initialize(gateway_b, secret)
    observed = await status_of(gateway_b, secret, session_b, execution_id)
    assert observed["execution_id"] == execution_id
    assert observed["phase"] not in {"CANCELLED", "FAILED", "COMPLETED"}
    assert observed["worker_alive"] is True

    executor.release.set()
    final = await wait_terminal(gateway_b, secret, session_b, execution_id)
    assert final["phase"] == "COMPLETED"
    assert "harmless task done" in (final["result_summary"] or "")


async def test_final_result_persisted(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    store = ExecutionStore(tmp_path / "executions")
    executor = BlockingExecutor()
    executor.release.set()
    gateway, secret, _ = make_gateway(workspace, executor, store)
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "persisted task", wait=True)
    execution_id = envelope["execution_id"]
    final = await status_of(gateway, secret, session, execution_id)
    assert final["phase"] == "COMPLETED"
    reloaded = ExecutionStore(tmp_path / "executions")
    record = reloaded.get(execution_id)
    assert record is not None
    assert record.result_summary and "harmless task done" in record.result_summary


# ── P0-E/F: progress + heartbeat ───────────────────────────────────────
async def test_incremental_progress_and_monotonic_lifecycle(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = ProgressExecutor()
    gateway, secret, _ = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "progress task")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.at_testing.wait(), timeout=2.0)
    mid = await status_of(gateway, secret, session, execution_id)
    assert mid["status"] == "RUNNING"
    assert mid["phase"] == "TESTING"
    assert mid["message"] == "running targeted pytest"
    executor.release.set()
    final = await wait_terminal(gateway, secret, session, execution_id)
    phases = [event["phase"] for event in final["recent_events"]]
    order = ["PLANNING", "EDITING", "TESTING", "FINALIZING", "COMPLETED"]
    seen = [phase for phase in phases if phase in order]
    assert seen == sorted(seen, key=order.index), seen


async def test_backward_phase_is_not_faked(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = ProgressExecutor(final_phase=ExecutionPhase.PLANNING)
    gateway, secret, _ = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "replan task")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.at_testing.wait(), timeout=2.0)
    executor.release.set()
    final = await wait_terminal(gateway, secret, session, execution_id)
    assert final["phase"] == "COMPLETED"
    kinds = [event["kind"] for event in final["recent_events"]]
    assert "replan" in kinds
    phases = [event["phase"] for event in final["recent_events"]]
    assert phases.index("TESTING") < len(phases) - 1
    # No phase after TESTING regresses to an earlier lifecycle step.
    order = ["PLANNING", "EDITING", "TESTING", "FINALIZING", "COMPLETED"]
    tail = [phase for phase in phases[phases.index("TESTING") :] if phase in order]
    assert tail == sorted(tail, key=order.index), tail


async def test_heartbeat_advances_while_blocked(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = BlockingExecutor()
    gateway, secret, _ = make_gateway(
        workspace, executor, ExecutionStore(None), heartbeat_interval_s=0.05
    )
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "heartbeat task")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.started.wait(), timeout=2.0)
    first = await status_of(gateway, secret, session, execution_id)
    await asyncio.sleep(0.2)
    second = await status_of(gateway, secret, session, execution_id)
    assert first["heartbeat_at"] is not None
    assert second["heartbeat_at"] > first["heartbeat_at"]
    assert second["worker_alive"] is True
    assert second["status"] == "RUNNING"
    executor.release.set()
    await wait_terminal(gateway, secret, session, execution_id)


# ── P0-I/R: explicit cancel + isolation ────────────────────────────────
async def test_explicit_cancel_works_and_is_idempotent(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = BlockingExecutor()
    gateway, secret, _ = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "cancel me")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.started.wait(), timeout=2.0)

    async def cancel():
        response = await rpc(
            gateway,
            "tools/call",
            {"name": "process.cancel", "arguments": {"execution_id": execution_id}},
            secret=secret,
            session=session,
        )
        return response["result"]["structuredContent"]

    first = await cancel()
    assert first["result"]["phase"] == "CANCELLED"
    second = await cancel()
    assert second["ok"] is True
    assert second["result"]["phase"] == "CANCELLED"


async def test_cross_principal_cancel_blocked(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    auth = RemoteAuth()
    secret_a = issue(auth, workspace, "alice")
    secret_b = issue(auth, workspace, "bob")
    executor = BlockingExecutor()
    gateway, _, _ = make_gateway(workspace, executor, ExecutionStore(None), auth=auth)
    session_a = await initialize(gateway, secret_a)
    session_b = await initialize(gateway, secret_b)
    envelope = await submit_hicode(gateway, secret_a, session_a, "alice task")
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(executor.started.wait(), timeout=2.0)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "process.cancel", "arguments": {"execution_id": execution_id}},
        secret=secret_b,
        session=session_b,
    )
    envelope_b = response["result"]["structuredContent"]
    assert envelope_b["ok"] is False
    assert envelope_b["error_code"] == "TOOL_DENIED"
    executor.release.set()
    await wait_terminal(gateway, secret_a, session_a, execution_id)


async def test_workspace_mismatch_status_blocks(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    other = make_repo(tmp_path / "other")
    auth = RemoteAuth()
    secret = auth.issue("tester", permissions=PERMS, workspaces=[str(workspace), str(other)])[1]
    executor = BlockingExecutor()
    executor.release.set()
    gateway, _, _ = make_gateway(
        workspace, executor, ExecutionStore(None), auth=auth, secret=secret
    )
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "task", wait=True)
    execution_id = envelope["execution_id"]
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "process.status",
            "arguments": {"execution_id": execution_id, "workspace": str(other)},
        },
        secret=secret,
        session=session,
    )
    blocked = response["result"]["structuredContent"]
    assert blocked["ok"] is False
    assert blocked["error_code"] == "WORKSPACE_DENIED"


async def test_no_pending_tasks_are_left(tmp_path: Path) -> None:
    workspace = make_repo(tmp_path)
    executor = BlockingExecutor()
    executor.release.set()
    gateway, secret, adapter = make_gateway(workspace, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await submit_hicode(gateway, secret, session, "task")
    await wait_terminal(gateway, secret, session, envelope["execution_id"])
    await asyncio.sleep(0)
    assert not adapter.jobs._tasks
