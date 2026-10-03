"""Restart convergence for resting BLOCKED projections.

Regression: a persisted BLOCKED child (e.g. an admission refusal such as
PINNED_EXECUTOR_NOT_WRITE_QUALIFIED) crashed gateway startup recovery —
``sweep_blocked_records`` converges BLOCKED to FAILED after its TTL, but the
transition table had no BLOCKED -> FAILED edge, so ``_finish`` raised
``IllegalTransition`` and every subsequent request failed with
"remote startup recovery failed".
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from veya.remote.execution import (
    DurableJobManager,
    ExecutionStatus,
    ExecutionStore,
    IllegalTransition,
    assert_legal_transition,
)


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


def _session(session_id: str, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        principal="owner",
        token_id="tok",
        active_workspace=str(root),
        explicit_workspace=str(root),
    )


def test_blocked_converges_to_failed_but_nothing_else() -> None:
    # The TTL sweeper's documented edge.
    assert_legal_transition("BLOCKED", "FAILED")
    # Previously pinned refusals stay put.
    with pytest.raises(IllegalTransition):
        assert_legal_transition("BLOCKED", "REJECTED")
    with pytest.raises(IllegalTransition):
        assert_legal_transition("FAILED", "COMPLETED")
    with pytest.raises(IllegalTransition):
        assert_legal_transition("COMPLETED", "FAILED")


async def test_restart_sweep_converges_stale_blocked_child(tmp_path: Path) -> None:
    """A restart over a stale BLOCKED child converges it, never crashes."""

    async def _never_runs(reporter: object) -> str:  # pragma: no cover
        raise AssertionError("blocked child must never run")

    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    owner = _session("owner-session", tmp_path)
    parent = manager.create_parent(
        session=owner,
        tool="worker.dispatch",
        binding=_binding(tmp_path),
        failure_mode="collect_all",
    )
    child = manager.submit(
        session=owner,
        tool="worker.dispatch",
        veya_tool="direct_opencode",
        binding=_binding(tmp_path),
        runner=_never_runs,
        parent_execution_id=parent.execution_id,
        worker_type="OPENCODE",
    )
    manager.attach_child(parent.execution_id, child.execution_id)
    # Simulate the persisted admission refusal, aged past the sweep TTL.
    child.status = str(ExecutionStatus.BLOCKED)
    child.phase = str(ExecutionStatus.BLOCKED)
    child.lifecycle_state = str(ExecutionStatus.BLOCKED)
    child.failure_class = "execution_blocked"
    child.error = "PINNED_EXECUTOR_NOT_WRITE_QUALIFIED"
    child.failure_detail = "PINNED_EXECUTOR_NOT_WRITE_QUALIFIED"
    child.completed_at = time.time() - 3600
    child.worker_alive = False
    manager._tasks.pop(child.execution_id, None)
    manager._persist(child)

    restarted = DurableJobManager(store)
    report = restarted.reconcile_unfinished()
    converged = restarted.lookup(child.execution_id)
    assert converged.status == ExecutionStatus.FAILED
    assert "PINNED_EXECUTOR_NOT_WRITE_QUALIFIED" in str(
        converged.failure_detail or converged.message or converged.error
    )
    assert report["blocked_swept"] >= 1
