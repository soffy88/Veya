"""L1 multi-executor parallel dispatch: parent/child, isolation, aggregation, cancel."""

from __future__ import annotations

import asyncio
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
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


class FakeHicode:
    def __init__(self, *, delay: float = 0.6) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.started = 0

    async def __call__(self, task, workspace=None, on_event=None, on_process=None, **kw):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started += 1
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return f"done:{task}@{workspace}"


class FakeCLIWorker:
    """Async stand-in for a CLI worker runner (hicode is retired).

    Adapts the same controllable-delay behavior as FakeHicode to the CLI
    runner interface ``async def runner(reporter)`` so parallel-dispatch,
    isolation and cancellation tests can run against an active worker.
    """

    def __init__(self, *, delay: float = 0.6) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.started = 0

    async def __call__(self, reporter):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started += 1
        try:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return "done"


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
    return gateway, secret


async def rpc(gateway, method, params, *, secret, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
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


async def wait_phase(gateway, secret, session, execution_id, phases, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await status(gateway, secret, session, execution_id)
        if result["phase"] in phases:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {phases}")


def _patch_hicode(monkeypatch, tmp_path: Path, fake: Any) -> None:
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)


def _patch_fake_worker(monkeypatch, fake: Any) -> None:
    """Patch the CLI worker runner with an async fake (hicode is retired).

    Preserves the real worktree creation/telemetry path; only the final
    command execution is replaced by the fake's controllable delay.
    """
    from veya.remote import tool_adapter

    def _make_fake_runner(self, *, worker, session, ws_binding, repo_root, lane, **kwargs):
        async def _run(reporter):
            reporter.phase("STARTING", message=f"starting {worker} worker", event="STARTING")
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"{worker} worker started",
            )
            worktree, verified_repo = await self._ensure_isolated_worktree(
                session, repo_root, lane, execution_id=reporter._execution_id
            )
            reporter.set_worktree(worktree, verified_repo)
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"isolated worktree ready: {worktree}",
                worker_workspace=worktree,
            )
            return await fake(reporter)

        return _run

    monkeypatch.setattr(
        tool_adapter.RemoteToolAdapter, "_make_cli_worker_runner", _make_fake_runner
    )


async def test_parallel_dispatch_parent_and_children(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)
    fake = FakeCLIWorker()
    _patch_fake_worker(monkeypatch, fake)
    from veya.remote import tool_adapter

    monkeypatch.setitem(tool_adapter._WORKER_BLOCKERS, "codex", "TEST_BLOCKER")
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "claude_code", "task": "task-a"},
                {"worker": "claude_code", "task": "task-b"},
                {"worker": "claude_code", "task": "task-c"},
                {"worker": "codex", "task": "task-d"},
            ]
        },
    )
    assert envelope["ok"] is True, envelope
    parent = envelope["execution_id"]
    assert parent.startswith("parent_")
    child_ids = envelope["result"]["child_execution_ids"]
    assert len(child_ids) == 4 and len(set(child_ids)) == 4
    parent_status = await wait_phase(
        gateway, secret, session, parent, {"COMPLETED", "PARTIAL_COMPLETED", "FAILED"}, timeout=30
    )
    counts = parent_status["aggregation"]
    assert counts["total"] == 4
    assert counts["completed"] == 3
    assert counts["blocked"] == 1
    assert parent_status["status"] == "PARTIAL_COMPLETED"
    workers = {child["worker_type"] for child in parent_status["children"]}
    assert {"CLAUDE_CODE", "CODEX"} <= workers
    # parallel, not serial
    assert fake.max_active >= 2, fake.max_active
    # distinct isolated worktrees
    worktrees = {
        child["execution_id"]: (await status(gateway, secret, session, child["execution_id"]))
        for child in parent_status["children"]
        if child["worker_type"] == "CLAUDE_CODE"
    }
    paths = {s["worker_workspace"] for s in worktrees.values()}
    assert len(paths) == 3
    assert all(p and ".veya/worktrees/task-" in p for p in paths)


async def test_sibling_failure_does_not_cancel_others(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)
    fake = FakeCLIWorker()
    _patch_fake_worker(monkeypatch, fake)
    from veya.remote import tool_adapter

    monkeypatch.setitem(tool_adapter._WORKER_BLOCKERS, "codex", "TEST_BLOCKER")
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "claude_code", "task": "good-1"},
                {"worker": "codex", "task": "blocked"},
                {"worker": "claude_code", "task": "good-2"},
            ]
        },
    )
    parent = envelope["execution_id"]
    final = await wait_phase(
        gateway, secret, session, parent, {"COMPLETED", "PARTIAL_COMPLETED", "FAILED"}, timeout=30
    )
    assert final["aggregation"]["completed"] == 2
    assert final["aggregation"]["blocked"] == 1
    blocked = [c for c in final["children"] if c["worker_type"] == "CODEX"]
    assert len(blocked) == 1
    assert blocked[0]["execution_mode"] == "direct_codex"
    assert blocked[0]["status"] == "BLOCKED"
    for child in final["children"]:
        if child["worker_type"] == "CLAUDE_CODE":
            assert child["status"] == "COMPLETED"


async def test_blocked_worker_failure_truth_reaches_parent_process_status(
    tmp_path: Path, monkeypatch
) -> None:
    make_repo(tmp_path)

    class _FailedWorker:
        async def __call__(self, reporter):
            from veya.remote.execution import ExecutionBlocked

            raise ExecutionBlocked("CLAUDE_CODE_TEST_BLOCKED", "known provider failure")

    _patch_fake_worker(monkeypatch, _FailedWorker())
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "claude_code", "task": "known failure"}]},
    )
    assert envelope["ok"] is True, envelope
    parent = envelope["execution_id"]
    final = await wait_phase(gateway, secret, session, parent, {"FAILED"})
    child = final["children"][0]

    assert child["status"] == "BLOCKED"
    assert child["failure_class"]
    assert child["failure_source"]
    assert child["failure_message"].endswith("known provider failure")
    assert child["failure_detail"].endswith("known provider failure")
    assert child["provider_error_code"] == "CLAUDE_CODE_TEST_BLOCKED"
    assert child["exit_code"] is None
    assert child["last_event"]["phase"] == "BLOCKED"


def test_worker_commands_are_not_hicode_wrappers(tmp_path: Path, monkeypatch) -> None:
    pi_target = tmp_path / "node_modules" / "pi-coding-agent" / "cli.js"
    pi_target.parent.mkdir(parents=True)
    pi_target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    pi_target.chmod(0o755)
    pi_bin = tmp_path / "pi"
    pi_bin.symlink_to(pi_target)
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_bin.chmod(0o755)
    antigravity_bin = tmp_path / "agy"
    antigravity_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    antigravity_bin.chmod(0o755)
    opencode_bin = tmp_path / "opencode"
    opencode_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    opencode_bin.chmod(0o755)
    monkeypatch.setenv("VEYA_PI_BIN", str(pi_bin))
    monkeypatch.setenv("VEYA_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("VEYA_CODEX_MODEL", "CODEX_TEST_MODEL")
    monkeypatch.setenv("VEYA_ANTIGRAVITY_BIN", str(antigravity_bin))
    monkeypatch.setenv("VEYA_ANTIGRAVITY_MODEL", "AGY_TEST_MODEL")
    monkeypatch.setenv("VEYA_OPENCODE_BIN", str(opencode_bin))
    monkeypatch.setenv("VEYA_OPENCODE_MODEL", "opencode-go/deepseek-v4.1-flash")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:10100/v1")
    monkeypatch.setenv("VEYA_LLM_ENDPOINT", "http://127.0.0.1:8791/v1")
    monkeypatch.setenv("CODEX_NORMAL_ENV_SENTINEL", "keep-me")

    from veya.remote.tool_adapter import (
        _resolve_antigravity_model,
        _resolve_opencode_model,
        _worker_command,
        _worker_model_identity,
    )

    dsh_argv, dsh_env = _worker_command("dsh", "do x")
    pi_argv, _ = _worker_command("pi", "do x")
    grok_argv, _ = _worker_command("grok", "do x")
    codex_argv, codex_env = _worker_command("codex", "do x")
    agy_argv, agy_env = _worker_command("antigravity", "do x")
    opencode_argv, opencode_env = _worker_command("opencode", "do x")
    assert "dsh" in dsh_argv[0] and "--profile" in dsh_argv
    assert "headless" in dsh_argv
    assert dsh_env.get("DEEPSEEK_BASE_URL", "").startswith("http://127.0.0.1:8791")
    assert dsh_env.get("DEEPSEEK_DEFAULT_MODEL") == _worker_model_identity("dsh")[1]
    assert dsh_env.get("DEEPSEEK_API_KEY")
    assert "pi" in pi_argv[0] and "--provider" in pi_argv
    assert _worker_model_identity("pi")[0] in pi_argv
    assert "--model" in pi_argv and _worker_model_identity("pi")[1] in pi_argv
    assert "--tools" in pi_argv and "read,bash,edit,write" in pi_argv
    assert "--approve" in pi_argv
    assert "grok" in grok_argv[0] and "--model" in grok_argv
    assert _worker_model_identity("grok")[1] in grok_argv
    assert "--tools" in grok_argv
    assert any("run_terminal_command" in value for value in grok_argv)
    assert "--sandbox" in grok_argv and "workspace" in grok_argv
    assert codex_argv[0] == str(codex_bin) and codex_argv[1] == "exec"
    assert "--ignore-user-config" in codex_argv
    assert "--model" in codex_argv and "CODEX_TEST_MODEL" in codex_argv
    assert "workspace-write" in codex_argv and codex_env.get("HOME")
    assert "OPENAI_BASE_URL" not in codex_env
    assert "VEYA_LLM_ENDPOINT" not in codex_env
    assert codex_env.get("CODEX_NORMAL_ENV_SENTINEL") == "keep-me"
    assert agy_argv[0] == str(antigravity_bin)
    assert "--print" in agy_argv and "--mode" in agy_argv and "accept-edits" in agy_argv
    assert "--sandbox" in agy_argv
    assert "--dangerously-skip-permissions" in agy_argv
    assert "OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS" not in agy_env
    assert "--model" in agy_argv and _resolve_antigravity_model() in agy_argv
    assert "--print-timeout" in agy_argv and "10m" in agy_argv
    assert agy_env.get("HOME")
    assert opencode_argv[0] == str(opencode_bin) and "run" in opencode_argv
    assert "--model" in opencode_argv and _resolve_opencode_model() in opencode_argv
    assert opencode_env.get("HOME")
    assert all(
        "hicode" not in str(value).lower()
        for argv in (
            dsh_argv,
            pi_argv,
            grok_argv,
            codex_argv,
            agy_argv,
            opencode_argv,
        )
        for value in argv
    )
    identities = {
        w: _worker_model_identity(w)
        for w in ("hicode", "dsh", "pi", "grok", "codex", "antigravity", "opencode")
    }
    assert len({m for _p, m in identities.values()}) >= 3
    assert all(provider and model for provider, model in identities.values())


def test_codex_default_model_is_luna(monkeypatch) -> None:
    from veya.remote.tool_adapter import _resolve_codex_model

    monkeypatch.delenv("VEYA_CODEX_MODEL", raising=False)
    assert _resolve_codex_model() == "gpt-5.6-luna"


def test_pi_binary_resolver_rejects_non_agent_override(tmp_path: Path, monkeypatch) -> None:
    from veya.remote.tool_adapter import _resolve_pi_binary

    fake_pi = tmp_path / "pi"
    fake_pi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pi.chmod(0o755)
    monkeypatch.setenv("VEYA_PI_BIN", str(fake_pi))

    try:
        _resolve_pi_binary()
    except RuntimeError as exc:
        assert "not a Pi Coding Agent executable" in str(exc)
    else:
        raise AssertionError("non-agent pi executable must fail closed")


def test_worker_availability_registry_lists_active_workers() -> None:
    from veya.remote.tool_adapter import worker_availability

    availability = worker_availability()
    assert {"CLAUDE_CODE", "DSH", "PI", "GROK", "CODEX", "ANTIGRAVITY", "OPENCODE"} <= set(
        availability["available_workers"]
    )
    # Hicode is retired from the active plane.
    assert "HICODE" not in availability["available_workers"]
    assert "CODEX" not in availability["temporarily_unavailable_workers"]
    assert "ANTIGRAVITY" not in availability["temporarily_unavailable_workers"]


async def test_parent_cancel_propagates_to_children(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)
    fake = FakeCLIWorker(delay=30)
    _patch_fake_worker(monkeypatch, fake)
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "claude_code", "task": "long-a"},
                {"worker": "claude_code", "task": "long-b"},
            ]
        },
    )
    parent = envelope["execution_id"]
    for _ in range(200):
        current = await status(gateway, secret, session, parent)
        if current["aggregation"]["running"] >= 1 and fake.started >= 2:
            break
        await asyncio.sleep(0.05)
    cancel = await call_tool(gateway, secret, session, "process.cancel", {"execution_id": parent})
    assert cancel["result"]["status"] == "CANCELLED"
    for child_id in envelope["result"]["child_execution_ids"]:
        child = await status(gateway, secret, session, child_id)
        assert child["status"] == "CANCELLED", child["status"]


async def test_child_cancel_is_isolated(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)
    fake = FakeCLIWorker(delay=1.5)
    _patch_fake_worker(monkeypatch, fake)
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {"worker": "claude_code", "task": "cancel-me"},
                {"worker": "claude_code", "task": "keep-me"},
            ]
        },
    )
    first, second = envelope["result"]["child_execution_ids"]
    for _ in range(200):
        if fake.started >= 2:
            break
        await asyncio.sleep(0.05)
    await call_tool(gateway, secret, session, "process.cancel", {"execution_id": first})
    assert (await status(gateway, secret, session, first))["status"] == "CANCELLED"
    other = await wait_phase(gateway, secret, session, second, {"COMPLETED"}, timeout=20)
    assert other["status"] == "COMPLETED"
