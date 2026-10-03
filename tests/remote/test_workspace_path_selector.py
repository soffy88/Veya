from __future__ import annotations

import json
import subprocess
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

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
REPO_TOOLS = {
    "git.status",
    "git.diff",
    "git.log",
    "test.run",
    "build.run",
    "shell.exec",
}


def _repo(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "README.md").write_text(name, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


class Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append((name, kwargs))
        if name == "coding_worktree_create":
            repo = Path(str(kwargs["workspace_path"]))
            worktree = repo / ".veya" / "worktrees" / str(kwargs["task_id"])
            worktree.mkdir(parents=True, exist_ok=True)
            return json.dumps(
                {
                    "status": "ok",
                    "data": {"worktree": {"path": str(worktree), "repo_root": str(repo)}},
                }
            )
        if name == "hicode_run":
            return json.dumps({"status": "ok", "data": {"output": str(kwargs["workspace"])}})
        return json.dumps({"status": "ok", "data": {"stdout": "done"}})


async def _rpc(
    gateway: Any, secret: str, session: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    assert response is not None
    return response["result"]["structuredContent"]


@pytest.fixture
def gateway(tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = _repo(projects, "hevi")
    executor = Executor()
    auth = RemoteAuth()
    _, secret = auth.issue("selector-test", permissions=PERMS, workspaces=[str(projects)])
    audit = RemoteAudit()
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(default_workspace=str(projects)),
        audit=audit,
        adapter=RemoteToolAdapter(executor, redact=audit.redact),
    )
    return gateway, secret, projects, repo, executor


async def _session(gateway: Any, secret: str) -> str:
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    assert response is not None
    return str(response["result"]["sessionId"])


def test_live_schema_exposes_canonical_selector() -> None:
    tools = {item["name"]: item for item in RemoteToolAdapter().list_tools()}
    for name in REPO_TOOLS:
        selector = tools[name]["inputSchema"]["properties"].get("workspace_path")
        assert selector and selector["type"] == "string", name


@pytest.mark.asyncio
async def test_workspace_path_selects_nested_repo_for_git_and_shell(gateway) -> None:
    server, secret, projects, repo, _executor = gateway
    session = await _session(server, secret)
    selector = str(repo.relative_to(projects))

    status = await _rpc(server, secret, session, "git.status", {"workspace_path": selector})
    assert status["ok"] is True
    assert status["result"]["repo_root"] == str(repo.resolve())
    assert status["result"]["cwd"] == str(repo.resolve())

    shell = await _rpc(
        server,
        secret,
        session,
        "shell.exec",
        {"workspace_path": selector, "command": "pwd", "wait": True},
    )
    assert shell["ok"] is True
    assert str(repo.resolve()) in shell["result"]["stdout_tail"]




@pytest.mark.asyncio
async def test_workspace_path_escape_is_blocked(gateway) -> None:
    server, secret, _projects, _repo, _executor = gateway
    session = await _session(server, secret)
    result = await _rpc(server, secret, session, "git.status", {"workspace_path": "../outside"})
    assert result["ok"] is False
    assert result["error_code"] == "WORKSPACE_DENIED"
