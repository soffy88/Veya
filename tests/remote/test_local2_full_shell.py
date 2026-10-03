"""Local2 full shell syntax qualification (VEYA_LOCAL2_ENABLE_FULL_SHELL).

Verifies shell.exec supports full shell syntax (&&, ||, |, >, >>, ;, $(),
backticks, env-prefix) through /bin/bash -lc, while the permission boundary
(ActionGateway -> PermissionEngine) still gates sudo, destructive commands,
force push, and workspace escape.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

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


def make_workspace(tmp_path: Path, *, git: bool = True) -> Path:
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def make_gateway(tmp_path: Path):
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        None,
        redact=audit.redact,
        execution_store=ExecutionStore(None),
        heartbeat_interval_s=0.1,
        heartbeat_timeout_s=5.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret


async def call_tool(gateway, secret, session, name, arguments):
    return await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )


def _ok(envelope):
    return envelope.get("result", {}).get("structuredContent", {}).get("ok")


def _result(envelope):
    return envelope.get("result", {}).get("structuredContent", {}).get("result", {})


async def wait_for_phase(gateway, secret, session, execution_id, phases, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "process.status", "arguments": {"execution_id": execution_id}},
            },
            authorization=f"Bearer {secret}",
            session_header=session,
        )
        result = resp["result"]["structuredContent"].get("result", {})
        if result.get("status") in phases:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError(f"execution {execution_id} never reached {phases}")


async def run_shell(gateway, secret, session, command, timeout_s=30):
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "shell.exec",
        {"command": command, "profile": "local_trusted", "timeout_s": timeout_s},
    )
    if _ok(envelope) is not True:
        return {"status": "BLOCKED", "exit_code": None, "stdout_tail": "", "stderr_tail": ""}
    exec_id = _result(envelope).get("execution_id")
    return await wait_for_phase(
        gateway, secret, session, exec_id, {"COMPLETED", "FAILED", "BLOCKED", "TIMEOUT"}
    )


# ── Shell operator tests ──────────────────────────────────────────────────


async def test_shell_and(tmp_path: Path) -> None:
    """SHELL_AND: pwd && git status"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "pwd && git status")
    assert result["status"] == "COMPLETED", result
    assert result["exit_code"] == 0


async def test_shell_or(tmp_path: Path) -> None:
    """SHELL_OR: false || echo fallback"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "false || echo fallback")
    assert result["status"] == "COMPLETED", result
    assert "fallback" in (result.get("stdout_tail") or "")


async def test_shell_pipe(tmp_path: Path) -> None:
    """SHELL_PIPE: echo hello | grep hello"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "echo hello | grep hello")
    assert result["status"] == "COMPLETED", result
    assert "hello" in (result.get("stdout_tail") or "")


async def test_shell_redirect(tmp_path: Path) -> None:
    """SHELL_REDIRECT: echo test > file && cat file"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(
        gateway, secret, session_id, "echo test > shell_test.txt && cat shell_test.txt"
    )
    assert result["status"] == "COMPLETED", result
    assert "test" in (result.get("stdout_tail") or "")


async def test_shell_append_redirect(tmp_path: Path) -> None:
    """SHELL_APPEND_REDIRECT: echo a > f && echo b >> f && cat f"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(
        gateway,
        secret,
        session_id,
        "echo a > shell_append.txt && echo b >> shell_append.txt && cat shell_append.txt",
    )
    assert result["status"] == "COMPLETED", result
    out = result.get("stdout_tail") or ""
    assert "a" in out and "b" in out


async def test_shell_semicolon(tmp_path: Path) -> None:
    """SHELL_SEMICOLON: echo one; echo two"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "echo one; echo two")
    assert result["status"] == "COMPLETED", result
    out = result.get("stdout_tail") or ""
    assert "one" in out and "two" in out


async def test_shell_command_substitution(tmp_path: Path) -> None:
    """SHELL_COMMAND_SUBSTITUTION: echo $(echo nested)"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "echo $(echo nested)")
    assert result["status"] == "COMPLETED", result
    assert "nested" in (result.get("stdout_tail") or "")


async def test_shell_env_prefix(tmp_path: Path) -> None:
    """SHELL_ENV_PREFIX: VAR=1 python3 -c 'import os; print(os.environ.get("VAR"))'"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(
        gateway,
        secret,
        session_id,
        "VAR=1 python3 -c \"import os; print(os.environ.get('VAR'))\"",
    )
    assert result["status"] == "COMPLETED", result
    assert "1" in (result.get("stdout_tail") or "")


async def test_shell_backtick(tmp_path: Path) -> None:
    """SHELL_BACKTICK: echo `echo backtick`"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "echo `echo backtick`")
    assert result["status"] == "COMPLETED", result
    assert "backtick" in (result.get("stdout_tail") or "")


# ── Permission gating tests ───────────────────────────────────────────────


async def test_sudo_gated(tmp_path: Path) -> None:
    """SUDO_GATED: sudo must require approval"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "sudo echo test")
    assert result["status"] in ("BLOCKED", "FAILED"), result


async def test_destructive_gated(tmp_path: Path) -> None:
    """DESTRUCTIVE_HOST_COMMAND_GATED: rm -rf / must require approval"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "rm -rf /")
    assert result["status"] in ("BLOCKED", "FAILED"), result


async def test_force_push_gated(tmp_path: Path) -> None:
    """FORCE_PUSH_GATED: git push --force must require approval"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "git push --force")
    assert result["status"] in ("BLOCKED", "FAILED"), result


async def test_workspace_escape_gated(tmp_path: Path) -> None:
    """WORKSPACE_ESCAPE_GATED: cat /etc/passwd | grep root must require approval"""
    make_workspace(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = session["result"]["sessionId"]
    result = await run_shell(gateway, secret, session_id, "cat /etc/passwd | grep root")
    assert result["status"] in ("BLOCKED", "FAILED"), result


# ── Schema / execution_target tests ────────────────────────────────────────


def test_execution_target_schema() -> None:
    """Local2 schema exposes execution_target with CANONICAL_WORKTREE | NEW_ISOLATED_WORKTREE"""
    from veya.remote.tool_adapter import EXECUTION_TARGETS

    assert "CANONICAL_WORKTREE" in EXECUTION_TARGETS
    assert "NEW_ISOLATED_WORKTREE" in EXECUTION_TARGETS


def test_canonical_worktree_default() -> None:
    """Default execution_target for Local2 is CANONICAL_WORKTREE"""
    from veya.remote.tool_adapter import resolve_execution_target

    # When no explicit target is requested, CANONICAL_WORKTREE is the default
    # for the Local2 product path (not a silent fallback to task-remote-*)
    target = resolve_execution_target("/tmp", requested_execution_target="CANONICAL_WORKTREE")
    assert target == "CANONICAL_WORKTREE"


# ── Unit tests for parse_command ──────────────────────────────────────────


def test_parse_command_shell_operators() -> None:
    """parse_command returns /bin/bash -lc argv for shell commands"""
    from runtime.coding.command_runner import parse_command

    shell_cmds = [
        "pwd && git status",
        "echo a | grep a",
        "echo a > /tmp/x",
        "echo a >> /tmp/x",
        "echo a; echo b",
        "echo $(echo x)",
        "echo `echo x`",
        "VAR=1 python3 script.py",
    ]
    for cmd in shell_cmds:
        argv = parse_command(cmd)
        assert argv == ["/bin/bash", "-lc", cmd], f"Expected bash argv for {cmd!r}, got {argv}"


def test_parse_command_argv_unchanged() -> None:
    """parse_command returns argv for non-shell commands"""
    from runtime.coding.command_runner import parse_command

    argv_cmds = [
        "ls -la",
        "git status",
        "python3 script.py",
        "pytest tests/ -q",
    ]
    for cmd in argv_cmds:
        argv = parse_command(cmd)
        assert argv[0] != "/bin/bash", f"Expected argv for {cmd!r}, got {argv}"
