from __future__ import annotations

import asyncio

import pytest

from server.coordinator_master import MasterCoordinator
from server.goal_run.action_protocol import CanonicalActionRequest, CanonicalActionResult
from server.goal_run.canonical_worker import CanonicalWorkerAdapter, MasterAgentActionAdapter
from server.goal_run.models import GoalRunState
from server.goal_run.supervisor_restart import (
    claim_exclusive_session,
    restart_and_resume_canonical_action,
)
from veya.platform import load


def test_canonical_action_round_trip_preserves_execution_context() -> None:
    request = CanonicalActionRequest(
        action_id="action-1",
        goal_run_id="goal-1",
        task_id="task-1",
        tool="run_in_sandbox",
        arguments={"command": "printf ok"},
        capability="computer",
        computer_ref="computer-1",
        context_ref="checkpoint-1",
        approval={"required": True},
        idempotency_key="goal-1:action-1",
        evidence_refs=("failure-0",),
    )
    restored = CanonicalActionRequest.from_dict(request.to_dict())
    assert restored == request
    assert restored.idempotency_key == "goal-1:action-1"

    result = CanonicalActionResult(
        action_id=request.action_id,
        status="failed",
        attempted=True,
        executed=False,
        result="physical failure",
        failure_evidence=({"stage": "physical_execution", "error": "permission denied"},),
        evidence_refs=("failure-0",),
    )
    assert CanonicalActionResult.from_dict(result.to_dict()) == result


def test_protocol_has_no_lifecycle_or_acceptance_authority() -> None:
    request_fields = set(CanonicalActionRequest.__dataclass_fields__)
    result_fields = set(CanonicalActionResult.__dataclass_fields__)
    assert not request_fields & {"goal_status", "retry", "verdict", "replan"}
    assert not result_fields & {"goal_status", "retry", "verdict", "replan"}


@pytest.mark.parametrize("missing", ["action_id", "goal_run_id", "task_id", "tool"])
def test_request_requires_authority_identity(missing: str) -> None:
    values = {
        "action_id": "a",
        "goal_run_id": "g",
        "task_id": "t",
        "tool": "tool",
    }
    values[missing] = ""
    with pytest.raises(ValueError):
        CanonicalActionRequest(**values)


@pytest.mark.asyncio
async def test_master_decision_executes_once_in_bound_goalrun_and_returns_result(tmp_path) -> None:
    state = GoalRunState(goal_id="goal-1", goal_text="do one action")
    worker = CanonicalWorkerAdapter(task_id="task-1", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    worker.computer_id = "computer-1"
    calls: list[str] = []

    async def gateway(request: CanonicalActionRequest) -> dict:
        calls.append(request.action_id)
        assert request.goal_run_id == state.goal_id
        assert request.computer_ref == worker.computer_id
        return {"status": "completed", "executed": True, "result": "ok"}

    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-1",
        computer_ref=worker.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker.execute_canonical_action(
            state, request, gateway_executor=gateway
        ),
    )
    result = await master.execute("physical_tool", {"value": 1})
    assert result.status == "completed"
    assert result.result == "ok"
    assert calls == [result.action_id]
    assert state.budget["last_canonical_action"]["request"]["goal_run_id"] == state.goal_id


@pytest.mark.asyncio
async def test_physical_failure_roundtrips_as_evidence_without_retry(tmp_path) -> None:
    state = GoalRunState(goal_id="goal-fail", goal_text="fail once")
    worker = CanonicalWorkerAdapter(task_id="task-fail", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    worker.computer_id = "computer-fail"

    async def gateway(_request: CanonicalActionRequest) -> None:
        raise RuntimeError("real physical failure")

    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-fail",
        computer_ref=worker.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker.execute_canonical_action(
            state, request, gateway_executor=gateway
        ),
    )
    result = await master.execute("physical_tool", {})
    assert result.status == "failed"
    assert result.failure_evidence[0]["stage"] == "physical_execution"


@pytest.mark.asyncio
async def test_approval_and_restart_keep_same_action_identity_and_computer(tmp_path) -> None:
    state = GoalRunState(goal_id="goal-approval", goal_text="approve one action")
    approval_started = asyncio.Event()
    approval_release = asyncio.Event()

    async def approve(_request):
        approval_started.set()
        await approval_release.wait()
        return True

    worker = CanonicalWorkerAdapter(
        task_id="task-approval",
        objective=state.goal_text,
        approval_resolver=approve,
        policy_hook=lambda request: load("obase").ActionDecision(
            verdict="REQUIRE_APPROVAL",
            reason="canonical approval test",
            request_id=request.request_id,
        ),
    )
    await worker.before_execution(state, str(tmp_path))
    worker.computer_id = "computer-approval"
    seen: list[str] = []

    async def gateway(request: CanonicalActionRequest) -> dict:
        seen.append(request.action_id)
        return {"status": "completed", "executed": True, "result": "approved"}

    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-approval",
        computer_ref=worker.computer_id,
        approval={"side_effect": "remote", "effect_capability": "idempotency_key"},
        executor=lambda request: worker.execute_canonical_action(
            state, request, gateway_executor=gateway
        ),
    )
    pending = asyncio.create_task(master.execute("write", {"path": "artifact"}))
    await approval_started.wait()
    assert not pending.done()
    assert state.runtime_checkpoint["canonical_action"]["status"] == "pending"
    assert state.runtime_checkpoint["canonical_action"]["idempotency_key"] == (
        state.goal_id + ":" + master.request("write", {"path": "artifact"}).action_id
    )
    approval_release.set()
    first = await pending
    assert first.status == "completed"
    second = await master.execute("write", {"path": "artifact"})
    assert second.status == "completed"
    assert first.action_id == second.action_id
    assert len(seen) == 1
    assert master.computer_ref == worker.computer_id


@pytest.mark.asyncio
async def test_gateway_rejection_returns_failure_evidence_without_physical_call(tmp_path) -> None:
    state = GoalRunState(goal_id="goal-reject", goal_text="reject one action")
    physical_calls: list[str] = []

    async def reject(_request):
        return False

    worker = CanonicalWorkerAdapter(
        task_id="task-reject",
        objective=state.goal_text,
        approval_resolver=reject,
        policy_hook=lambda request: load("obase").ActionDecision(
            verdict="REQUIRE_APPROVAL",
            reason="rejected by operator",
            request_id=request.request_id,
        ),
    )
    await worker.before_execution(state, str(tmp_path))
    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-reject",
        computer_ref=worker.computer_id,
        approval={"side_effect": "remote"},
        executor=lambda request: worker.execute_canonical_action(
            state,
            request,
            gateway_executor=lambda _request: physical_calls.append("called"),
        ),
    )
    result = await master.execute("publish", {"path": "artifact"})
    assert result.status == "failed"
    assert result.executed is False
    assert result.failure_evidence
    assert physical_calls == []


@pytest.mark.asyncio
async def test_master_coordinator_uses_bound_goalrun_adapter_instead_of_raw_physical_path() -> None:
    calls: list[tuple[str, dict]] = []

    class _GoalRunAdapter:
        async def execute(self, tool: str, arguments: dict) -> CanonicalActionResult:
            calls.append((tool, arguments))
            return CanonicalActionResult(
                action_id="action-coordinator",
                status="completed",
                attempted=True,
                executed=True,
                result="goalrun-result",
            )

    coordinator = MasterCoordinator(max_rounds=1)
    coordinator.bind_canonical_action_adapter(_GoalRunAdapter())
    result = await coordinator._guarded_handle_tool_call("physical_tool", {"x": 1})
    assert result == "goalrun-result"
    assert calls == [("physical_tool", {"x": 1})]


@pytest.mark.asyncio
async def test_restart_resumes_same_action_through_gateway_without_duplicate_side_effect(
    tmp_path,
) -> None:
    state = GoalRunState(goal_id="goal-restart", goal_text="persist one effect")
    physical_calls: list[str] = []

    worker_a = CanonicalWorkerAdapter(
        task_id="task-restart", objective=state.goal_text, approval_resolver=lambda _request: True
    )
    await worker_a.before_execution(state, str(tmp_path))
    computer_id = worker_a.computer_id

    async def physical(_request: CanonicalActionRequest) -> dict:
        physical_calls.append("physical")
        return {"status": "completed", "executed": True, "result": "committed"}

    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-restart",
        computer_ref=computer_id,
        approval={"side_effect": "remote", "effect_capability": "idempotency_key"},
        executor=lambda request: worker_a.execute_canonical_action(
            state, request, gateway_executor=physical
        ),
    )
    request = master.request("publish", {"path": "artifact"})
    claim_exclusive_session(
        worker_a.computer_store,
        computer_id=computer_id,
        owner_id=state.goal_id,
        supervisor_id="supervisor-a",
    )
    first = await worker_a.execute_canonical_action(state, request, gateway_executor=physical)
    assert first.status == "completed"
    assert physical_calls == ["physical"]

    worker_b = CanonicalWorkerAdapter(
        task_id="task-restart", objective=state.goal_text, approval_resolver=lambda _request: True
    )
    await worker_b.before_execution(state, str(tmp_path))
    assert worker_b.computer_id == computer_id
    resumed = await restart_and_resume_canonical_action(
        worker_a.computer_store,
        computer_id=computer_id,
        owner_id=state.goal_id,
        supervisor_a="supervisor-a",
        supervisor_b="supervisor-b",
        request=request,
        executor=lambda value: worker_b.execute_canonical_action(
            state, value, gateway_executor=physical
        ),
    )
    assert resumed.status == "completed"
    assert resumed.action_id == request.action_id
    assert physical_calls == ["physical"]
