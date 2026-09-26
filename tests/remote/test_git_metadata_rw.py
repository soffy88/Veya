from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.coding.command_runner import CommandRunner


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_linked_worktree_git_common_metadata_is_writable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "execution"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Veya Test")
    _git(repo, "config", "user.email", "veya@example.test")
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree), "main")

    (worktree / "file.txt").write_text("one\ntwo\n", encoding="utf-8")
    runner = CommandRunner(worktree)
    for command in (
        ["git", "add", "file.txt"],
        ["git", "commit", "-qm", "change"],
        ["git", "branch", "task"],
        ["git", "tag", "task-v1"],
        ["git", "status", "--short"],
    ):
        result = runner.run(command)
        assert result.status == "passed", (command, result.to_dict())

    common = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"], text=True
    ).strip()
    assert (Path(common) / "objects").is_dir()
    assert (Path(common) / "worktrees").is_dir()
