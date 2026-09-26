"""P4-A production control-plane invariants."""

import asyncio

import pytest

from runtime.execution.durable import DurableExecutionRepository
from runtime.execution.production_hardening import (
    AdmissionRejected,
    BudgetExhausted,
    GoalRunNotRunnable,
    HardeningLimits,
    ProductionControlPlane,
)


@pytest.mark.asyncio
async def test_admission_backpressure_lifecycle_and_recovery(tmp_path):
    control = ProductionControlPlane(
        HardeningLimits(max_active_user=1, max_active_bot=1, max_queue=2),
        state_path=tmp_path / "control.json",
    )
    first = await control.admit("user-a", "bot-a", "goal-a")
    second = await control.admit("user-a", "bot-a", "goal-b")
    third = await control.admit("user-a", "bot-a", "goal-c")
    assert first.status == "active"
    assert second.status == third.status == "queued"
    with pytest.raises(AdmissionRejected):
        await control.admit("user-a", "bot-a", "goal-d")

    await control.pause("goal-a")
    assert control.is_paused("goal-a")
    with pytest.raises(GoalRunNotRunnable):
        control.ensure_runnable("goal-a")
    await control.resume("goal-a")
    assert not control.is_paused("goal-a")
    await control.release(first.lease_id)
    assert control._leases[second.lease_id].status == "active"

    recovered = ProductionControlPlane.recover(tmp_path / "control.json")
    assert recovered._leases[second.lease_id].goal_run_id == "goal-b"
    assert recovered._leases[second.lease_id].status == "active"
    assert list(recovered._queue) == [third.lease_id]
    assert "goal-b" in recovered._cancel_events

    await recovered.cancel("goal-b")
    assert recovered.cancellation_event("goal-b").is_set()
    assert recovered._leases[second.lease_id].status == "cancelled"
    with pytest.raises(GoalRunNotRunnable):
        recovered.ensure_runnable("goal-b")

    drain_task = asyncio.create_task(recovered.drain(timeout_s=0.5))
    await asyncio.sleep(0.02)
    await recovered.release(third.lease_id)
    assert await drain_task


@pytest.mark.asyncio
async def test_overload_emergency_stop_and_no_lost_queued_work(tmp_path):
    control = ProductionControlPlane(
        HardeningLimits(max_active_user=1, max_active_bot=1, max_queue=1),
        state_path=tmp_path / "control.json",
    )
    active = await control.admit("u", "a", "g1")
    queued = await control.admit("u", "a", "g2")
    assert queued.status == "queued"
    with pytest.raises(AdmissionRejected):
        await control.admit("u", "a", "g3")
    assert queued.lease_id in control._queue

    await control.emergency_stop()
    assert control.cancellation_event("g1").is_set()
    assert control._leases[active.lease_id].status == "cancelled"
    assert control._leases[queued.lease_id].status == "cancelled"
    assert not control._queue


def test_budgets_block_without_success_and_audit_is_complete(tmp_path):
    control = ProductionControlPlane(
        HardeningLimits(max_tokens=5, max_tool_calls=1, max_provider_calls=1, max_wall_s=10),
        state_path=tmp_path / "control.json",
    )
    control.charge("bot-a", "goal-a", tokens=5, tool_calls=1, provider_calls=1)
    with pytest.raises(BudgetExhausted, match="token_budget"):
        control.charge("bot-a", "goal-a", tokens=1)

    time_control = ProductionControlPlane(HardeningLimits(max_wall_s=0))
    with pytest.raises(BudgetExhausted, match="time_budget"):
        time_control.charge("bot-a", "goal-time", tokens=0)

    for field, limits in (
        ("tool", HardeningLimits(max_tool_calls=0)),
        ("provider", HardeningLimits(max_provider_calls=0)),
    ):
        budget_control = ProductionControlPlane(limits)
        with pytest.raises(BudgetExhausted, match=f"{field}_budget"):
            budget_control.charge("bot-a", f"goal-{field}", **{f"{field}_calls": 1})

    control.record_execution(
        "bot-a",
        "goal-a",
        actor="master",
        action="tool.call",
        provider="provider-a",
        side_effect="ledger:op-1",
        approval="approved",
        verdict="PASS",
        restart="restored",
        cancel="none",
    )
    required = {
        "bot_id",
        "goal_run_id",
        "actor",
        "action",
        "provider",
        "side_effect",
        "approval",
        "verdict",
        "restart",
        "cancel",
    }
    assert required <= control.audit_records[-1].keys()
    assert control.audit_records[-1]["goal_run_id"] == "goal-a"

    control.record_failure("bot-a", "goal-a", "provider failed")
    control.record_failure("bot-b", "goal-b", "tool failed")
    assert control.failures_for("bot-a") != control.failures_for("bot-b")
    assert control.failures_for("bot-b")[0]["goal_run_id"] == "goal-b"


@pytest.mark.asyncio
async def test_existing_side_effect_ledger_deduplicates_after_control_checkpoint(tmp_path):
    repository = DurableExecutionRepository(sqlite_path=tmp_path / "runtime.sqlite3")
    await repository.connect()
    from runtime.execution.side_effects import SideEffectLedger

    ledger = SideEffectLedger(repository)
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        return {"ok": True}

    kwargs = {
        "goal_run_id": "goal-ledger",
        "work_item_id": "work-ledger",
        "operation_key": "veya:goal-ledger:work-ledger:publish:1",
        "operation_type": "publish",
        "target_ref": "artifact:1",
        "request": {"artifact": "1"},
        "provider": provider,
        "capability": "idempotency_key",
        "bot_id": "bot-a",
    }
    assert await ledger.execute(**kwargs) == {"ok": True}
    assert await ledger.execute(**kwargs) == {"ok": True}
    assert calls == 1
    await repository.close()
