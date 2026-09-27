import pytest

from runtime.execution.durable import DurableExecutionRepository
from runtime.execution.side_effects import SideEffectLedger
from server.goal_run.canonical_worker import CanonicalWorkerAdapter


def test_request_fingerprint_is_canonical():
    adapter = CanonicalWorkerAdapter(task_id="t1", objective="o1")
    adapter.goal_run_id = "g1"

    args1 = {"a": 1, "b": {"c": 2, "d": [1, 2]}}
    args2 = {"b": {"d": [1, 2], "c": 2}, "a": 1}

    req1 = adapter.request("my_tool", args1)
    req2 = adapter.request("my_tool", args2)

    assert req1.request_fingerprint == req2.request_fingerprint
    # Different occurrences
    assert req1.action_id != req2.action_id
    assert req1.idempotency_key != req2.idempotency_key


def test_identical_args_new_occurrence_executes_again():
    adapter = CanonicalWorkerAdapter(task_id="t1", objective="o1")
    adapter.goal_run_id = "g1"

    args = {"file": "hello.txt", "content": "world"}
    reqA = adapter.request("file.write", args)
    reqB = adapter.request("file.write", args)

    assert reqA.request_fingerprint == reqB.request_fingerprint
    assert reqA.action_id != reqB.action_id
    assert reqA.idempotency_key != reqB.idempotency_key


@pytest.mark.asyncio
async def test_action_instance_mutation_rejected(tmp_path):
    repo = DurableExecutionRepository(sqlite_path=f"{tmp_path}/db.sqlite")
    await repo.setup_schema()

    await repo.start_goal_run("g1", "bot1")
    await repo.create_work_item("w1", "g1", "task", "subtask1", payload={"intent": "test"})

    # Store initial execution with A/F1
    await repo.declare_side_effect(
        goal_run_id="g1",
        work_item_id="w1",
        operation_key="op1",
        operation_type="test",
        target_ref="target1",
        request={"a": 1},
        request_fingerprint="fingerprint1",
    )

    # Replay with mismatched fingerprint
    ledger = SideEffectLedger(repo)

    with pytest.raises(Exception, match="ACTION_INSTANCE_MUTATION_REJECTED"):
        await ledger.execute(
            goal_run_id="g1",
            work_item_id="w1",
            operation_key="op1",  # Same semantic occurrence
            operation_type="test",
            target_ref="target1",
            request={"a": 2},
            request_fingerprint="fingerprint2",  # Mismatched fingerprint
            provider=lambda: {"status": "completed"},
        )


@pytest.mark.asyncio
async def test_two_identical_concurrent_occurrences_are_distinct(tmp_path):
    adapter = CanonicalWorkerAdapter(task_id="t1", objective="o1")
    adapter.goal_run_id = "g1"

    reqA = adapter.request("tool", {"arg": 1})
    reqB = adapter.request("tool", {"arg": 1})

    # They should have completely distinct idempotency keys
    assert reqA.idempotency_key != reqB.idempotency_key
    assert reqA.action_id != reqB.action_id


@pytest.mark.asyncio
async def test_recovery_reuses_action_instance_id():
    adapter = CanonicalWorkerAdapter(task_id="t1", objective="o1")
    adapter.goal_run_id = "g1"

    tool_call_id = "call_12345"

    # Initial execution
    req1 = adapter.request("tool", {"arg": 1}, tool_call_id=tool_call_id)

    # Recovery replay (MasterAgent re-yields the exact same tool_call_id)
    req2 = adapter.request("tool", {"arg": 1}, tool_call_id=tool_call_id)

    assert req1.action_id == req2.action_id
    assert req1.idempotency_key == req2.idempotency_key
