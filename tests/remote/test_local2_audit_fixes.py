"""Local2 audit 2026-10-12: bare shell builtins and new-file promotion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.command_runner import parse_command
from runtime.coding.worktree import WorktreeManager


@pytest.mark.parametrize(
    "command",
    ["exit 3", "cd sub", "export A=1", "source /dev/null", ". ./env.sh", "type ls", "umask"],
)
def test_bare_builtin_runs_through_the_shell(command: str) -> None:
    assert parse_command(command) == ["/bin/bash", "-lc", command]


@pytest.mark.parametrize("command", ["foo=1 printenv foo", "A_b=2 env"])
def test_lowercase_env_prefix_runs_through_the_shell(command: str) -> None:
    assert parse_command(command)[:2] == ["/bin/bash", "-lc"]


@pytest.mark.parametrize(
    ("command", "argv"),
    [("python3 -V", ["python3", "-V"]), ("true", ["true"]), ("echo cd", ["echo", "cd"])],
)
def test_real_executables_stay_direct_argv(command: str, argv: list[str]) -> None:
    assert parse_command(command) == argv


def test_bare_exit_reports_its_exit_code() -> None:
    proc = subprocess.run(parse_command("exit 3"), check=False)
    assert proc.returncode == 3


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def test_promotion_of_a_new_file_reports_it_landed(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "calc.py").write_text("X = 1\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "init"],
    ):
        _git(repo, *cmd)
    manager = WorktreeManager(repo)
    worktree = Path(manager.create("task_newfile", "newfile").path)
    (worktree / "util.py").write_text("Y = 2\n", encoding="utf-8")
    manager.stage(worktree, paths=["util.py"])
    sha = manager.commit(worktree, "add util", expect_paths=["util.py"])["commit_sha"]
    verification = manager.verify(worktree, sha, expect_paths=["util.py"])
    result = manager.promote(worktree, sha, verification, expected_paths=["util.py"])
    assert (repo / "util.py").read_text(encoding="utf-8") == "Y = 2\n"
    assert result["landed_files"] == ["util.py"]
    assert result["verified"] is True


def test_approval_request_schema_declares_risk_class_enum() -> None:
    from veya.remote.models import RiskClass
    from veya.remote.tool_adapter import BINDING_INDEX

    prop = BINDING_INDEX["approval.request"].schema["properties"]["risk_class"]
    assert prop["type"] == "string"
    assert prop["enum"] == [r.value for r in RiskClass]
    assert prop["enum"] == ["P1_PRIVILEGED_HOST", "P2_ROOT_MUTATION", "P3_CRITICAL_HOST"]
