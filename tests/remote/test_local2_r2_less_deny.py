"""LOCAL2-R2 "don't refuse so readily" regression cover.

The user-visible complaint was "不要动不动就拒绝" (stop refusing everything).
Forensics (``~/.veya/remote_audit.jsonl``) showed three systemic false refusals
on this user's own machine:

1. read-only compound commands (``git rev-parse HEAD && git status``,
   ``test -f X; echo $?``, ``systemctl cat ...; journalctl --user ...``) were
   escalated to REQUIRE_APPROVAL because the workspace layer denied a segment
   whose target was outside the bound root — even though every segment is an
   ordinary read or a plain write into the caller's own home / ``/tmp``;
2. plain writes into ``$HOME`` / ``/tmp`` / ``/data`` (``mkdir -p ~/scratch``,
   ``touch /tmp/probe``) were refused as ``DENY_SCOPE_ESCAPE``;
3. ``process.status`` polls were refused ``WORKSPACE_DENIED`` whenever the
   session's active workspace differed from the execution's.

Each class gets a "should be allowed now" test AND a "still gated" test, so the
relaxation cannot silently widen the dangerous cases.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from veya.remote.action_gateway import ActionCategory, ActionGateway
from veya.remote.execution import DurableJobManager, ExecutionStore
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.permission_engine import (
    Decision,
    OperationContext,
    PermissionEngine,
    ReasonCode,
    parse_command_context,
)
from veya.remote.tool_adapter import RemoteToolAdapter, canonical_tool_name

HOME = Path.home()


def _session(path: Path, **overrides) -> RemoteSession:
    now = time.time()
    perms = RemotePermissions(
        read=True,
        write=True,
        shell=True,
        git=True,
        network=True,
        destructive=False,
        service_control=True,
    )
    return RemoteSession(
        session_id="rs-r2",
        principal="chatgpt-web",
        token_id="rt-r2",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=perms,
        created_at=now,
        expires_at=now + 3600,
        **overrides,
    )


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


# ── 1. read-only compound commands are ordinary work, not approval bait ──
@pytest.mark.parametrize(
    "command",
    [
        "pwd; echo SEP; git rev-parse --show-toplevel",
        "git rev-parse --abbrev-ref HEAD && git rev-parse HEAD",
        "git rev-parse HEAD && git status --short && git diff --stat",
        "test -f file.txt; echo RC=$?",
        "ss -ltn | grep -E ':(8790)'",
        "ls -la && wc -l file.txt",
        "git log -6 --oneline --decorate; git rev-parse HEAD",
    ],
)
def test_read_only_compound_commands_are_auto_open(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is True, (command, code)
    assert cl.category == ActionCategory.AUTO_OPEN, (command, cl.category)


def test_compound_command_reading_user_config_is_allowed(tmp_path: Path) -> None:
    """``printf ...; cat <service's own drop-in>`` must not be a scope escape."""
    _repo(tmp_path)
    dropin = (
        HOME
        / ".config"
        / "systemd"
        / "user"
        / "veya-remote-mcp.service.d"
        / "40-execution-policy.conf"
    )
    command = f"printf '%s\\n' '--- drop-in ---'; cat {dropin}"
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is True, (code, cl.capability_id)
    assert cl.category == ActionCategory.AUTO_OPEN


# ── 2. plain writes into the caller's own home / tmp / data root ─────────
@pytest.mark.parametrize(
    "target",
    [
        HOME / "veya-r2-scratch" / "note.txt",
        Path("/tmp/veya-r2-scratch/note.txt"),
        Path("/data/soffy/projects/.r2-scratch/note.txt"),
    ],
)
def test_plain_outside_write_is_allowed(tmp_path: Path, target: Path) -> None:
    engine = PermissionEngine()
    decision = engine.evaluate(
        OperationContext(
            operation="shell.exec",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(target,),
            filesystem_effect="write",
            command_effect="REVERSIBLE_MUTATION",
        )
    )
    assert decision.decision is Decision.ALLOW, (target, decision.reason)
    assert decision.reason is ReasonCode.ALLOW_PROJECT_MUTATION


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p /tmp/veya-r2-scratch",
        "touch /tmp/veya-r2-scratch.txt",
        "tee /tmp/veya-r2-scratch.log",
    ],
)
def test_plain_outside_write_commands_are_auto_open(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is True, (command, code, cl.capability_id)


def test_heredoc_writing_into_home_is_allowed(tmp_path: Path) -> None:
    _repo(tmp_path)
    command = f"cat > {HOME}/veya-r2-notes.md <<'EOF'\nhello\nEOF"
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is True, (code, cl.capability_id)


# ── 3. the dangerous cases must stay gated (no silent widening) ─────────
@pytest.mark.parametrize(
    "target",
    [
        Path("/home/otheruser/outside.txt"),  # a foreign home is still an escape
        Path("/etc/systemd/system/evil.service"),
        Path("/usr/local/bin/evil"),
    ],
)
def test_foreign_or_host_write_stays_gated(tmp_path: Path, target: Path) -> None:
    engine = PermissionEngine()
    decision = engine.evaluate(
        OperationContext(
            operation="shell.exec",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=(target,),
            filesystem_effect="write",
            command_effect="REVERSIBLE_MUTATION",
        )
    )
    assert decision.decision is not Decision.ALLOW, target


@pytest.mark.parametrize(
    "command",
    [
        f"rm -rf {HOME}",
        "rm -rf /tmp/veya-r2-scratch",
        f"dd if=/dev/zero of={HOME}/x.img",
        "sudo mkdir -p /opt/x",
        f"chmod -R 777 {HOME}",
        "mkfs.ext4 /dev/sdb1",
        ":(){ :|:& };:",  # fork bomb — catastrophic, never approval-capable
        ": () { :|:& } ; :",
    ],
)
def test_destructive_outside_commands_stay_gated(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is False, (command, cl.category)
    assert cl.category in {ActionCategory.REQUIRE_APPROVAL, ActionCategory.DENY}, command


@pytest.mark.parametrize("command", ["ls | cat", "sleep 1 & ls", "echo hi & wait", "a | b | c"])
def test_ordinary_pipelines_are_not_a_fork_bomb(tmp_path: Path, command: str) -> None:
    _repo(tmp_path)
    gw = ActionGateway()
    ok, code, _msg, cl = gw.check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert ok is True, (command, code)


def test_plain_outside_write_does_not_unlock_destructive_git(tmp_path: Path) -> None:
    _repo(tmp_path)
    gw = ActionGateway()
    for command in ("git push origin main", "git push --force", "git reset --hard"):
        ok, _code, _msg, cl = gw.check_action(
            "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
        )
        assert ok is False, command
        assert cl.category in {ActionCategory.REQUIRE_APPROVAL, ActionCategory.DENY}, command


# ── 4. legacy / natural tool names resolve instead of "unknown tool" ─────
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("shell_exec", "shell.exec"),
        ("run_command", "shell.exec"),
        ("file_read", "file.read"),
        ("file_write", "file.write"),
        ("git_status", "git.status"),
        ("git_diff", "git.diff"),
        ("workspace_info", "workspace.info"),
        ("process_status", "process.status"),
        ("process_cancel", "process.cancel"),
        ("veya.process.status", "process.status"),
    ],
)
def test_tool_aliases_resolve_to_canonical(alias: str, canonical: str) -> None:
    assert canonical_tool_name(alias) == canonical


def test_unknown_tool_is_still_denied() -> None:
    assert canonical_tool_name("definitely.not.a.tool") == "definitely.not.a.tool"


@pytest.mark.asyncio
async def test_aliased_tool_reaches_the_handler(tmp_path: Path) -> None:
    """``workspace_info`` used to be TOOL_DENIED 'unknown tool'; it must dispatch."""
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    res = await adapter._call_impl(
        _session(tmp_path), "workspace_info", {"path": ".", "workspace": str(tmp_path)}
    )
    assert res.ok is True, res.error_code


# ── 5. process.status poll is not refused on a workspace mismatch ────────
def _exec_session(tag: str, workspace: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=tag,
        principal="owner",
        token_id="tok",
        active_workspace=workspace,
        explicit_workspace=workspace,
    )


def _binding(workspace: str) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=workspace,
        requested_realpath=workspace,
        repo_root=workspace,
        repo_identity="repo",
        worktree_path=None,
        worktree_repo_root=None,
    )


async def _ok(reporter: object) -> str:
    return "ok"


@pytest.mark.asyncio
async def test_status_poll_survives_a_workspace_mismatch(tmp_path: Path) -> None:
    """A token-owned execution is pollable even from a different active workspace."""
    manager = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    execution_id = manager.submit(
        session=_exec_session("s", "/tmp"),
        tool="worker.dispatch",
        veya_tool="v",
        binding=_binding("/tmp"),
        runner=_ok,
        execution_type="remote",
        worker_type="PI",
        idempotency_key="r2-status",
    ).execution_id
    await asyncio.sleep(0)

    # Same token, different active workspace: this used to be WORKSPACE_DENIED.
    record = manager.status(execution_id, token_id="tok", workspace_realpath=str(tmp_path))
    assert record.execution_id == execution_id

    # A different token is still refused (ownership is the real boundary).
    from veya.remote.execution import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        manager.status(execution_id, token_id="other", workspace_realpath=str(tmp_path))
    assert exc.value.code == "TOOL_DENIED"
