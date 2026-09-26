"""P1: cross-process durable reattach / stalled detection for L1 executions.

This is a REAL cross-process test: a child process submits a durable Hicode job
and is then SIGKILLed (process restart/crash). A fresh process opens the same
ExecutionStore. The execution must remain *findable* (reattach) but must NOT be
reported as still RUNNING, and must not be reported as COMPLETED. Once the
heartbeat ages out it must surface as STALLED.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_CHILD = r"""
import asyncio, sys
from pathlib import Path

from server import hicode_agent
from veya.remote import RemoteAudit, RemoteAuth, RemotePermissions, RemoteSessionManager, RemoteToolAdapter
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway

workspace = Path(sys.argv[1])
store_dir = Path(sys.argv[2])
ready = Path(sys.argv[3])
hicode_agent.DEFAULT_WORKSPACE = str(workspace)


async def fake(task, workspace=None, on_event=None, on_process=None, **kw):
    if on_event:
        on_event({"stage": "planning", "tool": None, "detail": "planning"})
    await asyncio.sleep(3600)
    return "never"


hicode_agent._execute_hicode_core = fake


async def main():
    auth = RemoteAuth()
    _, secret = auth.issue(
        "restart-tester",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(workspace)],
    )
    adapter = RemoteToolAdapter(
        None,
        redact=RemoteAudit().redact,
        execution_store=ExecutionStore(store_dir),
        heartbeat_interval_s=0.1,
        heartbeat_timeout_s=3600.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=RemoteAudit(),
        adapter=adapter,
    )
    init = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session = init["result"]["sessionId"]
    env = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hicode.execute", "arguments": {"task": "long task"}},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    ready.write_text(env["result"]["structuredContent"]["execution_id"], encoding="utf-8")
    await asyncio.sleep(3600)


asyncio.run(main())
"""


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "base.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_process_restart_reattach_but_not_resume(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    store_dir = tmp_path / "exec"
    ready = tmp_path / "eid.txt"
    script = tmp_path / "child_submit.py"
    script.write_text(_CHILD, encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    child = subprocess.Popen(
        [sys.executable, str(script), str(repo), str(store_dir), str(ready)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            if child.poll() is not None:
                _, err = child.communicate(timeout=5)
                raise AssertionError(f"child exited early: {err.decode()[-800:]}")
            time.sleep(0.1)
        assert ready.exists(), "child never submitted a durable execution"
        execution_id = ready.read_text(encoding="utf-8").strip()
        # let the worker heartbeat at least once before the crash
        time.sleep(0.6)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)

    # A fresh process opens the same store.
    from veya.remote import RemoteToolAdapter
    from veya.remote.execution import ExecutionStore

    store = ExecutionStore(store_dir)
    persisted = store.get(execution_id)
    assert persisted is not None, "PROCESS_RESTART_REATTACH: record missing"
    assert persisted.heartbeat_at is not None

    adapter = RemoteToolAdapter(
        None,
        execution_store=store,
        heartbeat_interval_s=0.1,
        heartbeat_timeout_s=1.0,
    )
    # Reattach must not require the original connection/process: read by id.
    record = adapter.jobs.status(execution_id, token_id=persisted.token_id)
    assert record.execution_id == execution_id
    public = record.to_public(heartbeat_timeout_s=1.0)
    assert public["phase"] not in {"COMPLETED"}
    # STALLED detection after the heartbeat ages out (does not claim RUNNING).
    time.sleep(1.2)
    stale = adapter.jobs.status(execution_id, token_id=persisted.token_id)
    stale_public = stale.to_public(heartbeat_timeout_s=1.0)
    assert stale_public["status"] == "STALLED", stale_public["status"]
    assert stale_public["worker_alive"] is False
    assert stale_public["phase"] not in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}
