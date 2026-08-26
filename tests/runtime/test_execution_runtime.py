from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from runtime.execution.artifacts import ArtifactStore
from runtime.execution.cancellation import CancellationTree
from runtime.execution.checkpoint import ExecutionCheckpointStore
from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.fanin import fan_in
from runtime.execution.finalization import (
    FinalizationController,
    FinalizationObserver,
    calculate_finalization_reserve,
)
from runtime.execution.models import (
    ArtifactRef,
    Assertion,
    DelegateRequest,
    DelegateResult,
    Evidence,
    ExecutionCheckpoint,
    SpawnBudget,
)
from runtime.execution.scheduler import ContinuousReadyScheduler
from runtime.execution.spawn_guard import SpawnGuard, SpawnRejected


@pytest.mark.asyncio
async def test_spawn_guard_depth_parallel_budget_and_queue():
    guard = SpawnGuard(SpawnBudget(max_depth=2, max_parallel=1, max_tokens=10))
    with pytest.raises(SpawnRejected):
        await guard.pre_check(depth=2, estimated_tokens=1)

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first(_cancel):
        first_started.set()
        await release_first.wait()
        return "first"

    first_task = asyncio.create_task(guard.run("a", first, depth=0, estimated_tokens=4))
    await first_started.wait()
    second = asyncio.create_task(guard.run("b", lambda _cancel: asyncio.sleep(0), depth=0, estimated_tokens=4))
    await asyncio.sleep(0)
    assert guard.active_count == 1
    assert guard.queued_count == 1
    release_first.set()
    assert await first_task == "first"
    assert await second is None
    assert guard.active_count == 0
    assert guard.snapshot()["used_tokens"] == 8


@pytest.mark.asyncio
async def test_spawn_guard_reconciles_actual_usage():
    guard = SpawnGuard(SpawnBudget(max_parallel=1, max_tokens=20))

    async def child(_cancel):
        return {"prompt_tokens": 2, "completion_tokens": 5, "cost_usd": 0.25}

    result = await guard.run("actuals", child, depth=0, estimated_tokens=10, estimated_cost_usd=1.0)
    assert result["completion_tokens"] == 5
    snapshot = guard.snapshot()
    assert snapshot["used_tokens"] == 7
    assert snapshot["used_cost_usd"] == 0.25


@pytest.mark.asyncio
async def test_spawn_guard_raii_and_cancel_before_acquire():
    guard = SpawnGuard(SpawnBudget(max_parallel=1, max_tokens=100))
    hold = asyncio.Event()
    entered = asyncio.Event()

    async def blocker(_cancel):
        entered.set()
        await hold.wait()

    running = asyncio.create_task(guard.run("running", blocker, depth=0, estimated_tokens=10))
    await entered.wait()
    queued = asyncio.create_task(guard.run("queued", lambda _cancel: asyncio.sleep(0), depth=0, estimated_tokens=10))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert guard.queued_count == 0
    hold.set()
    await running
    assert guard.active_count == 0


@dataclass
class _Item:
    id: str
    duration: float
    depends_on: list[str] = field(default_factory=list)
    parallel: bool = True


@pytest.mark.asyncio
async def test_continuous_scheduler_fills_slot_without_batch_barrier():
    # A and B occupy both slots. D depends only on fast B; C depends on slow A.
    items = [
        _Item("A", 0.12),
        _Item("B", 0.01),
        _Item("C", 0.01, depends_on=["A"]),
        _Item("D", 0.01, depends_on=["B"]),
    ]
    started: list[str] = []
    b_finished = asyncio.Event()

    async def execute(item: _Item):
        started.append(item.id)
        await asyncio.sleep(item.duration)
        if item.id == "B":
            b_finished.set()
        return item.id

    run = await ContinuousReadyScheduler(max_parallel=2).run(items, execute)
    assert run.state.completed == {"A", "B", "C", "D"}
    assert b_finished.is_set()
    # D must have been admitted while A was still occupying its original slot;
    # the scheduler therefore did not wait for an A/B batch barrier.
    assert started[:3] == ["A", "B", "D"]


@pytest.mark.asyncio
async def test_continuous_scheduler_non_parallel_is_exclusive():
    items = [_Item("A", 0.01, parallel=True), _Item("B", 0.01, parallel=False), _Item("C", 0.01, parallel=True)]
    active = 0
    peak = 0

    async def execute(item: _Item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(item.duration)
        active -= 1
        return item.id

    run = await ContinuousReadyScheduler(max_parallel=3).run(items, execute)
    assert run.state.completed == {"A", "B", "C"}
    assert peak == 1


def test_fanin_preserves_partial_evidence_and_deduplicates():
    evidence = Evidence(id="e1", kind="url", source="https://example.test", content="same", producer="a")
    failed = DelegateResult(
        delegate_id="a",
        status="failed",
        stop_reason="exception",
        summary="crashed after research",
        evidence=[evidence],
        assertions=[Assertion(id="a1", statement="claim", evidence_ids=["e1"], producer="a")],
        artifacts=[ArtifactRef(path="outputs/report.md", kind="file", producer="a", status="partial")],
    )
    duplicate = DelegateResult(
        delegate_id="b",
        status="complete",
        stop_reason="completed",
        evidence=[Evidence(id="e2", kind="url", source="https://example.test", content="same", producer="b")],
    )
    batch = fan_in([failed, duplicate])
    assert batch.failed_count == 1
    assert batch.complete_count == 1
    assert len(batch.evidence) == 1
    assert len(batch.artifacts) == 1


def test_unknown_stop_reason_is_partial():
    result = DelegateResult.from_mapping(
        "child-1", {"status": "success", "stop_kind": "new_future_reason", "final_answer": "work"}
    )
    assert result.status == "partial"
    assert result.stop_reason == "unknown"


def test_finalization_reserve_and_trigger():
    assert calculate_finalization_reserve(100, min_reserve_s=20, reserve_ratio=0.15, max_reserve_s=40) == 20
    controller = FinalizationController(100, min_reserve_s=20, reserve_ratio=0.15, max_reserve_s=40)
    assert controller.start(20)
    assert controller.started
    assert not controller.start(1)


@pytest.mark.asyncio
async def test_finalization_observer_transitions_context_once():
    controller = FinalizationController(100, min_reserve_s=20, reserve_ratio=0, max_reserve_s=40)
    context = {"remaining_wall_s": 20, "budget_near": True}
    assert await FinalizationObserver(controller).before_round(context)
    assert context["finalizing"] is True
    assert not await FinalizationObserver(controller).before_round(context)


def test_artifact_store_layout_and_output_contract(tmp_path):
    store = ArtifactStore(tmp_path, "task-1")
    store.ensure_layout()
    workspace_file = store.path("workspace/draft.txt")
    workspace_file.write_text("draft", encoding="utf-8")
    ref = store.register("workspace/draft.txt", status="partial")
    assert not store.is_final(ref)
    output_file = store.path("workspace/final.txt")
    output_file.write_text("final", encoding="utf-8")
    final_ref = store.publish(output_file, "report.txt", status="verified")
    assert store.is_final(final_ref)
    manifest_path = store.write_manifest()
    assert manifest_path.exists()
    with pytest.raises(ValueError):
        store.path("../../outside.txt")


@pytest.mark.asyncio
async def test_delegate_runtime_timeout_is_partial_and_releases_slot():
    guard = SpawnGuard(SpawnBudget(max_parallel=1, subagent_timeout_s=1))
    events: list[dict] = []
    runtime = DelegateRuntime(guard, on_event=events.append)

    async def too_slow(_cancel):
        await asyncio.sleep(0.05)
        return {"status": "success", "stop_kind": "completed", "final_answer": "late"}

    request = DelegateRequest(
        delegate_id="timeout-child",
        parent_task_id="root",
        parent_trace_id="trace",
        objective="timeout",
        depth=0,
        timeout_s=0.001,
    )
    result = await runtime.run(request, too_slow)
    assert result.status == "partial"
    assert result.stop_reason == "wall_deadline"
    assert guard.active_count == 0
    assert [event["type"] for event in events] == [
        "delegate.queued",
        "delegate.started",
        "delegate.partial",
    ]


def test_cancellation_tree_propagates_to_descendants():
    tree = CancellationTree()
    tree.register("root")
    tree.register("child", "root")
    tree.register("grandchild", "child")
    assert tree.cancel("root") == ["root", "child", "grandchild"]
    assert tree.is_cancelled("grandchild")


def test_execution_checkpoint_roundtrip(tmp_path):
    store = ExecutionCheckpointStore(tmp_path / "run")
    checkpoint = ExecutionCheckpoint(
        event_cursor="e-3",
        scheduler_snapshot={"running": ["A"]},
        running_delegate_ids=["A"],
        completed_task_ids=["B"],
        pending_task_ids=["C"],
        finalization_started=True,
    )
    store.write(checkpoint)
    restored = store.read()
    assert restored is not None
    assert restored.event_cursor == "e-3"
    assert restored.finalization_started is True
