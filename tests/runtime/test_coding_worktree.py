from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.workspace_detect import detect_workspace
from runtime.coding.worktree import WorktreeError, WorktreeManager, branch_name_for


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Coding Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def test_create_diff_list_and_discard_isolated_worktree(tmp_path: Path):
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    original_branch = _git(root, "branch", "--show-current")
    original_status = _git(root, "status", "--porcelain")

    record = manager.create("task-123", "Fix failing tests")
    worktree = Path(record.path)
    (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    (worktree / "new.py").write_text("print('new')\n", encoding="utf-8")
    diff = manager.diff("task-123")

    assert record.branch_name == branch_name_for("task-123", "Fix failing tests")
    assert record.path == str(root / ".veya" / "worktrees" / "task-task-123")
    assert diff["branch_name"] == record.branch_name
    assert "-print('base')" in str(diff["patch"])
    assert "print('fixed')" in str(diff["patch"])
    assert "new.py" in record.changed_files or "new.py" in str(diff["changed_files"])
    assert "new.py" in str(diff["patch"])
    assert [item.task_id for item in manager.list()] == ["task-123"]
    assert _git(root, "branch", "--show-current") == original_branch
    assert _git(root, "status", "--porcelain") == original_status

    with pytest.raises(WorktreeError, match="uncommitted changes"):
        manager.discard("task-123")
    manager.discard("task-123", force=True)

    assert not worktree.exists()
    assert _git(root, "show-ref", "--verify", f"refs/heads/{record.branch_name}")


def test_worktree_rejects_paths_outside_owned_directory(tmp_path: Path):
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    with pytest.raises(WorktreeError, match="below the workspace worktree directory"):
        manager.status(path=root / "app.py")
    with pytest.raises(WorktreeError, match="single safe path component"):
        manager.create("../escape", "bad")
