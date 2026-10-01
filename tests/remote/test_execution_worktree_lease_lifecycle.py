"""D1 regression cover: execution worktree lease lifecycle and GC safety.

The worktree lifecycle is admit -> acquire -> run -> terminal -> release.  Two
defects motivated this file:

1. The terminal path in ``DurableJobManager`` only released a worktree when
   ``parent_execution_id is None``.  ``worker.dispatch`` children each own their
   own isolated worktree, so they never released: 585 leaked ``task-*``
   worktrees, 692MB of ``.git/worktrees``.
2. ``teardown_worktree`` guards only on the ``.veya/worktrees`` path prefix, so
   a path-based GC would also delete hand-made feature worktrees that happen to
   live in that directory (e.g. ``agy-remove-dangerous-bypass``).

Every test below asserts a real filesystem/git outcome.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.coding.worktree import WorktreeManager, teardown_worktree
from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionStatus


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Worktree Lease Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=_git(root, "rev-parse", "--git-common-dir"),
        worktree_path=None,
        worktree_repo_root=None,
    )


def _session(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="lease-session",
        principal="owner",
        token_id="lease-token",
        active_workspace=str(root),
        explicit_workspace=str(root),
    )


def _manager(tmp_path: Path) -> DurableJobManager:
    return DurableJobManager(ExecutionStore(tmp_path / "executions"))


async def _never_finishes(reporter: object) -> str:
    """Runner that keeps the execution non-terminal until the test ends it."""
    await asyncio.Event().wait()
    return "unreachable"


async def _execution_with_worktree(
    manager: DurableJobManager, root: Path, *, parent_execution_id: str | None
) -> tuple[str, str]:
    """Admit a durable execution that owns a real isolated worktree."""
    record = manager.submit(
        session=_session(root),
        tool="worker.dispatch",
        veya_tool="direct_pi",
        binding=_binding(root),
        runner=_never_finishes,
        execution_type="remote",
        worker_type="PI",
        task_id=f"lease-{uuid.uuid4().hex[:12]}",
        isolated_worktree=True,
        parent_execution_id=parent_execution_id,
    )
    assert record.worktree_path, "execution did not acquire a worktree"
    return record.execution_id, record.worktree_path


def _terminalize_without_release(
    manager: DurableJobManager, execution_id: str, status: str
) -> None:
    """Simulate a crash between "terminal persisted" and "worktree removed".

    Bypasses ``_finish`` so the leaked worktree survives for the GC to find,
    which is the only state ``reconcile_worktrees`` exists to repair.
    """
    record = manager._record_for_update(execution_id)
    record.status = status
    record.phase = status
    record.worktree_released_at = None
    manager._persist(record)


# ── terminal release on every terminal path ───────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        "TIMED_OUT",
    ],
)
@pytest.mark.asyncio
async def test_terminal_releases_worktree_for_child_execution(tmp_path: Path, status: str) -> None:
    """A CHILD execution must release its own worktree on every terminal path.

    This is the exact regression: the release hook was gated on
    ``parent_execution_id is None``, so children leaked a worktree each.
    """
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    parent_id = f"parent_{status.lower()}"
    child_id, worktree = await _execution_with_worktree(
        manager, root, parent_execution_id=parent_id
    )
    assert Path(worktree).is_dir()

    manager._finish(manager._record_for_update(child_id), status)

    assert not Path(worktree).exists(), f"child worktree leaked on {status}"
    released = manager._record_for_update(child_id).worktree_released_at
    assert released is not None, "release was not recorded durably"


async def test_terminal_retains_dirty_worktree(tmp_path: Path) -> None:
    """Fail-closed: an execution with uncommitted work keeps its worktree."""
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    (Path(worktree) / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")

    manager._finish(manager._record_for_update(exec_id), ExecutionStatus.COMPLETED)

    assert Path(worktree).is_dir(), "dirty worktree must not be discarded"
    record = manager._record_for_update(exec_id)
    assert record.worktree_released_at is None
    assert manager.metrics_snapshot()["worktrees_retained"] >= 1


async def test_terminal_respects_keep_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    record = manager.submit(
        session=_session(root),
        tool="worker.dispatch",
        veya_tool="direct_pi",
        binding=_binding(root),
        runner=_never_finishes,
        execution_type="remote",
        worker_type="PI",
        task_id="kept",
        isolated_worktree=True,
        keep_worktree=True,
    )
    assert record.worktree_path
    manager._finish(manager._record_for_update(record.execution_id), ExecutionStatus.COMPLETED)
    assert Path(record.worktree_path).is_dir(), "keep_worktree must be honoured"


# ── GC safety ──────────────────────────────────────────────────────────


async def test_gc_never_deletes_a_user_worktree(tmp_path: Path) -> None:
    """A named feature worktree under .veya/worktrees is user state.

    ``agy-remove-dangerous-bypass`` was deleted by a prefix-only GC before this
    guard existed.  Only ``task-*`` execution worktrees are reclaimable.
    """
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)

    user_worktree = root / ".veya" / "worktrees" / "feature-user-branch"
    user_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", "feature-user-branch", str(user_worktree), "HEAD")

    assert DurableJobManager._is_reclaimable_worktree(str(user_worktree)) is False
    assert DurableJobManager._is_reclaimable_worktree(str(root)) is False
    assert DurableJobManager._is_reclaimable_worktree(worktree) is True

    _terminalize_without_release(manager, exec_id, ExecutionStatus.COMPLETED)

    report = manager.reconcile_worktrees()
    assert report["released"] == 1, report
    assert report["errors"] == []
    assert user_worktree.is_dir(), "GC deleted a user worktree"
    assert not Path(worktree).exists(), "GC did not reclaim the execution worktree"
    listed = _git(root, "worktree", "list", "--porcelain").count("worktree ")
    assert listed == 2, f"expected main + user worktree only, got {listed}"


async def test_gc_never_deletes_an_active_execution_worktree(tmp_path: Path) -> None:
    """A non-terminal execution's worktree is live state, even after a crash."""
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    assert not manager._record_for_update(exec_id).is_terminal

    report = manager.reconcile_worktrees()

    assert Path(worktree).is_dir(), "GC deleted an active execution worktree"
    assert any(item.get("reason") == "NOT_TERMINAL" for item in report["retained"])


async def test_startup_reconciliation_reclaims_interrupted_release(tmp_path: Path) -> None:
    """A gateway that died between persist and release finishes the job."""
    root = _repo(tmp_path)
    store = ExecutionStore(tmp_path / "executions")
    first = DurableJobManager(store)
    exec_id, worktree = await _execution_with_worktree(first, root, parent_execution_id=None)
    _terminalize_without_release(first, exec_id, ExecutionStatus.COMPLETED)
    assert Path(worktree).is_dir()

    # A fresh manager replays the same durable state, as a restart would.
    restarted = DurableJobManager(store)
    report = restarted.reconcile_worktrees()

    assert report["released"] == 1
    assert not Path(worktree).exists(), "startup reconciliation did not reclaim"


async def test_gc_is_idempotent(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    _terminalize_without_release(manager, exec_id, ExecutionStatus.COMPLETED)

    first = manager.reconcile_worktrees()
    second = manager.reconcile_worktrees()

    assert first["released"] == 1
    assert second["released"] == 0, "second GC pass re-released the same worktree"
    assert second["errors"] == []


async def test_gc_dry_run_reclaims_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    _terminalize_without_release(manager, exec_id, ExecutionStatus.FAILED)

    report = manager.reconcile_worktrees(dry_run=True)

    assert report["dry_run"] is True
    assert report["released"] == 0
    assert Path(worktree).is_dir(), "dry run mutated the worktree set"
    assert any(item.get("reason") == "DRY_RUN" for item in report["retained"])


async def test_metrics_count_releases_and_retains(tmp_path: Path) -> None:
    """D5: leak counters must be observable, not inferred."""
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    clean_id, clean_wt = await _execution_with_worktree(manager, root, parent_execution_id=None)
    manager._finish(manager._record_for_update(clean_id), ExecutionStatus.COMPLETED)
    dirty_id, dirty_wt = await _execution_with_worktree(manager, root, parent_execution_id=None)
    (Path(dirty_wt) / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    manager._finish(manager._record_for_update(dirty_id), ExecutionStatus.FAILED)

    snapshot = manager.metrics_snapshot()
    assert not Path(clean_wt).exists()
    assert Path(dirty_wt).is_dir()
    assert snapshot.get("worktrees_released", 0) >= 1, snapshot
    assert snapshot.get("worktrees_retained", 0) >= 1, snapshot
