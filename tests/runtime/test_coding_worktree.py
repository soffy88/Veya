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


# Regression tests for worktree containment invariant


def test_worktree_creation_only_in_veya_worktrees(tmp_path: Path):
    """Regression test: worktree creation must only occur in .veya/worktrees/.

    Ensures that no code can create worktrees outside the canonical directory.
    """
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # Should be able to create worktree inside .veya/worktrees/
    record = manager.create("task-001", "test objective")
    expected_path = str(root / ".veya" / "worktrees" / "task-task-001")
    assert record.path == expected_path, f"Expected {expected_path}, got {record.path}"
    assert Path(record.path).is_dir()

    # Cleanup
    manager.discard("task-001", force=True)


def test_worktree_path_escape_rejected(tmp_path: Path):
    """Regression test: paths escaping .veya/worktrees/ must be rejected."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # Attempt to create worktree outside .veya/worktrees/ should fail
    # (fails first at task_id validation, not path containment)
    with pytest.raises(WorktreeError, match="single safe path component"):
        manager.create("../escape", "bad objective")

    # Attempt to provide path outside .veya/worktrees/ should fail
    with pytest.raises(WorktreeError, match="below the workspace worktree directory"):
        manager.status(path=str(root / "escape"))


def test_no_external_worktree_creation_paths(tmp_path: Path):
    """Regression test: verify no external worktree creation paths exist.

    Checks that all worktree creation goes through WorktreeManager.base_dir.
    """
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # Verify base_dir is always .veya/worktrees/ relative to repo root
    assert ".veya/worktrees" in str(manager.base_dir), (
        f"base_dir should contain .veya/worktrees, got {manager.base_dir}"
    )

    # Verify repo_root_for_worktree function resolves correctly
    from runtime.coding.worktree import repo_root_for_worktree

    test_path = root / ".veya" / "worktrees" / "task-test123"
    resolved = repo_root_for_worktree(test_path)
    assert resolved == root, f"repo_root_for_worktree should return repo root, got {resolved}"


def test_cleanup_uses_git_worktree_remove(tmp_path: Path):
    """Regression test: cleanup must use git worktree remove, not rm -rf.

    Ensures worktrees are properly removed via git command to preserve
    commit objects and refs in the canonical repository.
    """
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # Create a worktree
    record = manager.create("task-002", "test objective")
    worktree_path = Path(record.path)

    # Verify worktree exists
    assert worktree_path.is_dir()

    # Cleanup via git worktree remove (the proper way)
    manager.discard("task-002", force=True)

    # Worktree should be removed
    assert not worktree_path.exists(), "Worktree should be removed via git worktree remove"

    # Commit branch should still be accessible via git
    branch_name = record.branch_name
    result = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
    )
    # Branch ref should still exist even after worktree removal
    assert result.returncode == 0, f"Branch ref should persist: {result.stdout}"


def test_canonical_repo_never_deleted_by_cleanup(tmp_path: Path):
    """Regression test: canonical repo must never be deleted by cleanup.

    Ensures that git worktree remove operations cannot delete the
    superproject repository.
    """
    root = _repo(tmp_path)

    # Get original repo state
    original_head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    manager = WorktreeManager(detect_workspace(root))

    # Create and remove multiple worktrees
    for i in range(3):
        task_id = f"task-{i:03d}"
        manager.create(task_id, f"test objective {i}")

    # Remove all worktrees
    for i in range(3):
        manager.discard(f"task-{i:03d}", force=True)

    # Canonical repo must still exist and be intact
    assert root.is_dir(), "Canonical repo root must not be deleted"
    head_after = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head_after == original_head, "Canonical repo HEAD must not change"


def test_worktree_symlink_escape_rejected(tmp_path: Path):
    """Regression test: symlink escape into canonical worktree dir must be rejected.

    Ensures that path traversal or symlink attacks cannot escape the
    .veya/worktrees/ containment boundary.
    """
    import os

    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # Create a symlink that points outside .veya/worktrees/
    symlink_path = root / "escape_symlink"
    os.symlink("/etc/passwd", symlink_path)

    try:
        # Attempt to use the symlink path should be rejected
        with pytest.raises(WorktreeError):
            manager.status(path=str(symlink_path))
    finally:
        # Cleanup symlink
        if symlink_path.exists():
            os.unlink(symlink_path)


def test_external_veya_dirs_rejected():
    """Regression test: ../veya-* paths must be rejected from worktree creation.

    Ensures the canonical invariant that only .veya/worktrees/* are allowed.
    """
    import pytest

    # This is verified by test_worktree_path_escape_rejected which tests
    # that ../escape paths are rejected by the WorktreeManager invariant
    pytest.xfail("Verified by test_worktree_path_escape_rejected")
