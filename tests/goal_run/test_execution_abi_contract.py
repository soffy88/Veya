"""C3 Step 1 — canonical execution ABI contract tests.

Step 1 adds the contract only; no caller is migrated. These tests lock the
contract and prove it introduces no new authority.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from pathlib import Path

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from server.goal_run.execution_abi import (
    ActivityReference,
    CanonicalGoalExecutionRequest,
    CanonicalGoalExecutionResult,
    ExecutionBindingRef,
    ExecutionContractError,
    ExecutionReceiptReference,
    substrate_for,
    validate_request,
    validate_result,
    validate_result_evidence,
)
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import (
    ExecutionReceipt,
    ExecutorBinding,
    get_admission,
    record_execution_receipt,
    reset_admissions,
    reset_scopes,
)
from server.goal_run.models import GoalRunState, GoalStatus
from server.goal_run.store import save_goal_run


@pytest.fixture(autouse=True)
def _clean_admission():
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


def _state(tmp_path: Path, *, bot_id: str = "bot-a", executor: str = "codex") -> GoalRunState:
    state = GoalRunState(
        goal_id="goal-abi-1",
        goal_text="do the thing",
        bot_id=bot_id,
        requested_executor=executor,
        execution_id="exec-abi-1",
    )
    save_goal_run(state, str(tmp_path))
    return state


def _binding(
    tmp_path: Path, *, executor: str = "codex", substrate: str = "local2"
) -> ExecutorBinding:
    return get_admission(str(tmp_path)).create_binding(
        executor_id=executor,
        substrate_id=substrate,
        installation_id="default",
        provider_id=executor,
        goal_run_id="goal-abi-1",
        admission_id="adm-abi-1",
        provider_run_id="exec-abi-1",
        project_root=str(tmp_path),
    )


def _request(state: GoalRunState, binding: ExecutorBinding) -> CanonicalGoalExecutionRequest:
    return CanonicalGoalExecutionRequest(
        goal_run_id=state.goal_id,
        task_id="task-1",
        bot_id=state.bot_id,
        admitted_executor=binding.executor_id,
        substrate=binding.effective_substrate_id,
        binding=ExecutionBindingRef.from_binding(binding),
        objective=state.goal_text,
    )


# ── 1. type / field contract ────────────────────────────────────────────


def test_request_and_result_are_frozen_dataclasses_with_expected_fields():
    assert dataclasses.is_dataclass(CanonicalGoalExecutionRequest)
    assert dataclasses.is_dataclass(CanonicalGoalExecutionResult)
    assert [f.name for f in dataclasses.fields(CanonicalGoalExecutionRequest)] == [
        "goal_run_id",
        "task_id",
        "bot_id",
        "admitted_executor",
        "substrate",
        "objective",
        "binding",
        "mode",
    ]
    assert [f.name for f in dataclasses.fields(CanonicalGoalExecutionResult)] == [
        "execution_id",
        "status",
        "admitted_executor",
        "substrate",
        "activity",
        "receipt",
        "harness_evidence",
    ]
    # Frozen: no mutable state can leak through the ABI.
    request = CanonicalGoalExecutionRequest(
        goal_run_id="g",
        task_id="t",
        bot_id="b",
        admitted_executor="codex",
        substrate="local2",
        binding=ExecutionBindingRef("bind-1", "adm-1", "codex", "local2"),
        objective="o",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.substrate = "codex"  # type: ignore[misc]


# ── 5. no new authority ─────────────────────────────────────────────────


def test_abi_types_do_not_shadow_the_masteragent_contract():
    """The GoalRun ABI and the MasterAgent contract are distinct types."""
    from server.goal_run import execution_abi
    from server.models import execution as master_exec

    abi_request = execution_abi.CanonicalGoalExecutionRequest
    abi_result = execution_abi.CanonicalGoalExecutionResult
    master_request = master_exec.CanonicalExecutionRequest

    # Different objects with different field sets: neither substitutes the other.
    assert abi_request is not master_request
    assert {f.name for f in dataclasses.fields(abi_request)} == {
        "goal_run_id",
        "task_id",
        "bot_id",
        "admitted_executor",
        "substrate",
        "objective",
        "binding",
        "mode",
    }
    assert set(master_request.model_fields) == {
        "source",
        "mode",
        "objective",
        "project_root",
        "constraints",
        "preplanned_spec",
        "session_id",
        "capability",
    }
    assert dataclasses.is_dataclass(abi_result)
    assert not dataclasses.is_dataclass(master_request)

    # The ABI module no longer exports the generic MasterAgent names.
    assert not hasattr(execution_abi, "CanonicalExecutionRequest")
    assert not hasattr(execution_abi, "CanonicalExecutionResult")


def test_status_reuses_goal_status_and_abi_owns_no_new_authority():
    from server.goal_run import execution_abi

    assert not hasattr(execution_abi, "ExecutionStatus"), (
        "C3 must not introduce a second status authority; reuse GoalStatus"
    )
    assert typing.get_type_hints(CanonicalGoalExecutionResult)["status"] is GoalStatus

    source = inspect.getsource(execution_abi)
    for forbidden in (
        "save_goal_run",
        "record_execution_receipt",
        "ExecutionAdmission(",
        "class ExecutionStatus",
    ):
        assert forbidden not in source, f"ABI must not own authority: {forbidden!r}"


# ── 2. codex + local2 roles never collide ───────────────────────────────


def test_codex_local2_roles_do_not_collide(tmp_path: Path):
    state = _state(tmp_path)
    binding = _binding(tmp_path, executor="codex", substrate="local2")
    request = _request(state, binding)

    assert request.admitted_executor == "codex"
    assert request.substrate == "local2"
    validate_request(request, state=state)

    # Substrate must match the authoritative binding; it may not be overwritten.
    with pytest.raises(ExecutionContractError):
        validate_request(
            dataclasses.replace(request, substrate="codex"),
            state=state,
        )
    # A substrate must never overwrite the admitted executor identity.
    with pytest.raises(ExecutionContractError):
        validate_request(
            dataclasses.replace(request, binding=ExecutionBindingRef("b", "a", "local2", "local2")),
            state=state,
        )


# ── 3. default substrate rule ───────────────────────────────────────────


def test_default_substrate_rule(tmp_path: Path):
    assert substrate_for("codex") == "codex"
    assert substrate_for("codex", "local2") == "local2"

    state = _state(tmp_path)
    binding = _binding(tmp_path, executor="codex", substrate="")
    ref = ExecutionBindingRef.from_binding(binding)
    assert ref.effective_substrate_id == "codex"
    request = _request(state, binding)
    assert request.substrate == "codex"
    validate_request(request, state=state)


# ── 4. bot ownership ────────────────────────────────────────────────────


def test_bot_ownership_mismatch_is_rejected(tmp_path: Path):
    state = _state(tmp_path, bot_id="bot-a")
    binding = _binding(tmp_path)
    request = _request(state, binding)
    foreign = dataclasses.replace(request, bot_id="bot-b")
    with pytest.raises(CrossBotAccessDenied):
        validate_request(foreign, state=state)


# ── evidence authority: real persisted references only ──────────────────


def test_result_references_durable_evidence(tmp_path: Path):
    state = _state(tmp_path)
    binding = _binding(tmp_path, executor="codex", substrate="local2")
    request = _request(state, binding)

    activity = ActivityRecorder(str(tmp_path)).record(
        state.goal_id, "EXECUTOR_SELECTED", "STARTING", {"substrate_id": "local2"}
    )
    record_execution_receipt(
        str(tmp_path),
        ExecutionReceipt(
            goal_run_id=state.goal_id,
            admission_id=binding.admission_id,
            binding_id=binding.binding_id,
            executor_id="codex",
            substrate_id="local2",
            substrate_kind="native_substrate",
            installation_id="default",
            provider_session_id=binding.binding_id,
            provider_run_id="exec-abi-1",
            workspace_id=str(tmp_path),
            permission_decision="admitted",
            resource_leases=[],
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            final_state="completed",
        ),
    )

    result = CanonicalGoalExecutionResult(
        execution_id="exec-abi-1",
        status=GoalStatus.completed,
        admitted_executor="codex",
        substrate="local2",
        activity=ActivityReference(state.goal_id, activity.sequence, activity.activity_id),
        receipt=ExecutionReceiptReference(
            goal_run_id=state.goal_id,
            admission_id=binding.admission_id,
            binding_id=binding.binding_id,
            execution_id="exec-abi-1",
            final_state="completed",
        ),
    )
    validate_result(result, request=request)
    validate_result_evidence(result, project_root=str(tmp_path))

    # A fabricated receipt reference is rejected against durable evidence.
    fabricated = dataclasses.replace(
        result,
        receipt=dataclasses.replace(result.receipt, binding_id="bind-does-not-exist"),
    )
    with pytest.raises(ExecutionContractError):
        validate_result_evidence(fabricated, project_root=str(tmp_path))
