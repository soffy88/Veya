from __future__ import annotations

import time
from pathlib import Path

import pytest

from veya.remote.action_gateway import ActionCategory, ActionGateway
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.workspace_policy import WorkspacePolicy


def _session(root: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="p0-cd-session",
        token_id="p0-cd-token",
        principal="p0-cd",
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=True,
            service_control=True,
        ),
        active_workspace=str(root),
        workspaces=(str(root),),
        created_at=now,
        expires_at=now + 3600,
    )


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "git rev-parse --show-toplevel",
        "git status",
        "git diff",
        "rg pattern .",
        "grep pattern file.txt",
        "find . -maxdepth 1",
        "cat README.md",
        "pytest -q",
        "python -m compileall .",
    ],
)
def test_read_only_and_project_tools_require_zero_approval(tmp_path: Path, command: str) -> None:
    allowed, error, _, classification = ActionGateway().check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert allowed is True
    assert error is None
    assert classification.category == ActionCategory.AUTO_OPEN


@pytest.mark.parametrize(
    "operation,args",
    [
        ("file.write", {"path": "new.txt"}),
        ("file.patch", {"path": "new.txt"}),
        ("file.delete", {"path": "new.txt"}),
    ],
)
def test_project_mutation_is_auto_open(
    tmp_path: Path, operation: str, args: dict[str, str]
) -> None:
    allowed, error, _, classification = ActionGateway().check_action(
        operation, args, _session(tmp_path), str(tmp_path)
    )
    assert allowed is True
    assert error is None
    assert classification.category == ActionCategory.AUTO_OPEN


@pytest.mark.parametrize(
    "command",
    [
        "git add file.txt",
        "git commit -m change",
        "git branch feature",
        "git merge feature",
        "git rebase main",
        "git worktree add ../probe feature",
    ],
)
def test_project_git_and_worktree_operations_are_auto_open(tmp_path: Path, command: str) -> None:
    allowed, error, _, classification = ActionGateway().check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert allowed is True
    assert error is None
    assert classification.category == ActionCategory.AUTO_OPEN


def test_project_git_metadata_is_writable_without_destructive_capability(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    policy = WorkspacePolicy(tmp_path, _session(tmp_path).permissions)
    assert policy.resolve(".git/refs/heads/probe", must_exist=None, for_write=True)
    assert policy.resolve(".git/worktrees/probe/locked", must_exist=None, for_write=True)


@pytest.mark.parametrize(
    "command",
    [
        "sudo apt install curl",
        "systemctl restart ssh.service",
        "git push --force-with-lease origin main",
    ],
)
def test_host_and_irreversible_operations_require_approval(tmp_path: Path, command: str) -> None:
    allowed, error, _, classification = ActionGateway().check_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert allowed is False
    assert str(error) in {"APPROVAL_REQUIRED", "INVALID_APPROVAL"}
    assert classification.category == ActionCategory.REQUIRE_APPROVAL
