"""P0-R: direct fast path + async direct jobs (no LLM, streaming, cancel)."""

from __future__ import annotations

import asyncio
import json
import os
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

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable


class PrimitiveExecutor:
    """Canonical executor for read primitives; fails on any LLM/agent tool."""

    _LLM_MARKERS = ("hicode", "veya_", "agent", "llm", "planner")

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append(name)
        if any(marker in name for marker in self._LLM_MARKERS):
            raise AssertionError(f"LLM/agent tool invoked on the direct path: {name}")
        if name == "read_hashline":
            return "1#aa hello"
        if name == "list_files":
            return "a.py\nb/c.py"
        if name == "grep":
            return "a.py:1: hello"
        if name == "coding_workspace_detect":
            return json.dumps(
                {"status": "ok", "data": {"workspace": {"test_commands": [], "build_commands": []}}}
            )
        return json.dumps({"status": "ok", "tool": name})


def make_workspace(tmp_path: Path, *, git: bool = True) -> Path:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def make_gateway(
    tmp_path: Path,
    executor: Any,
    store: ExecutionStore,
    *,
    auth: RemoteAuth | None = None,
    secret: str | None = None,
):
    workspace = tmp_path
    auth = auth or RemoteAuth()
    if secret is None:
        _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(workspace)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        executor,
        redact=audit.redact,
        execution_store=store,
        heartbeat_interval_s=0.1,
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


async def call_tool(gateway, secret, session, name, arguments):
    response = await rpc(
        gateway,
        "tools/call",
        {"name": name, "arguments": arguments},
        secret=secret,
        session=session,
    )
    return response["result"]["structuredContent"]


async def status(gateway, secret, session, execution_id):
    envelope = await call_tool(
        gateway, secret, session, "process.status", {"execution_id": execution_id}
    )
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def wait_for_phase(gateway, secret, session, execution_id, phases, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await status(gateway, secret, session, execution_id)
        if result["phase"] in phases:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution never reached {phases}")


def py_loop(i: int, delay: float = 0.4, stream: str = "stdout") -> str:
    if stream == "stdout":
        body = "import time\n" + "".join(
            f"print('tick', {n}, flush=True); time.sleep({delay})\n" for n in range(i)
        )
    else:
        body = "import sys, time\n" + "".join(
            f"sys.stderr.write('err {n}\\n'); sys.stderr.flush(); time.sleep({delay})\n"
            for n in range(i)
        )
    return f'{PY} -c "{body}"'


@pytest.fixture(autouse=True)
def _small_sync_window(monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_DIRECT_SYNC_WINDOW_MS", "250")


# ── fast path is synchronous and LLM-free ──────────────────────────────
async def test_fast_file_read_sync(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "file.read", {"path": "a.py"})
    assert envelope["ok"] is True
    assert envelope.get("execution_id") is None
    assert "hello" in envelope["result"]["text"]
    assert executor.calls == []


async def test_fast_git_status_sync(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    started = time.monotonic()
    envelope = await call_tool(gateway, secret, session, "git.status", {})
    elapsed_ms = (time.monotonic() - started) * 1000
    assert envelope["ok"] is True
    assert envelope.get("execution_id") is None
    assert envelope["result"]["branch"] == "main"
    assert executor.calls == []  # never a worktree / job / LLM
    assert elapsed_ms < 2000


async def test_fast_workspace_info_sync(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "workspace.info", {})
    assert envelope["ok"] is True
    assert envelope.get("execution_id") is None
    assert "workspace" in envelope["result"]
    assert executor.calls == []


async def test_fast_artifact_tools(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, adapter = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    remote_session = gateway.sessions.require(session)
    task_id = adapter._task_id(remote_session, str(tmp_path.resolve()))
    outputs = tmp_path / ".veya" / "runs" / task_id / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "report.txt").write_text("artifact body\n", encoding="utf-8")
    listing = await call_tool(gateway, secret, session, "artifact.list", {})
    assert listing["ok"] is True
    assert "report.txt" in listing["result"]["text"]
    read = await call_tool(gateway, secret, session, "artifact.read", {"path": "report.txt"})
    assert read["ok"] is True
    assert "artifact body" in read["result"]["text"]
    assert executor.calls == []


# ── long commands detach immediately ───────────────────────────────────
async def test_long_shell_returns_execution_id(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    started = time.monotonic()
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(6, 0.4), "profile": "local_trusted", "timeout_s": 30},
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    assert envelope["ok"] is True
    assert envelope["result"]["accepted"] is True
    assert envelope["execution_id"].startswith("direct_")
    assert envelope["result"]["status"] == "RUNNING"
    assert elapsed_ms < 1500, f"submit blocked {elapsed_ms:.0f}ms"
    assert executor.calls == []  # direct host path, no canonical/agent tool
    await wait_for_phase(gateway, secret, session, envelope["execution_id"], {"COMPLETED"})


async def test_long_test_returns_execution_id(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "test.run",
        {"command": py_loop(4, 0.3), "profile": "local_trusted", "timeout_s": 30},
    )
    assert envelope["ok"] is True
    assert envelope["execution_id"].startswith("direct_")
    assert envelope["result"]["accepted"] is True
    final = await wait_for_phase(gateway, secret, session, envelope["execution_id"], {"COMPLETED"})
    assert final["exit_code"] == 0


async def test_long_build_returns_execution_id(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "build.run",
        {"command": py_loop(4, 0.3), "profile": "local_trusted", "timeout_s": 30},
    )
    assert envelope["ok"] is True
    assert envelope["execution_id"].startswith("direct_")
    assert envelope["result"]["accepted"] is True
    await wait_for_phase(gateway, secret, session, envelope["execution_id"], {"COMPLETED"})


async def test_short_command_returns_inline_result(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)
    # Warm the isolated worktree so this short command can finish inline.
    await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "print(0)"', "profile": "local_trusted", "timeout_s": 20},
    )
    await asyncio.sleep(0.4)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "print(1)"', "profile": "local_trusted", "timeout_s": 10},
    )
    eid = envelope["execution_id"]
    if envelope["result"].get("accepted"):
        final = await wait_for_phase(gateway, secret, session, eid, {"COMPLETED"})
    else:
        final = envelope["result"]
    assert final["status"] == "COMPLETED"
    assert "1" in final["stdout_tail"]


# ── Phase 1.1: command isolation is mandatory ──────────────────────────
async def test_command_runs_in_isolated_worktree(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {
            "command": f'{PY} -c "import os;print(os.getcwd())"',
            "profile": "local_trusted",
            "timeout_s": 20,
        },
    )
    final = await wait_for_phase(gateway, secret, session, envelope["execution_id"], {"COMPLETED"})
    assert "/.veya/worktrees/task-" in final["worktree"], final["worktree"]
    assert final["cwd"] == final["worktree"]
    assert final["stdout_tail"].strip() == final["worktree"]
    assert final["resolved_repo_root"] == str(tmp_path.resolve())


async def test_command_never_falls_back_to_owner_root(tmp_path: Path) -> None:
    # A non-git workspace cannot be isolated -> fail closed, no owner-root run.
    make_workspace(tmp_path, git=False)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "print(1)"', "profile": "local_trusted", "timeout_s": 10},
    )
    assert envelope["ok"] is False
    assert envelope["error_code"] in {"WORKSPACE_DENIED", "POLICY_BLOCKED"}


async def test_owner_dirty_preserved_by_command(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    dirty = tmp_path / "owner_wip.txt"
    dirty.write_text("owner work in progress\n", encoding="utf-8")
    head_before = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": f'{PY} -c "print(1)"', "profile": "local_trusted", "timeout_s": 20},
    )
    await wait_for_phase(gateway, secret, session, envelope["execution_id"], {"COMPLETED"})
    assert dirty.read_text(encoding="utf-8") == "owner work in progress\n"
    head_after = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "owner_wip.txt" in status  # still owner-untracked, not committed/cleaned


async def test_command_async_submit_latency_under_1s(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    values = []
    for _ in range(5):
        started = time.monotonic()
        envelope = await call_tool(
            gateway,
            secret,
            session,
            "shell.exec",
            {"command": py_loop(20, 0.5), "profile": "local_trusted", "timeout_s": 60},
        )
        values.append((time.monotonic() - started) * 1000)
        assert envelope["result"].get("accepted") is True
        await call_tool(
            gateway,
            secret,
            session,
            "process.cancel",
            {"execution_id": envelope["execution_id"]},
        )
    assert max(values) < 1000, values


# ── P1-B: direct_hicode worker identity + real progress projection ─────
async def test_direct_hicode_worker_identity_and_progress(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    monkeypatch.setenv("HICODE_REASONIX_MODEL", "test-model")
    monkeypatch.setenv("HICODE_REASONIX_BASE_URL", "http://opencode.test/v1")
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake_execute_hicode_core(
        task,
        workspace=None,
        max_steps=0,
        timeout_sec=0,
        session_id=None,
        continue_=False,
        on_event=None,
        force_cli=False,
        on_process=None,
    ):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
            on_event({"stage": "executing", "tool": "write_file", "detail": "write_file brief"})
            on_event(
                {"stage": "executing", "tool": "write_file", "detail": "write_file 完成 (3ms)"}
            )
            on_event({"stage": "stats", "tool": None, "detail": "tokens in=1 out=2"})
        return "done: created file"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake_execute_hicode_core)
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(None, redact=audit.redact, execution_store=ExecutionStore(None))
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"].startswith("ex_")
    assert envelope["result"]["accepted"] is True
    assert envelope["result"]["execution_mode"] == "direct_hicode"
    assert envelope["result"]["worker_type"] == "HICODE"
    assert envelope["result"]["orchestrator"] == "none"
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED"}, timeout=15
    )
    assert final["worker_type"] == "HICODE"
    assert final["model"] == "test-model"
    assert final["model_provider"] == "opencode-go"
    assert final["model_request_count"] >= 1
    assert final["tool_call_count"] >= 1
    kinds = [event["kind"] for event in final["recent_events"]]
    assert "MODEL_REQUEST_STARTED" in kinds
    assert "MODEL_REQUEST_COMPLETED" in kinds
    assert "TOOL_STARTED" in kinds
    assert "TOOL_COMPLETED" in kinds
    assert "CHECKPOINT" in kinds
    assert final["worker_workspace"] and ".veya/worktrees/task-" in final["worker_workspace"]
    assert final["worker_heartbeat"] in {"HEALTHY", "STALE", "UNKNOWN"}
    assert final["status"] == "COMPLETED"


def make_hicode_gateway(
    tmp_path: Path, store: ExecutionStore, *, auth=None, secret=None, heartbeat_interval_s=5.0
):
    auth = auth or RemoteAuth()
    if secret is None:
        _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        None,
        redact=audit.redact,
        execution_store=store,
        heartbeat_interval_s=heartbeat_interval_s,
        heartbeat_timeout_s=30.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, adapter


async def test_direct_hicode_rejects_auto_router(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway, secret, session, "hicode.execute", {"task": "x", "execution_mode": "auto"}
    )
    assert envelope["ok"] is False
    assert envelope["error_code"] == "INVALID_ARGUMENT"


async def test_hicode_cancel_propagates_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    cancelled = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(
        tmp_path, ExecutionStore(None), heartbeat_interval_s=0.05
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    for _ in range(300):
        current = await status(gateway, secret, session, execution_id)
        if current["model_in_flight"] is True:
            break
        await asyncio.sleep(0.02)
    assert current["model_in_flight"] is True
    first = await call_tool(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert first["result"]["phase"] == "CANCELLED"
    assert cancelled.is_set()
    second = await call_tool(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert second["ok"] is True
    assert second["result"]["phase"] == "CANCELLED"


async def test_hicode_llm_inflight_heartbeat(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    started = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        started.set()
        await asyncio.sleep(2)
        return "done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(
        tmp_path, ExecutionStore(None), heartbeat_interval_s=0.05
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(started.wait(), timeout=5)
    first = await status(gateway, secret, session, execution_id)
    await asyncio.sleep(0.25)
    second = await status(gateway, secret, session, execution_id)
    assert second["model_in_flight"] is True
    assert second["status"] == "RUNNING"
    assert second["worker_heartbeat"] == "HEALTHY"
    assert second["heartbeat_at"] > first["heartbeat_at"]
    await wait_for_phase(gateway, secret, session, execution_id, {"COMPLETED"}, timeout=10)


async def test_hicode_disconnect_reconnect(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    store = ExecutionStore(tmp_path / "exec")
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    release = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        await release.wait()
        return "persisted hicode result"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway_a, _, _ = make_hicode_gateway(
        tmp_path, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_a = await initialize(gateway_a, secret)
    envelope = await call_tool(gateway_a, secret, session_a, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    gateway_b, _, _ = make_hicode_gateway(
        tmp_path, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_b = await initialize(gateway_b, secret)
    observed = await status(gateway_b, secret, session_b, execution_id)
    assert observed["execution_id"] == execution_id
    assert observed["execution_mode"] == "direct_hicode"
    release.set()
    final = await wait_for_phase(
        gateway_b, secret, session_b, execution_id, {"COMPLETED"}, timeout=10
    )
    assert "persisted hicode result" in (final["result_summary"] or "")


async def test_hicode_worker_workspace_identity(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake(task, workspace=None, on_event=None, **kw):
        return f"workspace={workspace}"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED"}, timeout=10
    )
    assert final["worker_workspace"] and ".veya/worktrees/task-" in final["worker_workspace"]
    assert final["resolved_repo_root"] == str(tmp_path.resolve())
    assert final["worktree_repo_root"] == str(tmp_path.resolve())
    assert f"workspace={final['worker_workspace']}" in (final["result_summary"] or "")


async def test_unvalidated_path_cannot_reach_hicode(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", "/")
    calls: list[Any] = []

    async def fake(task, workspace=None, on_event=None, **kw):
        calls.append(workspace)
        return "should not run"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway, secret, session, "hicode.execute", {"task": "x", "workspace": "/etc"}
    )
    assert envelope["ok"] is False
    assert envelope["error_code"] in {"WORKSPACE_DENIED", "AUTH_DENIED"}
    assert calls == []


# ── streaming stdout/stderr ────────────────────────────────────────────
async def test_stdout_stream_visible(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(8, 0.3), "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    sizes = []
    deadline = time.time() + 10
    while time.time() < deadline:
        result = await status(gateway, secret, session, execution_id)
        sizes.append(result["bytes_stdout"])
        if result["phase"] == "COMPLETED":
            break
        await asyncio.sleep(0.25)
    distinct = [
        value for index, value in enumerate(sizes) if index == 0 or value != sizes[index - 1]
    ]
    assert len(distinct) >= 2, sizes
    final = await status(gateway, secret, session, execution_id)
    assert "tick 7" in final["stdout_tail"]


async def test_stderr_stream_visible(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(4, 0.3, stream="stderr"), "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    saw_stderr = False
    deadline = time.time() + 10
    while time.time() < deadline:
        result = await status(gateway, secret, session, execution_id)
        if result["bytes_stderr"] > 0:
            saw_stderr = True
        if result["phase"] == "COMPLETED":
            break
        await asyncio.sleep(0.2)
    assert saw_stderr
    final = await status(gateway, secret, session, execution_id)
    assert "err 3" in final["stderr_tail"]


async def test_heartbeat_during_quiet_command(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    quiet = f'{PY} -c "import time; time.sleep(2)"'
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": quiet, "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    first = await status(gateway, secret, session, execution_id)
    await asyncio.sleep(0.4)
    second = await status(gateway, secret, session, execution_id)
    assert first["heartbeat_at"] is not None
    assert second["heartbeat_at"] > first["heartbeat_at"]
    assert second["worker_alive"] is True
    assert second["bytes_stdout"] == 0  # quiet, but not stalled
    assert second["status"] == "RUNNING"


async def test_process_status_low_latency(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(6, 0.3), "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    durations = []
    for _ in range(10):
        started = time.monotonic()
        await status(gateway, secret, session, execution_id)
        durations.append((time.monotonic() - started) * 1000)
    assert max(durations) < 500, durations
    await wait_for_phase(gateway, secret, session, execution_id, {"COMPLETED"})


# ── no LLM / no hicode ever ────────────────────────────────────────────
async def test_direct_tools_never_invoke_llm(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    store = ExecutionStore(tmp_path / "exec")
    gateway, secret, _ = make_gateway(tmp_path, executor, store)
    session = await initialize(gateway, secret)
    calls = [
        ("file.read", {"path": "a.py"}),
        ("file.search", {"pattern": "hello"}),
        ("workspace.list", {}),
        ("git.status", {}),
        (
            "shell.exec",
            {"command": f'{PY} -c "print(1)"', "profile": "local_trusted", "timeout_s": 10},
        ),
    ]
    for name, args in calls:
        await call_tool(gateway, secret, session, name, args)
    assert not any(
        marker in call for call in executor.calls for marker in PrimitiveExecutor._LLM_MARKERS
    )


# ── reconnect / cancel / timeout survival ──────────────────────────────
async def test_direct_job_reconnect(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    store = ExecutionStore(tmp_path / "exec")
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    gateway_a, _, _ = make_gateway(tmp_path, PrimitiveExecutor(), store, auth=auth, secret=secret)
    session_a = await initialize(gateway_a, secret)
    envelope = await call_tool(
        gateway_a,
        secret,
        session_a,
        "shell.exec",
        {"command": py_loop(6, 0.4), "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    # A brand-new gateway process reads the persisted direct job.
    gateway_b, _, _ = make_gateway(tmp_path, PrimitiveExecutor(), store, auth=auth, secret=secret)
    session_b = await initialize(gateway_b, secret)
    observed = await status(gateway_b, secret, session_b, execution_id)
    assert observed["execution_id"] == execution_id
    assert observed["execution_type"] == "direct"
    final = await wait_for_phase(gateway_b, secret, session_b, execution_id, {"COMPLETED"})
    assert final["exit_code"] == 0


async def test_direct_job_cancel_and_idempotent(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(20, 0.5), "profile": "local_trusted", "timeout_s": 60},
    )
    execution_id = envelope["execution_id"]

    async def cancel():
        return await call_tool(
            gateway, secret, session, "process.cancel", {"execution_id": execution_id}
        )

    first = await cancel()
    assert first["result"]["phase"] == "CANCELLED"
    second = await cancel()
    assert second["ok"] is True
    assert second["result"]["phase"] == "CANCELLED"


async def test_direct_cancel_kills_process_group(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    child_pid_file = tmp_path / "child.pid"
    parent = (
        f'{PY} -c "import subprocess,time\n'
        f"p=subprocess.Popen(['sleep','30'])\n"
        f"open('{child_pid_file}','w').write(str(p.pid))\n"
        f'time.sleep(30)"'
    )
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": parent, "profile": "local_trusted", "timeout_s": 60},
    )
    execution_id = envelope["execution_id"]
    deadline = time.time() + 5
    while not child_pid_file.exists() and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text().strip())
    await call_tool(gateway, secret, session, "process.cancel", {"execution_id": execution_id})
    # The whole process group (parent + sleep child) must be gone.
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"child process {child_pid} survived cancel (process group not killed)")


async def test_direct_job_survives_client_timeout(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    gateway, secret, _ = make_gateway(tmp_path, PrimitiveExecutor(), ExecutionStore(None))
    session = await initialize(gateway, secret)
    # The submit RPC returns after the sync window (client timeout) while the
    # command keeps running and reaches COMPLETED.
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(5, 0.4), "profile": "local_trusted", "timeout_s": 30},
    )
    execution_id = envelope["execution_id"]
    mid = await status(gateway, secret, session, execution_id)
    assert mid["phase"] in {"STARTING", "RUNNING"}
    final = await wait_for_phase(gateway, secret, session, execution_id, {"COMPLETED"})
    assert final["exit_code"] == 0
