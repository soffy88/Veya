"""P0-N: explicit workspace binding is authoritative and fail-closed.

Reproduces the observed regression: the session/default repo is ``oprim`` while
the client explicitly requests ``veya``. Execution must land in ``veya`` (or be
blocked) — never silently inside ``oprim``.
"""

from __future__ import annotations

import json
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
from veya.remote.mcp_server import create_gateway
from veya.remote.workspace_binding import (
    git_repo_identity,
    resolve_requested_workspace,
    verify_worktree_repo_identity,
)

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


class WorktreeExecutor:
    """Controllable canonical executor that records every dispatch."""

    def __init__(self, *, repo_root_override: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.repo_root_override = repo_root_override

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append((name, kwargs))
        if name == "coding_worktree_create":
            workspace = str(kwargs["workspace_path"])
            path = Path(workspace) / ".veya" / "worktrees" / f"task-{kwargs['task_id']}"
            path.mkdir(parents=True, exist_ok=True)
            repo_root = self.repo_root_override or workspace
            return json.dumps(
                {"status": "ok", "data": {"worktree": {"path": str(path), "repo_root": repo_root}}}
            )
        if name in {"coding_run_command", "coding_run_tests", "coding_build"}:
            return json.dumps({"status": "ok", "data": {"stdout": "done"}})
        return json.dumps({"status": "ok", "tool": name})

    def dispatched(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for _n, kwargs in self.calls if _n == name]


def make_repos(tmp_path: Path) -> tuple[Path, Path]:
    import subprocess

    oprim = tmp_path / "oprim"
    veya = tmp_path / "veya"
    for repo in (oprim, veya):
        repo.mkdir(parents=True)
        (repo / "README.md").write_text(f"# {repo.name}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return oprim, veya


def make_gateway(tmp_path: Path, executor: Any, workspaces: list[Path]):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester", permissions=PERMS, workspaces=[str(w) for w in workspaces]
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(executor, redact=audit.redact)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
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


async def initialize(gateway, secret, *, workspace: str | None = None) -> str:
    params: dict[str, Any] = {"clientInfo": {"name": "test"}}
    if workspace:
        params["workspace"] = workspace
    response = await rpc(gateway, "initialize", params, secret=secret)
    assert "result" in response, response
    return response["result"]["sessionId"]


async def shell_exec(gateway, secret, session, *, command: str, workspace: str | None = None):
    args: dict[str, Any] = {"command": command, "wait": True}
    if workspace:
        args["workspace"] = workspace
    return await rpc(
        gateway,
        "tools/call",
        {"name": "shell.exec", "arguments": args},
        secret=secret,
        session=session,
    )


# ── resolver unit contract ─────────────────────────────────────────────
def test_repo_identity_is_common_dir(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    assert git_repo_identity(oprim) != git_repo_identity(veya)
    binding = resolve_requested_workspace(veya, allowed_roots=[str(oprim), str(veya)])
    assert binding.repo_root == str(veya.resolve())
    assert binding.is_git_repo is True


def test_resolver_blocks_unauthorized(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    try:
        resolve_requested_workspace(veya, allowed_roots=[str(oprim)])
    except Exception as exc:
        assert getattr(exc, "reason", "") == "WORKSPACE_NOT_ALLOWED"
    else:  # pragma: no cover - must not reach
        raise AssertionError("unauthorized workspace was accepted")


def test_worktree_identity_mismatch_blocks(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    requested = resolve_requested_workspace(veya, allowed_roots=[str(veya)])
    try:
        verify_worktree_repo_identity(
            requested,
            worktree_path=str(veya / ".veya" / "worktrees" / "task-x"),
            worktree_repo_root=str(oprim),
        )
    except Exception as exc:
        assert getattr(exc, "reason", "") == "WORKTREE_REPO_IDENTITY_MISMATCH"
    else:  # pragma: no cover - must not reach
        raise AssertionError("cross-repo worktree was accepted")


# ── the regression ─────────────────────────────────────
async def test_explicit_workspace_overrides_session_default_repo(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    # Default active workspace is the first authorized root (oprim).
    session = await initialize(gateway, secret)
    response = await shell_exec(gateway, secret, session, command="pwd", workspace=str(veya))
    assert response["result"]["isError"] is False, response
    result = response["result"]["structuredContent"]["result"]
    assert str(veya.resolve()) in result["stdout_tail"]
    assert str(oprim.resolve()) not in result["stdout_tail"]
    # No cross-repo dispatch of any canonical worktree tool.
    assert not any(
        str(oprim.resolve()) in str(kwargs.get("workspace_path", ""))
        for kwargs in executor.dispatched("coding_worktree_create")
    )


async def test_no_cwd_or_previous_session_fallback(tmp_path: Path, monkeypatch) -> None:
    oprim, veya = make_repos(tmp_path)
    # Even with the process cwd inside oprim, the explicit workspace wins.
    monkeypatch.chdir(oprim)
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret, workspace=str(oprim))
    # A previous binding to oprim must not leak into the veya execution.
    await shell_exec(gateway, secret, session, command="pwd", workspace=str(oprim))
    response = await shell_exec(gateway, secret, session, command="pwd", workspace=str(veya))
    assert response["result"]["isError"] is False, response
    result = response["result"]["structuredContent"]["result"]
    assert str(veya.resolve()) in result["stdout_tail"]
    assert result["resolved_repo_root"] == str(veya.resolve())


async def test_worktree_repo_identity_mismatch_blocks_execution(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    executor = WorktreeExecutor(repo_root_override=str(oprim))
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret)
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "file.write",
            "arguments": {"path": "a.py", "content": "x", "workspace": str(veya)},
        },
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is False
    assert envelope["error_code"] == "WORKSPACE_DENIED"
    assert executor.dispatched("write_file") == []


async def test_owner_dirty_worktree_preserved(tmp_path: Path) -> None:
    oprim, veya = make_repos(tmp_path)
    owner_file = veya / "owner_dirty.txt"
    owner_file.write_text("owner work in progress\n", encoding="utf-8")
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret)
    await shell_exec(gateway, secret, session, command="pwd", workspace=str(veya))
    assert owner_file.read_text(encoding="utf-8") == "owner work in progress\n"


async def test_hicode_execute_passes_explicit_workspace(tmp_path: Path, monkeypatch) -> None:
    oprim, veya = make_repos(tmp_path)
    # The canonical Hicode sandbox resolver must accept the bound repo; point its
    # root at the test tree without importing/maintaining a global env change.
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret, workspace=str(oprim))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "hicode.execute",
            "arguments": {"task": "harmless no-op", "workspace": str(veya), "wait": True},
        },
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"]
    assert envelope["result"]["phase"] == "COMPLETED"
    # The canonical hicode tool must have been told the explicit workspace.
    assert executor.dispatched("hicode_run")[-1]["workspace"] == str(veya.resolve())


async def test_hicode_nested_repo_uses_bound_root_not_narrow_default(
    tmp_path: Path, monkeypatch
) -> None:
    oprim, veya = make_repos(tmp_path)
    from server import hicode_agent

    # The legacy HICODE_WORKSPACE default is narrower than the one Remote root.
    # Remote containment is authoritative; the worker receives a verified
    # isolated worktree under the explicitly selected nested repository.
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(oprim))
    executor = WorktreeExecutor()
    gateway, secret, _ = make_gateway(tmp_path, executor, [oprim, veya])
    session = await initialize(gateway, secret, workspace=str(oprim))
    response = await rpc(
        gateway,
        "tools/call",
        {
            "name": "hicode.execute",
            "arguments": {"task": "harmless no-op", "workspace": str(veya), "wait": True},
        },
        secret=secret,
        session=session,
    )
    envelope = response["result"]["structuredContent"]
    assert envelope["ok"] is True, envelope
    assert executor.dispatched("hicode_run")[-1]["workspace"] == str(veya.resolve())


def test_bound_hicode_workspace_is_scoped_without_global_root_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    from server import hicode_agent

    default_root = tmp_path / "default"
    child_root = tmp_path / "isolated-child"
    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(default_root))

    with hicode_agent.bound_hicode_workspace(child_root):
        assert hicode_agent._resolve_workspace(str(child_root)) == child_root.resolve()

    with pytest.raises(ValueError, match="HICODE_WORKSPACE"):
        hicode_agent._resolve_workspace(str(child_root))
