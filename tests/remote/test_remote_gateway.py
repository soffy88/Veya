"""Adapter + JSON-RPC gateway tests using an injected (fake) canonical executor.

These tests prove the *gateway contract* — routing, permission enforcement,
long-job handling, protocol response shape — without importing the canonical
tool registry. The real-runtime wiring is covered by the qualification harness.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.mcp_server import create_gateway


class FakeExecutor:
    """Records canonical calls and answers the shapes the adapter expects."""

    def __init__(self, worktree: Path, *, delay_s: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.worktree = worktree
        self.delay_s = delay_s

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append((name, kwargs))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if name == "coding_worktree_create":
            self.worktree.mkdir(parents=True, exist_ok=True)
            return json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "worktree": {
                            "path": str(self.worktree),
                            # The canonical tool reports the repo it created the
                            # worktree for; the adapter verifies identity against
                            # the explicitly requested workspace.
                            "repo_root": str(kwargs.get("workspace_path", self.worktree)),
                        }
                    },
                }
            )
        if name == "read_hashline":
            return f"[hashline {kwargs['filepath']}]\n1#aa hello"
        if name == "list_files":
            return "a.py\nb/c.py"
        if name == "grep":
            return "a.py:1: hello"
        if name == "coding_worktree_status":
            return json.dumps({"status": "ok", "data": {"clean": False}})
        if name == "coding_diff":
            return json.dumps({"status": "ok", "data": {"diff": "--- a\n+++ b\n"}})
        return json.dumps({"status": "ok", "tool": name, "echo": kwargs})

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def make_gateway(
    tmp_path: Path,
    executor: FakeExecutor,
    *,
    permissions: RemotePermissions | None = None,
):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester",
        permissions=permissions or RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(tmp_path)],
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(executor, redact=audit.redact)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, audit, adapter


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


async def rpc(
    gateway,
    method: str,
    params: dict[str, Any],
    *,
    secret: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}" if secret else None,
        session_header=session,
    )


async def initialize(gateway, secret: str) -> str:
    response = await rpc(gateway, "initialize", {"clientInfo": {"name": "test"}}, secret=secret)
    assert "result" in response, response
    return response["result"]["sessionId"]


# ── protocol ───────────────────────────────────────────────────────────
def test_health_has_no_secrets(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    health = gateway.health()
    assert health["status"] == "ok"
    assert secret not in json.dumps(health)
    assert health["tools"] == 27


async def test_initialize_requires_auth(tmp_path: Path) -> None:
    gateway, _secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    response = await rpc(gateway, "initialize", {})
    assert response["error"]["data"]["error_code"] == "AUTH_DENIED"
    response = await rpc(gateway, "initialize", {}, secret="wrong")
    assert response["error"]["data"]["error_code"] == "AUTH_DENIED"
    assert gateway.sessions.active_count() == 0


async def test_initialize_creates_bound_session(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    response = await rpc(gateway, "initialize", {}, secret=secret)
    result = response["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["workspace"] == str(tmp_path.resolve())
    assert result["permissions"]["write"] is True


async def test_tools_list_requires_session(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    response = await rpc(gateway, "tools/list", {}, secret=secret)
    assert response["error"]["data"]["error_code"] == "AUTH_DENIED"
    session = await initialize(gateway, secret)
    response = await rpc(gateway, "tools/list", {}, secret=secret, session=session)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert (
        "file.read" in names
        and "veya.mission.create" in names
        and "worker.dispatch" in names
        and len(names) == 27
    )


# ── adapter routing ────────────────────────────────────────────────────
async def test_file_read_maps_to_canonical_tool(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "file.read", "arguments": {"path": "a.py"}},
        secret=secret,
        session=session,
    )
    result = response["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]["result"]
    assert "hello" in payload["text"]
    # P0-A: file.read is a direct primitive; no canonical round-trip, no job.
    assert executor.names() == []


async def test_unknown_tool_denied(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway, "tools/call", {"name": "os.exec", "arguments": {}}, secret=secret, session=session
    )
    assert response["result"]["structuredContent"]["error_code"] == "TOOL_DENIED"


async def test_path_escape_denied_at_call(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "file.read", "arguments": {"path": "../../etc/passwd"}},
        secret=secret,
        session=session,
    )
    assert response["result"]["structuredContent"]["error_code"] == "WORKSPACE_DENIED"


async def test_read_only_session_cannot_write(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(
        tmp_path, executor, permissions=RemotePermissions(read=True)
    )
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "file.write", "arguments": {"path": "a.py", "content": "x"}},
        secret=secret,
        session=session,
    )
    assert response["result"]["structuredContent"]["error_code"] == "TOOL_DENIED"
    assert executor.calls == []


async def test_shell_disabled_session_blocked(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(
        tmp_path, executor, permissions=RemotePermissions(read=True, write=True)
    )
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": {"command": "ls", "wait": True}},
        secret=secret,
        session=session,
    )
    assert response["result"]["structuredContent"]["error_code"] == "TOOL_DENIED"
    assert "coding_worktree_create" not in executor.names()


async def test_destructive_command_guard(tmp_path: Path) -> None:
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": {"command": "rm -rf build"}},
        secret=secret,
        session=session,
    )
    assert response["result"]["structuredContent"]["error_code"] == "POLICY_BLOCKED"
    assert executor.calls == []


async def test_file_write_uses_isolated_worktree(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, adapter = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "file.write", "arguments": {"path": "tests/test_x.py", "content": "pass"}},
        secret=secret,
        session=session,
    )
    assert response["result"]["isError"] is False
    assert executor.names() == ["coding_worktree_create", "write_file"]
    written = executor.calls[1][1]["filepath"]
    assert written.startswith(str((tmp_path / "wt").resolve()))
    assert adapter._base_dir  # sanity: adapter still bound


async def test_git_status_is_fast_sync_without_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "git.status", "arguments": {}},
        secret=secret,
        session=session,
    )
    assert response["result"]["isError"] is False
    payload = response["result"]["structuredContent"]["result"]
    assert payload["clean"] is True
    assert payload["branch"] == "main"
    # Fast git metadata must NEVER create a worktree / dispatch a job.
    assert executor.names() == []


# ── long jobs ──────────────────────────────────────────────────────────
async def test_long_job_completes_and_reports_status(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": {"command": "echo hi", "wait": True}},
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True
    execution_id = envelope["execution_id"]
    status = await rpc(
        gateway,
        "tools/call",
        {"name": "process.status", "arguments": {"execution_id": execution_id}},
        secret=secret,
        session=session,
    )
    assert status["result"]["structuredContent"]["result"]["state"] == "SUCCEEDED"


async def test_long_job_cancel(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    executor = FakeExecutor(tmp_path / "wt", delay_s=5.0)
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    start = await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": {"command": "sleep 30", "wait": False}},
        secret=secret,
        session=session,
    )
    execution_id = start["result"]["structuredContent"]["execution_id"]
    cancel = await rpc(
        gateway,
        "tools/call",
        {"name": "process.cancel", "arguments": {"execution_id": execution_id}},
        secret=secret,
        session=session,
    )
    assert cancel["result"]["structuredContent"]["result"]["state"] == "CANCELLED"


async def test_process_status_is_session_scoped(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    executor = FakeExecutor(tmp_path / "wt", delay_s=5.0)
    gateway, secret, _, _ = make_gateway(tmp_path, executor)
    session_one = await initialize(gateway, secret)
    # A different token is required for a genuinely distinct session (the same
    # token+workspace reuses its live session by design).
    _, secret_two = gateway.auth.issue(
        "other",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(tmp_path)],
    )
    session_two = await initialize(gateway, secret_two)
    start = await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": {"command": "sleep 30", "wait": False}},
        secret=secret,
        session=session_one,
    )
    execution_id = start["result"]["structuredContent"]["execution_id"]
    status = await rpc(
        gateway,
        "tools/call",
        {"name": "process.status", "arguments": {"execution_id": execution_id}},
        secret=secret_two,
        session=session_two,
    )
    assert status["result"]["structuredContent"]["error_code"] == "TOOL_DENIED"
    await rpc(
        gateway,
        "tools/call",
        {"name": "process.cancel", "arguments": {"execution_id": execution_id}},
        secret=secret,
        session=session_one,
    )


async def test_audit_trail_records_mutations_and_redacts(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    executor = FakeExecutor(tmp_path / "wt")
    gateway, secret, audit, _ = make_gateway(tmp_path, executor)
    session = await initialize(gateway, secret)
    await rpc(
        gateway,
        "tools/call",
        {
            "name": "file.write",
            "arguments": {"path": "a.py", "content": "api_key=abcd1234"},
        },
        secret=secret,
        session=session,
    )
    records = audit.records()
    writes = [r for r in records if r["tool"] == "file.write"]
    assert len(writes) == 2  # requested + outcome
    assert writes[0]["status"] == "requested"
    assert writes[-1]["status"] == "completed"
    assert writes[-1]["effect"] == "WRITE"
    assert secret not in json.dumps(records)


async def test_notification_returns_no_body(tmp_path: Path) -> None:
    gateway, secret, _, _ = make_gateway(tmp_path, FakeExecutor(tmp_path / "wt"))
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        authorization=f"Bearer {secret}",
    )
    assert response is None
