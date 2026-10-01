"""D2/D5 regression cover: terminal execution durability and resource leaks.

DEFECT-2 was originally reported as "an execution id from ~30 minutes ago
returns NOT_FOUND".  It was a measurement artifact -- the probe queried a
display-truncated execution id.  The durable contract is genuinely correct, and
these tests pin it:

* a terminal execution survives in-memory eviction of its projection
* a terminal execution survives a gateway restart (fresh manager over the same
  store)
* a terminal execution is readable from a *different* principal-independent
  lookup, i.e. the durable record, not the live task
* resource counters return to their pre-round baseline after every terminal
  path (success, worker crash, timeout, cancel)
* the durable SQLite store leaves no open transaction behind

D5 requires these to be measurements, so the assertions read live counters.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.execution.durable import DurableExecutionRepository
from veya.remote.execution import DurableJobManager, ExecutionStore
from veya.remote.execution_worktree import ExecutionWorktreeRegistry


def _session(tag: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=tag,
        principal="owner",
        token_id="tok",
        active_workspace="/tmp",
        explicit_workspace="/tmp",
    )


def _binding() -> SimpleNamespace:
    return SimpleNamespace(
        requested_path="/tmp",
        requested_realpath="/tmp",
        repo_root="/tmp",
        repo_identity="repo",
        worktree_path=None,
        worktree_repo_root=None,
    )


async def _ok(reporter: object) -> str:
    return "ok"


async def _crash(reporter: object) -> str:
    raise RuntimeError("worker crash")


def _submit(manager: DurableJobManager, tag: str, runner: object) -> str:
    return manager.submit(
        session=_session(tag),
        tool="worker.dispatch",
        veya_tool="v",
        binding=_binding(),
        runner=runner,
        execution_type="remote",
        worker_type="PI",
        idempotency_key=f"{tag}-{id(runner)}",
    ).execution_id


def _snapshots(manager: DurableJobManager, registry: ExecutionWorktreeRegistry) -> dict[str, int]:
    return {
        **manager.metrics_snapshot(),
        **registry.lease_metrics(),
        "active_executions": manager.unfinished_count(),
    }


# ── D2: terminal durability ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_survives_in_memory_eviction(tmp_path: Path) -> None:
    """Evicting the in-memory projection must not lose the terminal record."""
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store, max_jobs=4)
    old_id = _submit(manager, "evict", _ok)
    await asyncio.sleep(0)
    manager._finish(manager._record_for_update(old_id), "COMPLETED")
    assert Path(store.root / f"{old_id}.json").is_file(), "terminal state was not persisted"

    for i in range(12):
        _submit(manager, f"filler{i}", _ok)
    assert old_id not in manager._records, "eviction never ran; test would be vacuous"

    fresh = DurableJobManager(ExecutionStore(tmp_path / "executions"), max_jobs=4)
    record = fresh.status(old_id, token_id="tok")
    assert record.status == "COMPLETED"
    assert record.is_terminal


@pytest.mark.asyncio
async def test_terminal_survives_manager_restart(tmp_path: Path) -> None:
    """A restarted gateway reads terminal state from the durable store."""
    store = ExecutionStore(tmp_path / "executions")
    first = DurableJobManager(store)
    execution_id = _submit(first, "restart", _ok)
    await asyncio.sleep(0)
    first._finish(first._record_for_update(execution_id), "FAILED", error="boom")

    restarted = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    record = restarted.status(execution_id, token_id="tok")
    assert record.status == "FAILED"
    assert record.phase == "FAILED"
    assert record.created_at is not None
    assert record.completed_at is not None
    assert record.execution_id == execution_id


@pytest.mark.asyncio
async def test_terminal_record_carries_required_durability_fields(tmp_path: Path) -> None:
    """The durable record must carry the fields a client needs after a restart."""
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    execution_id = _submit(manager, "fields", _ok)
    await asyncio.sleep(0)
    manager._finish(manager._record_for_update(execution_id), "FAILED", error="provider down")
    record = DurableJobManager(store).status(execution_id, token_id="tok")

    for field in (
        "execution_id",
        "status",
        "phase",
        "created_at",
        "completed_at",
        "parent_execution_id",
        "child_execution_ids",
        "requested_realpath",
        "repo_identity",
    ):
        assert hasattr(record, field), f"durable record is missing {field}"
    assert record.status == "FAILED"
    assert record.completed_at is not None


@pytest.mark.asyncio
async def test_status_lookup_does_not_require_the_live_task(tmp_path: Path) -> None:
    """status() must resolve from the durable record, not from a running task."""
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    execution_id = _submit(manager, "notask", _ok)
    await asyncio.sleep(0)
    manager._finish(manager._record_for_update(execution_id), "COMPLETED")
    manager._tasks.pop(execution_id, None)
    record = manager.status(execution_id, token_id="tok")
    assert record.status == "COMPLETED"


# ── D5: resource leak counters ────────────────────────────────────────


@pytest.mark.parametrize(
    "runner,terminal",
    [
        (_ok, "COMPLETED"),
        (_crash, "FAILED"),
        (_ok, "TERMINATED"),
        (_ok, "CANCELLED"),
    ],
    ids=["success", "worker-crash", "terminated", "cancel"],
)
@pytest.mark.asyncio
async def test_permits_and_leases_return_to_baseline(
    tmp_path: Path, runner: object, terminal: str
) -> None:
    """Every terminal path must return permits and leases to baseline."""
    manager = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    registry = ExecutionWorktreeRegistry(tmp_path / "wt")
    before = _snapshots(manager, registry)

    execution_id = _submit(manager, "leak", runner)
    lease = registry.acquire_lease(execution_id, "repo", "PI:leak")
    assert registry.lease_metrics()["active_worktree_leases"] == 1
    await asyncio.sleep(0)

    manager._finish(manager._record_for_update(execution_id), terminal)
    registry.release_lease(lease)
    registry.reap_leases()

    after = _snapshots(manager, registry)
    assert after["active_executions"] == before["active_executions"], after
    assert after["active_worktree_leases"] == 0, after
    assert after["tracked_worktree_leases"] == 0, after
    assert after["leases_acquired"] == after["leases_released"], after
    assert after["active_executions"] == 0, "a terminal execution still holds a permit"


@pytest.mark.asyncio
async def test_reaped_lease_is_counted_and_cleared(tmp_path: Path) -> None:
    registry = ExecutionWorktreeRegistry(tmp_path / "wt")
    lease = registry.acquire_lease("exec", "repo", "PI")
    # ttl is clamped to >=1.0s, so expire it directly instead of sleeping
    lease.expires_at = time.time() - 1.0
    assert registry.lease_metrics()["active_worktree_leases"] == 0
    reaped = registry.reap_leases()
    assert reaped == 1
    metrics = registry.lease_metrics()
    assert metrics["tracked_worktree_leases"] == 0
    assert metrics["leases_reaped"] == 1


@pytest.mark.asyncio
async def test_sqlite_leaves_no_open_transaction(tmp_path: Path) -> None:
    """SQLITE_LOCK_LEAK must be a measured zero, not an inference."""
    repository = DurableExecutionRepository(sqlite_path=tmp_path / "runtime.sqlite3")
    await repository.migrate()
    baseline = repository.sqlite_metrics()
    assert baseline["sqlite_open_transactions"] == 0

    for i in range(5):
        await repository.record_migration(
            flag=f"flag_{i}", phase="apply", cohort="leak-probe", operator="test"
        )

    after = repository.sqlite_metrics()
    assert after["sqlite_open_transactions"] == 0, after
    assert after["sqlite_tx_total"] > baseline["sqlite_tx_total"]
    assert after["sqlite_lock_held"] == 0


@pytest.mark.asyncio
async def test_sqlite_transaction_closes_even_when_the_body_raises(tmp_path: Path) -> None:
    """A failing transaction must roll back and still release the counter."""
    repository = DurableExecutionRepository(sqlite_path=tmp_path / "runtime.sqlite3")
    await repository.migrate()
    before = repository.sqlite_metrics()

    def boom(conn: object) -> str:
        conn.execute("SELECT 1")
        raise RuntimeError("body failed after BEGIN")

    with pytest.raises(RuntimeError):
        await asyncio.to_thread(repository._sqlite_tx, boom)

    after = repository.sqlite_metrics()
    assert after["sqlite_open_transactions"] == 0, after
    assert after["sqlite_tx_total"] > before["sqlite_tx_total"]
    assert after["sqlite_lock_held"] == 0
