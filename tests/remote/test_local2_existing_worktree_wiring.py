"""P0 Local2 live-MCP existing-worktree wiring ratchet.

Every test here guards one qualification clause of
``VEYA_LOCAL2_LIVE_MCP_EXISTING_WORKTREE_WIRING``:

* an existing ``.veya/worktrees/*`` worktree is a TERMINAL execution target
  (no ``WorktreeManager.create``, no ``existing/.veya/worktrees/*`` nesting);
* CODE runs in the existing worktree while RUNTIME comes from the canonical
  project root (project python/pytest/ruff visible);
* ``ExecutionDomain`` (policy) maps to ``SandboxProfile`` (mechanism) without
  breaking the legacy ``profile=`` ids;
* spawn failures (``exit_code None``) terminate FAILED with taxonomy — never
  COMPLETED (``FALSE_SUCCESS=0``);
* path escape fails closed.

Live command tests drive the REAL gateway + real subprocesses (no mocks for
the execution itself). ``WorktreeManager.create`` is patched to explode if it
is ever called, so a nested-worktree regression fails loudly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runtime.coding.command_runner import resolve_interpreter_argv
from runtime.coding.sandbox_profiles import get_sandbox_profile
from runtime.coding.worktree import WorktreeManager
from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.direct_exec import run_direct_command
from veya.remote.execution import ExecutionStore, direct_spawn_failure
from veya.remote.mcp_server import create_gateway
from veya.remote.runtime_profile import (
    discover_runtime_profile,
    execution_domain_to_profile,
)
from veya.remote.tool_adapter import (
    _direct_failure_class,
    find_existing_worktree_root,
    resolve_execution_target,
)

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def make_linked_worktree(main: Path, name: str) -> Path:
    target = main / ".veya" / "worktrees" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(target)],
        check=True,
        capture_output=True,
    )
    return target


def make_venv(project_root: Path, name: str = ".venv") -> Path:
    """A minimal but REAL venv layout: bin/python executes (symlink to host)."""
    venv = project_root / name
    bindir = venv / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / "python"
    try:
        link.symlink_to(PY)
    except OSError:
        shutil.copy2(PY, link)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\nimplementation = CPython\nversion_info = 3\n", encoding="utf-8"
    )
    return venv


class GuardedExecutor:
    """Fake canonical tool executor: worktree creation is FORBIDDEN here."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append((name, kwargs))
        if name == "coding_worktree_create":
            raise AssertionError("nested worktree creation attempted via executor")
        if name in {"write_file", "edit_hashline"}:
            return json.dumps({"status": "ok", "data": {"path": kwargs.get("filepath")}})
        if name in {"coding_run_command", "coding_run_tests", "coding_build"}:
            return json.dumps({"status": "ok", "data": {"stdout": "done"}})
        return json.dumps({"status": "ok", "tool": name})

    def worktree_creates(self) -> int:
        return sum(1 for name, _ in self.calls if name == "coding_worktree_create")


@pytest.fixture()
def linked_pair(tmp_path: Path) -> tuple[Path, Path]:
    main = make_git_repo(tmp_path / "proj")
    make_venv(main)
    wt = make_linked_worktree(main, "task-existing-aaa")
    return main, wt


@pytest.fixture()
def no_create(monkeypatch: pytest.MonkeyPatch):
    """Explode if WorktreeManager.create() is ever called (P0-C)."""

    async def _forbidden(*args: Any, **kwargs: Any):  # pragma: no cover - tripwire
        raise AssertionError("WorktreeManager.create() must not run for EXISTING_WORKTREE")

    def _forbidden_sync(*args: Any, **kwargs: Any):
        raise AssertionError("WorktreeManager.create() must not run for EXISTING_WORKTREE")

    monkeypatch.setattr(WorktreeManager, "create", _forbidden_sync)
    return _forbidden


def make_gateway(workspaces: list[Path], executor: Any):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester", permissions=PERMS, workspaces=[str(w) for w in workspaces]
    )
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(executor, redact=audit.redact, execution_store=ExecutionStore(None))
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
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


async def initialize(gateway, secret, *, workspace: str) -> str:
    response = await rpc(
        gateway, "initialize", {"workspace": workspace}, secret=secret, session=None
    )
    assert "result" in response, response
    return response["result"]["sessionId"]


async def wait_record(gateway, secret, session, execution_id: str) -> dict[str, Any]:
    response = await rpc(
        gateway,
        "tools/call",
        {"name": "process.status", "arguments": {"execution_id": execution_id}},
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    return structured.get("result", structured)


def nested_worktrees(worktree: Path) -> list[Path]:
    nested = worktree / ".veya" / "worktrees"
    if not nested.is_dir():
        return []
    return [p for p in nested.iterdir() if p.is_dir()]


# ── P0-B: canonical resolver ────────────────────────────────────────────


def test_resolver_marks_existing_worktree(linked_pair: tuple[Path, Path]) -> None:
    main, wt = linked_pair
    assert resolve_execution_target(str(wt), None, "") == "EXISTING_WORKTREE"
    assert resolve_execution_target(str(main), str(wt / "sub"), "") == "EXISTING_WORKTREE"
    assert resolve_execution_target(str(main), None, "") == "CANONICAL_WORKTREE"
    assert (
        resolve_execution_target(str(main), None, "", intent="mutation")
        == "NEW_ISOLATED_WORKTREE"
    )
    assert resolve_execution_target(str(main), None, "EXISTING_WORKTREE") == "EXISTING_WORKTREE"
    assert resolve_execution_target(str(main), None, "HOST") == "HOST"
    assert resolve_execution_target(str(main), None, "CANONICAL_WORKTREE") == "CANONICAL_WORKTREE"
    assert find_existing_worktree_root(wt) == wt.resolve()
    assert find_existing_worktree_root(main) is None


# ── P0-C: terminal target, zero creation ────────────────────────────────


@pytest.mark.asyncio()
async def test_live_existing_worktree_no_nested_worktree(
    linked_pair: tuple[Path, Path], no_create: Any
) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "shell.exec",
            "arguments": {
                "workspace": str(wt),
                "command": f"{PY} --version",
                "profile": "local_trusted",
                "wait": True,
                "wait_timeout_s": 120.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    record = structured.get("result", structured)
    execution_id = structured.get("execution_id") or record.get("execution_id")
    assert execution_id, structured
    record = await wait_record(gateway, secret, session, execution_id)
    assert record["status"] == "COMPLETED", record
    assert record["exit_code"] == 0, record
    assert record["resolved_repo_root"] == str(wt), record
    assert record["worker_workspace"] == str(wt), record
    assert record["cwd"] == str(wt) or str(record["cwd"]).startswith(str(wt)), record
    assert executor.worktree_creates() == 0
    assert nested_worktrees(wt) == []


# ── P0-D: canonical runtime reused ──────────────────────────────────────


def test_existing_worktree_uses_canonical_runtime_profile(
    linked_pair: tuple[Path, Path],
) -> None:
    main, wt = linked_pair
    assert not (wt / ".venv").exists() and not (wt / "venv").exists()
    profile = discover_runtime_profile(str(wt), repo_root=str(main), force_refresh=True)
    assert profile.venv_root == str((main / ".venv").resolve())
    assert profile.python is not None
    rewritten = resolve_interpreter_argv(["venv/bin/python", "--version"], wt, profile)
    assert rewritten[0] == str(main / ".venv" / "bin" / "python"), rewritten
    assert rewritten[1:] == ["--version"]


@pytest.mark.asyncio()
async def test_shell_exec_existing_worktree(linked_pair: tuple[Path, Path], no_create: Any) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "shell.exec",
            "arguments": {
                "workspace": str(main),
                "workspace_path": f".veya/worktrees/{wt.name}",
                "command": "venv/bin/python --version",
                "profile": "local_trusted",
                "wait": True,
                "wait_timeout_s": 120.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    execution_id = structured.get("execution_id")
    assert execution_id, structured
    record = await wait_record(gateway, secret, session, execution_id)
    assert record["status"] == "COMPLETED", record
    assert "Python" in (record.get("stdout_tail") or ""), record
    assert record["resolved_repo_root"] == str(wt), record
    assert record["worker_workspace"] == str(wt), record
    assert executor.worktree_creates() == 0
    assert nested_worktrees(wt) == []


@pytest.mark.asyncio()
async def test_test_run_existing_worktree(linked_pair: tuple[Path, Path], no_create: Any) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "test.run",
            "arguments": {
                "workspace": str(wt),
                "command": f"{PY} -m pytest --version",
                "wait": True,
                "wait_timeout_s": 120.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    execution_id = structured.get("execution_id")
    assert execution_id, structured
    record = await wait_record(gateway, secret, session, execution_id)
    assert record["status"] == "COMPLETED", record
    assert record["resolved_repo_root"] == str(wt), record
    assert executor.worktree_creates() == 0
    assert nested_worktrees(wt) == []


@pytest.mark.asyncio()
async def test_build_run_existing_worktree(linked_pair: tuple[Path, Path], no_create: Any) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "build.run",
            "arguments": {
                "workspace": str(wt),
                "command": "echo build-ok",
                "wait": True,
                "wait_timeout_s": 120.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    execution_id = structured.get("execution_id")
    assert execution_id, structured
    record = await wait_record(gateway, secret, session, execution_id)
    assert record["status"] == "COMPLETED", record
    assert "build-ok" in (record.get("stdout_tail") or ""), record
    assert record["resolved_repo_root"] == str(wt), record
    assert executor.worktree_creates() == 0
    assert nested_worktrees(wt) == []


@pytest.mark.asyncio()
async def test_file_write_existing_worktree_no_nested(
    linked_pair: tuple[Path, Path], no_create: Any
) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "file.write",
            "arguments": {"workspace": str(wt), "path": "probe.txt", "content": "hi"},
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    assert structured.get("ok") is True, structured
    writes = [kw for name, kw in executor.calls if name == "write_file"]
    assert writes, executor.calls
    assert Path(writes[0]["filepath"]).resolve().parent == wt.resolve()
    assert executor.worktree_creates() == 0
    assert nested_worktrees(wt) == []


# ── P0-E/F: domain mapping + backward compatibility ─────────────────────


def test_execution_domain_profile_mapping() -> None:
    assert execution_domain_to_profile("L0_WORKSPACE_FULL") == "l0_workspace_full"
    assert execution_domain_to_profile("L0_ISOLATED") == "l0_isolated"
    assert execution_domain_to_profile("L0_HOST") == "l0_host"
    assert execution_domain_to_profile("l0_host") == "l0_host"
    assert execution_domain_to_profile("") is None
    assert execution_domain_to_profile(None) is None
    assert execution_domain_to_profile("BOGUS") is None
    # Old and new ids stay resolvable side by side (P0-F).
    for profile_id in (
        "local_trusted",
        "local_restricted",
        "docker_python",
        "docker_node",
        "l0_workspace_full",
        "l0_isolated",
        "l0_host",
    ):
        assert get_sandbox_profile(profile_id).id == profile_id


@pytest.mark.asyncio()
async def test_old_profile_backward_compatibility(
    linked_pair: tuple[Path, Path], no_create: Any
) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    for profile_id in ("local_trusted", "local_restricted"):
        if profile_id == "local_restricted" and shutil.which("bwrap") is None:
            continue
        response = await rpc(
            gateway,
            "tools/call",
            {
                "name": "shell.exec",
                "arguments": {
                    "workspace": str(wt),
                    "command": f"{PY} --version",
                    "profile": profile_id,
                    "wait": True,
                    "wait_timeout_s": 120.0,
                },
            },
            secret=secret,
            session=session,
        )
        structured = response["result"]["structuredContent"]
        execution_id = structured.get("execution_id")
        assert execution_id, (profile_id, structured)
        record = await wait_record(gateway, secret, session, execution_id)
        assert record["status"] == "COMPLETED", (profile_id, record)
        assert record["profile"] == profile_id, (profile_id, record)
    # An explicit domain wins over a legacy profile string.
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "shell.exec",
            "arguments": {
                "workspace": str(wt),
                "command": f"{PY} --version",
                "profile": "local_restricted",
                "execution_domain": "L0_WORKSPACE_FULL",
                "wait": True,
                "wait_timeout_s": 120.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    execution_id = structured.get("execution_id")
    assert execution_id, structured
    record = await wait_record(gateway, secret, session, execution_id)
    assert record["status"] == "COMPLETED", record
    assert record["profile"] == "l0_workspace_full", record
    assert nested_worktrees(wt) == []


# ── P0-G: spawn failure is FAILED, never COMPLETED ──────────────────────


@pytest.mark.asyncio()
async def test_spawn_failure_is_failed_not_completed() -> None:
    result = await run_direct_command(
        "/tmp",
        ["/definitely/missing/binary-xyz", "--version"],
        profile="local_trusted",
        timeout_s=30.0,
    )
    assert result.status == "failed"
    assert result.exit_code is None
    assert "unable to execute command" in result.stderr_tail
    assert _direct_failure_class(result) == "PROCESS_START_FAILURE"


def test_direct_spawn_failure_predicate() -> None:
    class Rec:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    assert direct_spawn_failure(
        Rec(execution_type="direct", tool="shell.exec", direct_status="failed", exit_code=None)
    )
    assert direct_spawn_failure(
        Rec(execution_type="direct", tool="test.run", direct_status="timeout", exit_code=None)
    )
    assert not direct_spawn_failure(
        Rec(execution_type="direct", tool="shell.exec", direct_status="passed", exit_code=0)
    )
    assert not direct_spawn_failure(
        Rec(execution_type="direct", tool="shell.exec", direct_status="failed", exit_code=1)
    )
    # CLI-worker/Hicode children share the direct type but never set
    # direct_status through finish_command — they must not be judged here.
    assert not direct_spawn_failure(
        Rec(execution_type="direct", tool="worker.dispatch", direct_status=None, exit_code=None)
    )
    assert not direct_spawn_failure(
        Rec(execution_type="hicode", tool="shell.exec", direct_status="failed", exit_code=None)
    )


# ── P0 path escape fails closed ─────────────────────────────────────────


@pytest.mark.asyncio()
async def test_existing_worktree_path_escape_fail_closed(
    linked_pair: tuple[Path, Path], no_create: Any
) -> None:
    main, wt = linked_pair
    executor = GuardedExecutor()
    gateway, secret = make_gateway([main, wt], executor)
    session = await initialize(gateway, secret, workspace=str(wt))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "shell.exec",
            "arguments": {
                "workspace": str(wt),
                "workspace_path": "../../outside-escape",
                "command": f"{PY} --version",
                "wait": True,
                "wait_timeout_s": 60.0,
            },
        },
        secret=secret,
        session=session,
    )
    structured = response["result"]["structuredContent"]
    assert structured.get("ok") is False, structured
    assert structured.get("execution_id") is None, structured
    assert executor.worktree_creates() == 0
