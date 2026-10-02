"""Real nested-Git regression coverage for the Veya Local workspace contract."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from runtime.coding.worktree import WorktreeManager
from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
    resolve_repo_target,
)
from veya.remote.mcp_server import create_gateway
from veya.remote.workspace_binding import WorkspaceBindingError, git_repo_identity

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)


def _git_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "probe.py").write_text("print('canonical')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "nested-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


class _IsolatedFileExecutor:
    """Use the real WorktreeManager while keeping optional server deps out."""

    async def __call__(self, name: str, kwargs: dict):
        if name == "coding_worktree_create":
            manager = WorktreeManager(kwargs["workspace_path"])
            record = manager.create(kwargs["task_id"], kwargs["objective"])
            return json.dumps({"status": "ok", "data": {"worktree": record.to_dict()}})
        if name == "write_file":
            target = Path(kwargs["filepath"])
            if target.exists() and not kwargs.get("overwrite", True):
                raise RuntimeError("file exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(kwargs["content"]), encoding="utf-8")
            return "written"
        if name == "edit_hashline":
            from server.hashline import apply

            target = Path(kwargs["filepath"])
            updated = apply(
                target.read_text(encoding="utf-8"),
                start_tag=kwargs["start_tag"],
                end_tag=kwargs.get("end_tag"),
                new_text=kwargs["new_text"],
            )
            target.write_text(updated["content"], encoding="utf-8")
            return "patched"
        return json.dumps({"status": "ok", "tool": name})


def _gateway(parent: Path, executor=None) -> tuple[object, str, RemoteToolAdapter]:
    auth = RemoteAuth()
    _, secret = auth.issue("nested-test", permissions=PERMS, workspaces=[str(parent)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(executor, redact=audit.redact)
    return (
        create_gateway(
            auth=auth,
            sessions=RemoteSessionManager(default_workspace=parent),
            audit=audit,
            adapter=adapter,
        ),
        secret,
        adapter,
    )


async def _rpc(gateway, secret: str, method: str, params: dict, session: str | None = None):
    response = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
        session_header=session,
    )
    assert response is not None
    return response


async def _initialize(gateway, secret: str) -> str:
    response = await _rpc(gateway, secret, "initialize", {})
    return response["result"]["sessionId"]


async def _call(gateway, secret: str, session: str, name: str, arguments: dict):
    response = await _rpc(
        gateway,
        secret,
        "tools/call",
        {"name": name, "arguments": arguments},
        session,
    )
    return response["result"]["structuredContent"]


def test_direct_git_workspace_resolution_is_unchanged(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "direct")
    resolution = resolve_repo_target(
        repo, "src/probe.py", operation="file.read", allowed_roots=[str(repo)], require_repo=True
    )
    assert resolution.workspace_root == str(repo.resolve())
    assert resolution.repo_root == str(repo.resolve())
    assert resolution.evidence["repo_discovery"] == "bound_workspace_root"


def test_parent_resolves_each_nested_repo_and_non_repo_fails_closed(tmp_path: Path) -> None:
    stratum = _git_repo(tmp_path, "stratum")
    hevi = _git_repo(tmp_path, "hevi")
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()

    for name, repo in (("stratum", stratum), ("hevi", hevi)):
        resolution = resolve_repo_target(
            tmp_path,
            f"{name}/src/probe.py",
            operation="shell.exec",
            allowed_roots=[str(tmp_path)],
            require_repo=True,
        )
        assert resolution.repo_root == str(repo.resolve())
        assert resolution.target_path == str((repo / "src" / "probe.py").resolve())
        assert resolution.evidence["repo_discovery"] == "nested_git_repository"

    with pytest.raises(WorkspaceBindingError) as exc:
        resolve_repo_target(
            tmp_path,
            "ordinary",
            operation="shell.exec",
            allowed_roots=[str(tmp_path)],
            require_repo=True,
        )
    assert exc.value.code == "WORKSPACE_DENIED"
    assert exc.value.reason == "NOT_A_GIT_REPOSITORY"


def test_nested_path_escape_and_symlink_escape_are_denied(tmp_path: Path) -> None:
    _git_repo(tmp_path, "stratum")
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    link = tmp_path / "link-outside"
    link.symlink_to(outside, target_is_directory=True)
    try:
        for requested in ("../outside", str(outside), "/etc", "link-outside/file.py"):
            with pytest.raises(WorkspaceBindingError) as exc:
                resolve_repo_target(
                    tmp_path,
                    requested,
                    operation="file.write",
                    allowed_roots=[str(tmp_path)],
                    require_repo=True,
                )
            assert exc.value.code == "WORKSPACE_DENIED"
    finally:
        link.unlink()
        outside.rmdir()


def test_repo_identity_is_part_of_one_session_isolation_key(tmp_path: Path) -> None:
    stratum = _git_repo(tmp_path, "stratum")
    hevi = _git_repo(tmp_path, "hevi")
    session = RemoteSessionManager(default_workspace=tmp_path).create(
        RemoteAuth().issue("nested-test", permissions=PERMS, workspaces=[str(tmp_path)])[0]
    )
    adapter = RemoteToolAdapter()
    assert git_repo_identity(stratum) != git_repo_identity(hevi)
    assert adapter._task_id(session, stratum) != adapter._task_id(session, str(hevi))


@pytest.mark.asyncio
async def test_nested_file_write_patch_are_isolated_and_stale_safe(tmp_path: Path) -> None:
    stratum = _git_repo(tmp_path, "stratum")
    canonical_probe = stratum / "probe.txt"
    gateway, secret, _ = _gateway(tmp_path, _IsolatedFileExecutor())
    session = await _initialize(gateway, secret)

    written = await _call(
        gateway,
        secret,
        session,
        "file.write",
        {"path": "stratum/probe.txt", "content": "one\n", "overwrite": True},
    )
    assert written["ok"] is True, written
    assert not canonical_probe.exists()

    read = await _call(gateway, secret, session, "file.read", {"path": "stratum/probe.txt"})
    assert read["ok"] is True, read
    tag = re.search(r"LINE#[0-9a-f]{8}", read["result"]["text"])
    assert tag is not None
    assert "one" in read["result"]["text"]

    patched = await _call(
        gateway,
        secret,
        session,
        "file.patch",
        {"path": "stratum/probe.txt", "start_tag": tag.group(0), "new_text": "two"},
    )
    assert patched["ok"] is True, patched
    assert not canonical_probe.exists()
    reread = await _call(gateway, secret, session, "file.read", {"path": "stratum/probe.txt"})
    assert "two" in reread["result"]["text"]

    stale = await _call(
        gateway,
        secret,
        session,
        "file.patch",
        {"path": "stratum/probe.txt", "start_tag": tag.group(0), "new_text": "stale"},
    )
    assert stale["ok"] is False


@pytest.mark.asyncio
async def test_structured_shell_selector_compat_cd_and_git_tools_use_nested_repo(
    tmp_path: Path,
) -> None:
    stratum = _git_repo(tmp_path, "stratum")
    _git_repo(tmp_path, "hevi")
    gateway, secret, _ = _gateway(tmp_path)
    session = await _initialize(gateway, secret)

    shell = await _call(
        gateway,
        secret,
        session,
        "shell.exec",
        {
            "path": "stratum",
            "command": f'{sys.executable} -c "import os; print(os.getcwd())"',
            "profile": "local_trusted",
            "wait": True,
        },
    )
    assert shell["ok"] is True, shell
    assert shell["result"]["resolved_repo_root"] == str(stratum.resolve())
    # read/execute targets the nested repo's own canonical tree, not the parent
    # and not a throwaway worktree.
    assert shell["result"]["cwd"] == str(stratum.resolve())

    compat = await _call(
        gateway,
        secret,
        session,
        "shell.exec",
        {
            "command": "cd stratum && git status --short",
            "profile": "local_trusted",
            "wait": True,
        },
    )
    assert compat["ok"] is True, compat
    assert compat["result"]["resolved_repo_root"] == str(stratum.resolve())

    for name in ("git.status", "git.diff", "git.log"):
        result = await _call(gateway, secret, session, name, {"path": "stratum"})
        assert result["ok"] is True, (name, result)
        assert result["result"]["repo_root"] == str(stratum.resolve())


@pytest.mark.asyncio
async def test_test_and_build_use_same_nested_resolution(tmp_path: Path) -> None:
    _git_repo(tmp_path, "stratum")
    hevi = _git_repo(tmp_path, "hevi")
    gateway, secret, _ = _gateway(tmp_path)
    session = await _initialize(gateway, secret)
    command = f"{sys.executable} -c \"print('probe')\""

    for name in ("test.run", "build.run"):
        result = await _call(
            gateway,
            secret,
            session,
            name,
            {"path": "hevi", "command": command, "profile": "local_trusted", "wait": True},
        )
        assert result["ok"] is True, (name, result)
        assert result["result"]["resolved_repo_root"] == str(hevi.resolve())
        assert "probe" in result["result"]["stdout_tail"]


@pytest.mark.asyncio
async def test_workspace_path_is_canonical_nested_repo_selector(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "hevi")
    gateway, secret, _ = _gateway(tmp_path)
    session = await _initialize(gateway, secret)

    status = await _call(
        gateway,
        secret,
        session,
        "git.status",
        {"workspace_path": "hevi"},
    )
    assert status["ok"] is True, status
    assert status["result"]["repo_root"] == str(repo.resolve())
    assert status["result"]["resolution"]["evidence"]["repo_discovery"] == ("nested_git_repository")

    schema = {item["name"]: item for item in RemoteToolAdapter().list_tools()}
    assert schema["git.status"]["inputSchema"]["properties"]["workspace_path"]["type"] == ("string")
