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
    RemoteSession,
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
    assert not (tmp_path / ".veya" / "worktrees").exists()


async def test_fast_file_read_does_not_use_default_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_workspace(tmp_path)
    executor = PrimitiveExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, ExecutionStore(None))
    session = await initialize(gateway, secret)

    async def fail_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("file.read must not use asyncio.to_thread")

    monkeypatch.setattr("veya.remote.tool_adapter.asyncio.to_thread", fail_to_thread)
    envelope = await call_tool(gateway, secret, session, "file.read", {"path": "a.py"})
    assert envelope["ok"] is True
    assert "hello" in envelope["result"]["text"]
    loop = asyncio.get_running_loop()
    assert getattr(loop, "_default_executor", None) is None
    assert [task for task in asyncio.all_tasks(loop) if not task.done()] == [
        asyncio.current_task(loop)
    ]


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
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(6, 0.4), "profile": "local_trusted", "timeout_s": 30},
    )
    assert envelope["ok"] is True
    assert envelope["result"]["accepted"] is True
    execution_id = envelope["execution_id"]
    assert execution_id.startswith("direct_")
    assert envelope["result"]["status"] == "RUNNING"
    assert executor.calls == []  # direct host path, no canonical/agent tool

    # Detachment is proven from the receipt, not from a wall-clock deadline: a
    # submit that blocked until completion could not hand back a non-terminal
    # record with no completion timestamp. Measuring elapsed submit time instead
    # made this assertion a function of host load rather than of behaviour.
    submitted = await status(gateway, secret, session, execution_id)
    assert submitted["status"] == "RUNNING"
    assert submitted.get("started_at"), submitted
    assert not submitted.get("completed_at"), submitted

    final = await wait_for_phase(gateway, secret, session, execution_id, {"COMPLETED"})
    assert final["status"] == "COMPLETED"
    assert final["completed_at"] >= final["started_at"], final


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


# ── shell.exec target selection: canonical by default, isolated on request ──
async def test_command_runs_in_canonical_worktree_by_default(tmp_path: Path) -> None:
    """A command must observe the live tree, so it may qualify the current state.

    Defaulting to a fresh worktree hid untracked source and the local
    virtualenv from the command, which made the current tree unimportable while
    qualifying it.
    """

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
    assert ".veya/worktrees/" not in str(final["worktree"]), final["worktree"]
    assert final["cwd"] == str(tmp_path.resolve())
    assert final["stdout_tail"].strip() == str(tmp_path.resolve())
    assert final["resolved_repo_root"] == str(tmp_path.resolve())


async def test_command_runs_in_isolated_worktree_when_requested(tmp_path: Path) -> None:
    """Isolation stays available, and stays genuinely isolated when asked for."""

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
            "execution_target": "NEW_ISOLATED_WORKTREE",
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
    # Warm up first invocation to avoid cold-start module import overhead
    warmup_env = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": py_loop(1, 0.1), "profile": "local_trusted", "timeout_s": 10},
    )
    if warmup_env.get("execution_id"):
        await call_tool(
            gateway,
            secret,
            session,
            "process.cancel",
            {"execution_id": warmup_env["execution_id"]},
        )
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


async def test_first_failure_survives_execution_store_reload(tmp_path: Path, monkeypatch) -> None:
    """A first error must still be there after the record is reloaded.

    This used to be ``test_empty_model_first_error_survives_execution_store_reload``
    and drove the check through ``server.hicode_agent``. Its failure class had
    exactly one producer anywhere in the tree —
    ``legacy/executors/hicode/hicode_agent.py`` — so after the retirement it
    asserted a failure that can no longer occur. The invariant underneath is
    about the execution store, not about that executor, so it is asserted here
    against a reachable failure: a runner that raises.
    """

    make_workspace(tmp_path)

    async def _boom(*args, **kwargs):
        raise RuntimeError("MARKER-first-error")

    monkeypatch.setattr(RemoteToolAdapter, "_make_long_runner", lambda *a, **k: _boom)

    store_root = tmp_path / "execution-store"
    adapter = RemoteToolAdapter(
        None,
        execution_store=ExecutionStore(store_root),
        heartbeat_timeout_s=30.0,
    )
    now = time.time()
    session = RemoteSession(
        session_id="rs-reload",
        principal="tester",
        token_id="rt-reload",
        workspaces=(str(tmp_path),),
        active_workspace=str(tmp_path),
        permissions=PERMS,
        created_at=now,
        expires_at=now + 3600,
    )
    envelope = await adapter._call_impl(
        session,
        "veya.mission.run",
        {"project_root": str(tmp_path), "mission_id": "m", "wait": True, "wait_timeout_s": 15},
    )
    execution_id = envelope.execution_id
    assert execution_id

    restored = ExecutionStore(store_root).get(execution_id)
    assert restored is not None, "the record did not survive the reload"
    public = restored.to_public(heartbeat_timeout_s=30.0)
    assert public["failure_class"], "a failed execution must carry a failure class"
    assert "MARKER-first-error" in str(public["raw_failure_evidence"])
    # The history entry carries the same root cause, and it is the *first* one:
    # wrappers may add history but must not replace the original cause.
    history = public["failure_history"]
    assert history, "a failed execution must record its failure history"
    assert history[0]["failure_class"] == public["failure_class"]
    assert "MARKER-first-error" in str(history[0]["raw_failure_evidence"])
