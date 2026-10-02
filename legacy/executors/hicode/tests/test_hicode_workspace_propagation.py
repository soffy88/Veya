"""Regression coverage for explicit workspace propagation through Hicode."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

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


def _git_repo(path: Path, name: str) -> Path:
    repo = path / name
    repo.mkdir()
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


def _gateway(
    projects: Path,
    repos: list[Path],
    *,
    store: ExecutionStore | None = None,
):
    auth = RemoteAuth()
    _, secret = auth.issue(
        "hicode-regression",
        permissions=PERMS,
        workspaces=[str(projects)],
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(
        None,
        redact=audit.redact,
        execution_store=store or ExecutionStore(None),
        heartbeat_interval_s=0.02,
        heartbeat_timeout_s=10.0,
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(
            ttl_s=3600,
            max_sessions=8,
            default_workspace=str(projects),
        ),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret


async def _rpc(
    gateway: Any, secret: str, method: str, params: dict[str, Any], session: str | None = None
):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
        session_header=session,
    )


async def _initialize(gateway: Any, secret: str) -> str:
    response = await _rpc(gateway, secret, "initialize", {})
    return str(response["result"]["sessionId"])


async def _call(
    gateway: Any,
    secret: str,
    session: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await _rpc(
        gateway,
        secret,
        "tools/call",
        {"name": name, "arguments": arguments},
        session,
    )
    return cast(dict[str, Any], response["result"]["structuredContent"])


async def _completed(
    gateway: Any,
    secret: str,
    session: str,
    execution_id: str,
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    for _ in range(400):
        args: dict[str, Any] = {"execution_id": execution_id}
        if workspace is not None:
            args["workspace"] = str(workspace)
        result = await _call(gateway, secret, session, "process.status", args)
        assert result["ok"] is True, result
        if result["result"]["status"] in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}:
            return cast(dict[str, Any], result["result"])
        await asyncio.sleep(0.01)
    raise AssertionError(f"Hicode execution did not terminate: {execution_id}")


@pytest.fixture
def hicode_fake(monkeypatch):
    from server import hicode_agent

    captured: list[dict[str, Any]] = []

    async def fake_execute_hicode_core(
        task: str, workspace: str | None = None, **kwargs: Any
    ) -> str:
        captured.append({"task": task, "workspace": workspace, **kwargs})
        worker = str(workspace)
        return (
            f"pwd -> {worker}\n"
            f"git rev-parse --show-toplevel -> {worker}\n"
            "git branch --show-current -> main\n"
        )

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake_execute_hicode_core)
    return captured


async def _run_hicode(gateway: Any, secret: str, session: str, workspace: Path) -> dict[str, Any]:
    accepted = await _call(
        gateway,
        secret,
        session,
        "hicode.execute",
        {
            "workspace": str(workspace),
            "task": "read-only workspace identity check",
        },
    )
    assert accepted["ok"] is True, accepted
    return await _completed(gateway, secret, session, accepted["execution_id"], workspace=workspace)


async def test_hicode_explicit_workspace_propagates(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    final = await _run_hicode(gateway, secret, session, repo)

    assert final["status"] == "COMPLETED"
    assert final["requested_workspace"] == str(repo.resolve())
    assert final["resolved_repo_root"] == str(repo.resolve())
    assert final["worktree_repo_root"] == str(repo.resolve())
    assert final["worker_workspace"]
    assert str(repo.resolve()) in final["worker_workspace"]
    assert final["worker_workspace"] != str(repo.resolve())


async def test_hicode_execution_context_keeps_workspace(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    final = await _run_hicode(gateway, secret, session, repo)
    worker = final["worker_workspace"]
    task = hicode_fake[-1]["task"]

    assert hicode_fake[-1]["workspace"] == worker
    assert f"workspace: {worker}" in task
    assert f"workspace: {projects.resolve()}\n" not in task


async def test_hicode_worker_worktree_matches_repo(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    final = await _run_hicode(gateway, secret, session, repo)
    worker = Path(final["worker_workspace"])

    assert final["worktree_repo_root"] == str(repo.resolve())
    assert hicode_fake[-1]["workspace"] == str(worker)
    assert f"pwd -> {worker}" in final["result_summary"]
    assert f"git rev-parse --show-toplevel -> {worker}" in final["result_summary"]


async def test_hicode_no_fallback_to_default_root(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    final = await _run_hicode(gateway, secret, session, repo)

    assert final["requested_workspace"] == str(repo.resolve())
    assert final["resolved_repo_root"] != str(projects.resolve())
    assert final["worktree_repo_root"] == str(repo.resolve())


async def test_hicode_cross_repo_switching(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo_a = _git_repo(projects, "repo-a")
    veya = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo_a, veya])
    session = await _initialize(gateway, secret)

    first = await _run_hicode(gateway, secret, session, repo_a)
    second = await _run_hicode(gateway, secret, session, veya)
    third = await _run_hicode(gateway, secret, session, repo_a)

    assert first["resolved_repo_root"] == third["resolved_repo_root"] == str(repo_a.resolve())
    assert second["resolved_repo_root"] == str(veya.resolve())
    assert first["worktree_repo_root"] == third["worktree_repo_root"] == str(repo_a.resolve())
    assert second["worktree_repo_root"] == str(veya.resolve())
    assert hicode_fake[-3]["workspace"] == first["worker_workspace"]
    assert hicode_fake[-2]["workspace"] == second["worker_workspace"]
    assert hicode_fake[-1]["workspace"] == third["worker_workspace"]


async def test_hicode_resume_keeps_workspace(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)
    first = await _run_hicode(gateway, secret, session, repo)

    resumed = await _rpc(gateway, secret, "initialize", {}, session=session)
    resumed_session = str(resumed["result"]["sessionId"])
    second = await _run_hicode(gateway, secret, resumed_session, repo)

    assert resumed_session == session
    assert first["requested_workspace"] == second["requested_workspace"] == str(repo.resolve())
    assert second["resolved_repo_root"] == str(repo.resolve())
    assert second["worktree_repo_root"] == str(repo.resolve())


@pytest.mark.parametrize("workspace", ["/etc", "../../"])
async def test_hicode_path_escape_denied(tmp_path: Path, workspace: str, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    result = await _call(
        gateway,
        secret,
        session,
        "hicode.execute",
        {"workspace": workspace, "task": "must not execute"},
    )

    assert result["ok"] is False
    assert result["error_code"] in {"AUTH_DENIED", "WORKSPACE_DENIED"}
    assert hicode_fake == []


async def test_hicode_symlink_escape_denied(tmp_path: Path, hicode_fake) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _git_repo(projects, "veya")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = projects / "escape"
    link.symlink_to(outside, target_is_directory=True)
    gateway, secret = _gateway(projects, [repo])
    session = await _initialize(gateway, secret)

    result = await _call(
        gateway,
        secret,
        session,
        "hicode.execute",
        {"workspace": str(link), "task": "must not execute"},
    )

    assert result["ok"] is False
    assert result["error_code"] == "WORKSPACE_DENIED"
    assert hicode_fake == []
