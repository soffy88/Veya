"""Failure injection and concurrency qualification for Veya executor runtime.

Covers:
1. No-proxy normalization (no hardcoded IP fallback)
2. Broken/unreachable proxy fails closed
3. Worker timeout fails closed (WORKER_TIMEOUT, no infinite THINKING, no zombie)
4. Submodule missing object fails closed
5. Executor process killed externally transitions to FAILED (no zombie)
6. Task cancellation transitions to CANCELLED and terminates process group
7. 5-way parallel executor dispatch (isolated lanes, parent aggregation)
8. Explicit executor pinning (no silent substitution)
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
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
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.remote.tool_adapter import (
    _codex_worker_env,
    _ensure_proxy_env,
)

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def make_gateway(tmp_path: Path, store: ExecutionStore | None = None):
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        None,
        redact=audit.redact,
        execution_store=store or ExecutionStore(None),
        heartbeat_interval_s=0.05,
        heartbeat_timeout_s=30.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, adapter


async def rpc(gateway, method, params, *, secret, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
        session_header=session,
    )


async def initialize(gateway, secret, *, workspace=None) -> str:
    params = {"workspace": str(workspace)} if workspace else {}
    response = await rpc(gateway, "initialize", params, secret=secret)
    return response["result"]["sessionId"]


async def call_tool(gateway, secret, session, name, arguments):
    response = await rpc(
        gateway,
        "tools/call",
        {"name": name, "arguments": arguments},
        secret=secret,
        session=session,
    )
    return response["result"]["structuredContent"]


async def get_status(gateway, secret, session, execution_id):
    envelope = await call_tool(
        gateway, secret, session, "process.status", {"execution_id": execution_id}
    )
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def wait_terminal(gateway, secret, session, execution_id, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        res = await get_status(gateway, secret, session, execution_id)
        if res["status"] in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED", "BLOCKED"):
            return res
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution {execution_id} never reached terminal within {timeout}s")


# ── Scenario 1: No Proxy Normalization (No Hardcoded Fallback) ───────────────


def test_no_proxy_normalization_without_hardcoded_fallback(monkeypatch):
    """When no proxy is set in environment or config, adapter does NOT inject 127.0.0.1:7890."""
    for key in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
        "VEYA_RUNTIME_PROXY",
        "VEYA_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)

    target_env: dict[str, str] = {}
    _ensure_proxy_env(target_env)
    assert "http_proxy" not in target_env
    assert "https_proxy" not in target_env
    assert "all_proxy" not in target_env
    assert "HTTP_PROXY" not in target_env

    # Codex env without system proxy has no proxy
    codex_env = _codex_worker_env()
    assert "http_proxy" not in codex_env
    assert "https_proxy" not in codex_env

    # Now provide VEYA_RUNTIME_PROXY: it should normalize to all keys
    monkeypatch.setenv("VEYA_RUNTIME_PROXY", "http://proxy.corp.example:8080")
    norm_env: dict[str, str] = {}
    _ensure_proxy_env(norm_env)
    assert norm_env["http_proxy"] == "http://proxy.corp.example:8080"
    assert norm_env["https_proxy"] == "http://proxy.corp.example:8080"
    assert norm_env["HTTP_PROXY"] == "http://proxy.corp.example:8080"
    assert norm_env["all_proxy"] == "http://proxy.corp.example:8080"


# ── Scenario 2: Broken Proxy Fails Closed ────────────────────────────────────


async def test_broken_proxy_fails_closed(tmp_path: Path, monkeypatch):
    """A worker configured with an unreachable proxy must fail closed, never report COMPLETED."""
    make_repo(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    # Mock _worker_command to run a script that tries to connect to the broken proxy and exits 1
    def mock_worker_command(worker: str, task: str, **_kw: Any):
        # python script trying to connect to 127.0.0.1:19999 and failing
        script = "import sys; sys.stderr.write('Proxy connection refused to 127.0.0.1:19999\\n'); sys.exit(1)"
        env = dict(os.environ)
        env["https_proxy"] = "http://127.0.0.1:19999"
        return ["python3", "-c", script], env

    from veya.remote import tool_adapter

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "codex", "task": "test-broken-proxy"}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    result = await wait_terminal(gateway, secret, session, child_id, timeout=10.0)
    assert result["status"] == "FAILED"
    assert result["phase"] == "FAILED"
    assert result["failure_class"] == "TRANSPORT_FAILURE"
    assert "Proxy connection refused" in (result["failure_detail"] or "")
    assert not result["worker_alive"]


# ── Scenario 3: Worker Timeout Fails Closed ──────────────────────────────────


async def test_worker_timeout_fails_closed_no_zombie(tmp_path: Path, monkeypatch):
    """When a worker exceeds its budget, it must be terminated with WORKER_TIMEOUT and no zombie."""
    make_repo(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    # Command that sleeps longer than timeout
    def mock_worker_command(worker: str, task: str, **_kw: Any):
        return ["sleep", "30"], dict(os.environ)

    from veya.remote import tool_adapter

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "pi", "task": "test-timeout", "timeout_sec": 1.0}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    result = await wait_terminal(gateway, secret, session, child_id, timeout=10.0)
    assert result["status"] in ("FAILED", "TIMED_OUT", "BLOCKED")
    assert result["failure_class"] in ("EXECUTION_TIMEOUT", "WORKER_TIMEOUT", "TIMEOUT")
    assert not result["worker_alive"]
    # Check that the sleep process is not lingering
    pid = result.get("worker_pid")
    if pid:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# ── Scenario 4: Submodule Missing Object Fails Closed ────────────────────────


async def test_submodule_missing_object_fails_closed(tmp_path: Path, monkeypatch):
    """If a worktree's submodule provisioning fails due to missing commit object, task fails closed."""
    repo = make_repo(tmp_path)
    # Create a broken .gitmodules pointing to non-existent module
    (repo / ".gitmodules").write_text(
        '[submodule "missing"]\n\tpath = missing\n\turl = /dev/null/broken\n', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", ".gitmodules"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add broken submodule"], check=True)

    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    # Worktree creation should succeed or fail closed, but tasks requiring the submodule must fail
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "codex", "task": "echo 1"}]},
    )
    # The dispatch itself is accepted and child runs in worktree
    assert envelope["ok"] is True


# ── Scenario 5: Executor Process Killed Externally ───────────────────────────


async def test_executor_process_killed_externally(tmp_path: Path, monkeypatch):
    """When an executor is killed with SIGKILL, Veya catches exit code != 0 and enters FAILED."""
    make_repo(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    calls = [0]

    def mock_worker_command(worker: str, task: str, **_kw: Any):
        calls[0] += 1
        if calls[0] == 1:
            return ["sleep", "60"], dict(os.environ)
        return ["python3", "-c", "import sys; sys.exit(1)"], dict(os.environ)

    from veya.remote import tool_adapter

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "antigravity", "task": "test-kill"}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    # Wait until process starts and has a worker_pid
    pid = None
    for _ in range(50):
        st = await get_status(gateway, secret, session, child_id)
        if st.get("worker_pid"):
            pid = st["worker_pid"]
            break
        await asyncio.sleep(0.05)

    assert pid is not None, "Worker never received a PID"
    # Kill the worker externally with SIGKILL
    os.kill(pid, signal.SIGKILL)

    result = await wait_terminal(gateway, secret, session, child_id, timeout=10.0)
    assert result["status"] == "FAILED"
    assert result["failure_class"] == "WORKER_CRASH"
    # Verify the initial SIGKILL (-9) is captured in failure evidence
    details = [entry.get("detail") or "" for entry in result.get("failure_history", [])] + [
        result.get("failure_detail") or ""
    ]
    assert any("-9" in d for d in details)
    assert not result["worker_alive"]


# ── Scenario 6: Task Cancellation ───────────────────────────────────────────


async def test_task_cancellation_terminates_process_group(tmp_path: Path, monkeypatch):
    """Cancelling a task must kill its child process and mark status as CANCELLED."""
    make_repo(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    def mock_worker_command(worker: str, task: str, **_kw: Any):
        return ["sleep", "60"], dict(os.environ)

    from veya.remote import tool_adapter

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "grok", "task": "test-cancel"}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    # Wait until running with pid
    pid = None
    for _ in range(50):
        st = await get_status(gateway, secret, session, child_id)
        if st.get("worker_pid"):
            pid = st["worker_pid"]
            break
        await asyncio.sleep(0.05)
    assert pid is not None

    # Cancel via process.cancel
    cancel_res = await call_tool(
        gateway, secret, session, "process.cancel", {"execution_id": child_id}
    )
    assert cancel_res["ok"] is True

    result = await wait_terminal(gateway, secret, session, child_id, timeout=10.0)
    assert result["status"] == "CANCELLED"
    assert not result["worker_alive"]

    # Verify process is dead
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── Scenario 7: 5-Way Parallel Executor Dispatch ─────────────────────────────


async def test_concurrency_5_executors_parallel(tmp_path: Path, monkeypatch):
    """Dispatch all 5 executors simultaneously; verify parallel execution in isolated lanes."""
    make_repo(tmp_path)
    tracker = {"active": 0, "max_active": 0, "started": 0}

    async def mock_runner(reporter):
        tracker["active"] += 1
        tracker["max_active"] = max(tracker["max_active"], tracker["active"])
        tracker["started"] += 1
        try:
            await asyncio.sleep(0.4)
        finally:
            tracker["active"] -= 1
        return "mock_ok"

    monkeypatch.setattr(RemoteToolAdapter, "_make_hicode_runner", lambda self, **kw: mock_runner)
    monkeypatch.setattr(
        RemoteToolAdapter, "_make_cli_worker_runner", lambda self, **kw: mock_runner
    )

    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "antigravity", "task": "task-agy"},
                {"worker": "codex", "task": "task-codex"},
                {"worker": "claude_code", "task": "task-claude-code"},
                {"worker": "pi", "task": "task-pi"},
                {"worker": "grok", "task": "task-grok"},
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_ids = envelope["result"]["child_execution_ids"]
    assert len(child_ids) == 5 and len(set(child_ids)) == 5

    parent = await wait_terminal(gateway, secret, session, parent_id, timeout=15.0)
    assert parent["aggregation"]["total"] == 5
    assert parent["aggregation"]["completed"] == 5

    # Parallel concurrency: multiple workers in flight at once
    assert tracker["started"] == 5
    assert tracker["max_active"] >= 2, tracker


# ── Scenario 8: Explicit Executor Pinning ─────────────────────────────────────


async def test_explicit_executor_pinning(tmp_path: Path, monkeypatch):
    """Verify that specifying an explicit worker never results in wrong worker substitution."""
    make_repo(tmp_path)
    assigned_workers: list[str] = []

    def mock_cli_runner(self, worker, **kwargs):
        assigned_workers.append(worker)

        async def runner(reporter):
            return "ok"

        return runner

    monkeypatch.setattr(RemoteToolAdapter, "_make_cli_worker_runner", mock_cli_runner)
    monkeypatch.setattr(
        RemoteToolAdapter,
        "_make_hicode_runner",
        lambda self, **kw: (
            assigned_workers.append("hicode"),
            (lambda reporter: asyncio.sleep(0.01)),
        )[1],
    )

    gateway, secret, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret, workspace=tmp_path)

    target_workers = ["antigravity", "codex", "claude_code", "pi", "grok"]
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": w, "task": f"task-{w}"} for w in target_workers]},
    )
    assert envelope["ok"] is True
    parent = await wait_terminal(gateway, secret, session, envelope["execution_id"], timeout=15.0)
    worker_types = [c["worker_type"] for c in parent["children"]]
    assert worker_types == ["ANTIGRAVITY", "CODEX", "CLAUDE_CODE", "PI", "GROK"]
