"""SR-005 — an explicit execution_target outranks an execution_id.

Authority order, identical in every seam:

    explicit execution_target
      > execution_id-derived target
      > session implicit target
      > default target

An execution_id still identifies execution context; it just may not *relocate*
a request the caller pinned. With no explicit target the established
execution_id behaviour must be preserved exactly (the two "no explicit target"
rows of the matrix), so those cases are asserted too.

Unit-level against the resolvers themselves: these are pure precedence rules and
do not need a gateway, which keeps the assertion unambiguous about *which* seam
is being tested.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from veya.remote.tool_adapter import RemoteToolAdapter

CANONICAL = "CANONICAL_WORKTREE"
SESSION = "CURRENT_SESSION_WORKTREE"


class _Binding:
    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root
        self.requested_realpath = repo_root


class _Session:
    def __init__(self, worktrees: dict[str, str]) -> None:
        self.worktrees: ClassVar[dict[str, str]] = {}
        self.worktrees = worktrees


class _Executions:
    """Stands in for ExecutionWorktreeRegistry."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def resolve(self, execution_id: str, repo_root: str) -> Any:
        if execution_id not in self.mapping:
            raise AssertionError(f"unknown execution_id {execution_id}")
        worktree = self.mapping[execution_id]

        class _B:
            worktree_path = worktree
            canonical_repo_root = repo_root

        return _B()


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "canonical"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    session_wt = tmp_path / "session_wt"
    session_wt.mkdir()
    (session_wt / ".git").write_text("gitdir: /nowhere/meta\n", encoding="utf-8")
    exec_wt = tmp_path / "exec_wt"
    exec_wt.mkdir()
    return repo, session_wt, exec_wt


def _adapter(exec_map: dict[str, str]) -> Any:
    adapter = RemoteToolAdapter.__new__(RemoteToolAdapter)
    adapter.execution_worktrees = _Executions(exec_map)
    return adapter


def test_base_dir_canonical_beats_execution_id(tree: tuple[Path, Path, Path]) -> None:
    repo, _session_wt, exec_wt = tree
    adapter = _adapter({"exec-1": str(exec_wt)})
    resolved = adapter._base_dir(
        _Session({}),
        str(repo),
        execution_id="exec-1",
        execution_target=CANONICAL,
    )
    assert Path(resolved).resolve() == repo.resolve(), resolved


def test_direct_workdir_canonical_beats_execution_id(tree: tuple[Path, Path, Path]) -> None:
    repo, _session_wt, exec_wt = tree
    adapter = _adapter({"exec-1": str(exec_wt)})
    resolved = adapter._direct_workdir(
        _Session({}),
        _Binding(str(repo)),
        execution_id="exec-1",
        execution_target=CANONICAL,
    )
    assert Path(resolved).resolve() == repo.resolve(), resolved


def test_direct_workdir_session_beats_execution_id(tree: tuple[Path, Path, Path]) -> None:
    repo, session_wt, exec_wt = tree
    adapter = _adapter({"exec-1": str(exec_wt)})
    resolved = adapter._direct_workdir(
        _Session({str(repo): str(session_wt)}),
        _Binding(str(repo)),
        execution_id="exec-1",
        execution_target=SESSION,
    )
    assert Path(resolved).resolve() == session_wt.resolve(), resolved


def test_direct_workdir_execution_id_only_is_preserved(tree: tuple[Path, Path, Path]) -> None:
    """No explicit target: the historical execution_id behaviour must survive."""

    repo, _session_wt, exec_wt = tree
    adapter = _adapter({"exec-1": str(exec_wt)})
    resolved = adapter._direct_workdir(_Session({}), _Binding(str(repo)), execution_id="exec-1")
    assert Path(resolved).resolve() == exec_wt.resolve(), resolved


def test_base_dir_execution_id_only_is_preserved(tree: tuple[Path, Path, Path]) -> None:
    repo, _session_wt, exec_wt = tree
    adapter = _adapter({"exec-1": str(exec_wt)})
    resolved = adapter._base_dir(_Session({}), str(repo), execution_id="exec-1")
    assert Path(resolved).resolve() == exec_wt.resolve(), resolved


def test_base_dir_no_target_no_execution_id_uses_default(tree: tuple[Path, Path, Path]) -> None:
    repo, _session_wt, _exec_wt = tree
    adapter = _adapter({})
    resolved = adapter._base_dir(_Session({}), str(repo))
    assert Path(resolved).resolve() == repo.resolve(), resolved


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
