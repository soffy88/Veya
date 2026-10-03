"""Local2 unlimited concurrency: no artificial global MCP session cap.

Acceptance mapping:

* session cap removed / unlimited semantics
* 32 parallel read-only RPCs, 0 concurrent-session-limit errors
* 8+ durable executions while new RPCs/status/reads still succeed
* parallel L1 worker.dispatch (HICODE/PI/GROK/DSH), no serialization
* multi-session / multi-client concurrency
* no session-registry growth after reads and submit/status cycles
* cancel/failure isolation and cross-user ownership denial

A session is a short-lived RPC context.  A durable execution runs independently
and must never hold or consume a session slot.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
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
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.remote.session import normalize_session_cap

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable
READ_RPC_COUNT = 32


# ── harness ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _small_sync_window(monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_DIRECT_SYNC_WINDOW_MS", "100")


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def make_gateway(tmp_path: Path, *, store: ExecutionStore | None = None):
    """Gateway with the *default* (unlimited) session admission."""
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
        sessions=RemoteSessionManager(ttl_s=3600),  # no max_sessions
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, auth, adapter


async def rpc(gateway, method, params, *, secret, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
        session_header=session,
    )


async def initialize(gateway, secret) -> str:
    response = await rpc(gateway, "initialize", {}, secret=secret)
    assert "result" in response, response
    return response["result"]["sessionId"]


async def call_tool(gateway, secret, session, name, arguments):
    response = await rpc(
        gateway,
        "tools/call",
        {"name": name, "arguments": arguments},
        secret=secret,
        session=session,
    )
    if "result" not in response:
        return {"ok": False, "error": response.get("error"), "error_code": None}
    return response["result"]["structuredContent"]


def _limit_errors(envelopes: list[dict[str, Any]]) -> int:
    count = 0
    for envelope in envelopes:
        text = str(envelope.get("error") or "") + str(envelope.get("message") or "")
        if "concurrent session limit" in text or envelope.get("error_code") == "LIMIT_EXCEEDED":
            count += 1
    return count


async def status(gateway, secret, session, execution_id):
    envelope = await call_tool(
        gateway, secret, session, "process.status", {"execution_id": execution_id}
    )
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def wait_terminal(gateway, secret, session, execution_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await status(gateway, secret, session, execution_id)
        if result["phase"] in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution {execution_id} never terminalized")


def sleep_cmd(seconds: float) -> str:
    return f'{PY} -c "import time; time.sleep({seconds})"'


# ── 1. unlimited semantics ─────────────────────────────────────────────
def test_normalize_session_cap_semantics() -> None:
    assert normalize_session_cap(None) is None
    assert normalize_session_cap(0) is None
    assert normalize_session_cap(-1) is None
    assert normalize_session_cap("") is None
    assert normalize_session_cap("0") is None
    assert normalize_session_cap("none") is None
    assert normalize_session_cap("unlimited") is None
    assert normalize_session_cap("garbage") is None
    # A huge sentinel is NOT unlimited; it stays a real cap.
    assert normalize_session_cap(999999) == 999999
    assert normalize_session_cap("8") == 8


def test_session_manager_unlimited_by_default() -> None:
    manager = RemoteSessionManager()
    assert manager.max_sessions is None
    auth = RemoteAuth()
    token, _ = auth.issue("tester", permissions=PERMS, workspaces=["/tmp"])
    for _ in range(64):
        manager.create(token)
    assert manager.active_count() == 64

    capped = RemoteSessionManager(max_sessions=1)
    capped.create(token)
    with pytest.raises(Exception) as exc:
        capped.create(token)
    assert getattr(exc.value, "code", "") == "LIMIT_EXCEEDED"


def test_create_gateway_default_and_env_semantics(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_REMOTE_MAX_SESSIONS", raising=False)
    gateway = create_gateway()
    assert gateway.sessions.max_sessions is None
    for value in ("0", "none", "unlimited"):
        monkeypatch.setenv("VEYA_REMOTE_MAX_SESSIONS", value)
        assert create_gateway().sessions.max_sessions is None
    monkeypatch.setenv("VEYA_REMOTE_MAX_SESSIONS", "7")
    assert create_gateway().sessions.max_sessions == 7


# ── 2. 32 parallel read-only RPCs ─────────────────────────────────────
async def test_parallel_read_rpcs_no_session_limit(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)

    warm = await call_tool(
        gateway, secret, session, "shell.exec", {"command": sleep_cmd(0.01), "timeout_s": 30}
    )
    await wait_terminal(gateway, secret, session, warm["execution_id"])

    calls = []
    for _ in range(READ_RPC_COUNT // 4):
        calls.append(("file.read", {"path": "a.py"}))
        calls.append(("git.status", {}))
        calls.append(("workspace.info", {}))
        calls.append(("process.status", {"execution_id": warm["execution_id"]}))
    assert len(calls) == READ_RPC_COUNT

    results = await asyncio.gather(
        *(call_tool(gateway, secret, session, name, args) for name, args in calls)
    )
    passed = sum(1 for envelope in results if envelope["ok"])
    assert passed == READ_RPC_COUNT
    assert _limit_errors(results) == 0


# ── 3. 8+ concurrent executions while RPCs keep flowing ───────────────
async def test_eight_concurrent_executions_do_not_block_new_rpc(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _adapter = make_gateway(tmp_path)
    session = await initialize(gateway, secret)

    submitted = await asyncio.gather(
        *(
            call_tool(
                gateway, secret, session, "shell.exec", {"command": sleep_cmd(1.5), "timeout_s": 30}
            )
            for _ in range(8)
        )
    )
    ids = [envelope["execution_id"] for envelope in submitted]
    assert all(envelope["ok"] and envelope["result"]["accepted"] for envelope in submitted)
    assert len(set(ids)) == 8

    snapshots = await asyncio.gather(
        *(status(gateway, secret, session, execution_id) for execution_id in ids)
    )
    active = sum(1 for snapshot in snapshots if snapshot["phase"] not in {"COMPLETED"})
    assert active >= 8

    # New RPCs, a new execution, and read-only queries must all succeed while
    # 8 durable executions are running.
    extra = await call_tool(
        gateway, secret, session, "shell.exec", {"command": sleep_cmd(0.01), "timeout_s": 30}
    )
    assert extra["ok"] is True and extra["result"]["accepted"] is True
    reads = await asyncio.gather(
        call_tool(gateway, secret, session, "file.read", {"path": "a.py"}),
        call_tool(gateway, secret, session, "git.status", {}),
        call_tool(gateway, secret, session, "workspace.info", {}),
        call_tool(gateway, secret, session, "process.status", {"execution_id": ids[0]}),
    )
    assert all(envelope["ok"] for envelope in reads)
    assert _limit_errors(reads) == 0

    for execution_id in ids:
        await call_tool(gateway, secret, session, "process.cancel", {"execution_id": execution_id})


# ── 4. parallel worker.dispatch (HICODE/PI/GROK/DSH) ──────────────────
async def test_parallel_worker_dispatch_four_workers(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)
    tracker = {"active": 0, "max_active": 0, "started": 0}

    async def runner(reporter):
        tracker["active"] += 1
        tracker["max_active"] = max(tracker["max_active"], tracker["active"])
        tracker["started"] += 1
        try:
            await asyncio.sleep(0.6)
        finally:
            tracker["active"] -= 1
        return "ok"

    def make(self, **kwargs):
        return runner

    monkeypatch.setattr(RemoteToolAdapter, "_make_cli_worker_runner", make)

    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "claude_code", "task": "A"},
                {"worker": "pi", "task": "B"},
                {"worker": "grok", "task": "C"},
                {"worker": "dsh", "task": "D"},
            ]
        },
    )
    assert envelope["ok"] is True, envelope
    child_ids = envelope["result"]["child_execution_ids"]
    assert len(child_ids) == 4 and len(set(child_ids)) == 4

    parent = await wait_terminal(gateway, secret, session, envelope["execution_id"], timeout=30)
    assert parent["aggregation"]["total"] == 4
    assert parent["aggregation"]["completed"] == 4
    workers = {child["worker_type"] for child in parent["children"]}
    assert workers == {"CLAUDE_CODE", "PI", "GROK", "DSH"}  # no cross-worker substitution
    # Parallel, not serialized: all four were in flight at once.
    assert tracker["started"] == 4
    assert tracker["max_active"] >= 2, tracker


# ── 5. multi-session / multi-client ───────────────────────────────────
async def test_multi_session_concurrent_submit_and_status(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    sessions = await asyncio.gather(*(initialize(gateway, secret) for _ in range(8)))
    assert len(set(sessions)) == 8

    submitted = await asyncio.gather(
        *(
            call_tool(
                gateway, secret, session, "shell.exec", {"command": sleep_cmd(0.2), "timeout_s": 30}
            )
            for session in sessions
        )
    )
    assert all(envelope["ok"] and envelope["result"]["accepted"] for envelope in submitted)
    ids = [envelope["execution_id"] for envelope in submitted]
    assert len(set(ids)) == 8

    queried = await asyncio.gather(
        *(
            call_tool(gateway, secret, session, "process.status", {"execution_id": execution_id})
            for session, execution_id in zip(sessions, ids, strict=True)
        )
    )
    assert all(envelope["ok"] for envelope in queried)


# ── 6. no session-registry growth (leak) ──────────────────────────────
async def test_no_session_registry_growth_after_reads_and_cycles(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    baseline = gateway.sessions.active_count()
    assert baseline == 1

    for _ in range(200):
        envelope = await call_tool(gateway, secret, session, "file.read", {"path": "a.py"})
        assert envelope["ok"] is True
    assert gateway.sessions.active_count() == baseline

    for _ in range(30):
        envelope = await call_tool(
            gateway, secret, session, "shell.exec", {"command": sleep_cmd(0.01), "timeout_s": 30}
        )
        await wait_terminal(gateway, secret, session, envelope["execution_id"])
    assert gateway.sessions.active_count() == baseline


# ── 7. cancel / failure isolation ─────────────────────────────────────
async def test_cancel_one_execution_leaves_siblings_running(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    submitted = await asyncio.gather(
        *(
            call_tool(
                gateway, secret, session, "shell.exec", {"command": sleep_cmd(3.0), "timeout_s": 30}
            )
            for _ in range(4)
        )
    )
    ids = [envelope["execution_id"] for envelope in submitted]
    cancel = await call_tool(gateway, secret, session, "process.cancel", {"execution_id": ids[0]})
    assert cancel["ok"] is True and cancel["result"]["status"] == "CANCELLED"

    for execution_id in ids[1:]:
        final = await wait_terminal(gateway, secret, session, execution_id, timeout=30)
        assert final["status"] == "COMPLETED", final


async def test_failed_execution_does_not_lock_session_pool(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    failed = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "import sys; sys.exit(3)"', "timeout_s": 30},
    )
    final = await wait_terminal(gateway, secret, session, failed["execution_id"])
    assert final["status"] in {"FAILED", "BLOCKED"}

    read = await call_tool(gateway, secret, session, "file.read", {"path": "a.py"})
    assert read["ok"] is True
    again = await call_tool(
        gateway, secret, session, "shell.exec", {"command": sleep_cmd(0.01), "timeout_s": 30}
    )
    assert again["ok"] is True and again["result"]["accepted"] is True


# ── 9. long-running execution holds no session slot ───────────────────
async def test_long_running_execution_does_not_consume_session_slot(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    baseline = gateway.sessions.active_count()

    submitted = await call_tool(
        gateway, secret, session, "shell.exec", {"command": sleep_cmd(3.0), "timeout_s": 30}
    )
    assert submitted["ok"] is True and submitted["result"]["accepted"] is True
    execution_id = submitted["execution_id"]

    # High-frequency polling + reads while the execution runs must neither be
    # rejected nor grow the session registry.
    for _ in range(25):
        reads = await asyncio.gather(
            call_tool(gateway, secret, session, "process.status", {"execution_id": execution_id}),
            call_tool(gateway, secret, session, "file.read", {"path": "a.py"}),
            call_tool(gateway, secret, session, "git.status", {}),
            call_tool(gateway, secret, session, "workspace.info", {}),
        )
        assert all(envelope["ok"] for envelope in reads)
        assert gateway.sessions.active_count() == baseline
        await asyncio.sleep(0.05)

    await wait_terminal(gateway, secret, session, execution_id)
    assert gateway.sessions.active_count() == baseline


# ── 8. security boundaries preserved ──────────────────────────────────
async def test_cross_user_execution_read_and_cancel_denied(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, auth, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    submitted = await call_tool(
        gateway, secret, session, "shell.exec", {"command": sleep_cmd(1.0), "timeout_s": 30}
    )
    execution_id = submitted["execution_id"]

    _, other_secret = auth.issue("intruder", permissions=PERMS, workspaces=[str(tmp_path)])
    other_session = await initialize(gateway, other_secret)
    read = await call_tool(
        gateway, other_secret, other_session, "process.status", {"execution_id": execution_id}
    )
    assert read["ok"] is False
    cancel = await call_tool(
        gateway, other_secret, other_session, "process.cancel", {"execution_id": execution_id}
    )
    assert cancel["ok"] is False
    await call_tool(gateway, secret, session, "process.cancel", {"execution_id": execution_id})


async def test_path_escape_is_fail_closed(tmp_path: Path) -> None:
    make_repo(tmp_path)
    gateway, secret, _, _ = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    escape = await call_tool(
        gateway, secret, session, "file.read", {"path": "../../../../etc/passwd"}
    )
    assert escape["ok"] is False
    assert escape["error_code"] in {"WORKSPACE_DENIED", "INVALID_ARGUMENT", "TOOL_DENIED"}
