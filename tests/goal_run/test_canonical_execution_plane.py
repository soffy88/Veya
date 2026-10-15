"""C3 Step 2 — canonical execution plane + two adapters (additive).

No production caller is migrated: the plane is exercised directly. Both
adapters converge on the existing ``ExecutionControl`` seam and the result is
built only from durable records.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from server.goal_run.execution_abi import (
    CanonicalExecutionPlane,
    CanonicalGoalExecutionRequest,
    CanonicalGoalExecutionResult,
    ExecutionAdapterError,
    ExecutionBindingRef,
    ExecutionContractError,
    validate_result_evidence,
)
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import reset_admissions, reset_scopes
from server.goal_run.execution_evidence import load_binding_record, load_durable_receipts
from server.goal_run.models import GoalStatus
from server.goal_run.pre_admission import bind_execution, create_pending_run
from server.goal_run.store import load_goal_run
from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)
from veya.remote.executor_adapters import register_default_adapters


@pytest.fixture(autouse=True)
def _reset():
    register_default_adapters()
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


def _bound_run(
    tmp_path: Path,
    *,
    bot_id: str = "bot-a",
    admitted: str = "codex",
    substrate: str = "local2",
    objective: str = "echo plane-ok",
):
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id=f"disp-{uuid.uuid4().hex[:8]}",
        session_id="sess",
        requested_executor=admitted,
        tasks=[{"worker": admitted, "task": objective}],
        bot_id=bot_id,
    )
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    bind_execution(pre, execution_id, str(tmp_path), substrate_id=substrate, admit=True)
    state = load_goal_run(str(tmp_path), pre.goal_run_id)
    assert state is not None
    binding = load_binding_record(str(tmp_path), str(state.runtime_checkpoint["binding_id"]))
    assert binding is not None
    return state, binding, execution_id


def _request(state, binding, *, objective: str = "echo plane-ok", bot_id: str | None = None):
    return CanonicalGoalExecutionRequest(
        goal_run_id=state.goal_id,
        task_id=next(iter(state.tasks), ""),
        bot_id=bot_id or state.bot_id,
        admitted_executor=str(binding["executor_id"]),
        substrate=str(binding["substrate_id"]),
        binding=ExecutionBindingRef(
            binding_id=str(binding["binding_id"]),
            admission_id=str(binding["admission_id"]),
            executor_id=str(binding["executor_id"]),
            substrate_id=str(binding["substrate_id"]),
        ),
        objective=objective,
    )


# ── 1 + 2: one entry, both adapters; codex/local2 never confused ────────


async def test_both_adapters_dispatch_from_one_entry(tmp_path: Path, monkeypatch):
    plane = CanonicalExecutionPlane()

    # Native-substrate path: admitted codex, physical substrate local2.
    state, binding, _ = _bound_run(tmp_path / "l2", substrate="local2")
    result_local2 = await plane.execute(_request(state, binding), project_root=str(tmp_path / "l2"))
    assert isinstance(result_local2, CanonicalGoalExecutionResult)
    assert result_local2.admitted_executor == "codex"
    assert result_local2.substrate == "local2"

    # Executor/harness path: admitted codex running its own worker substrate.
    monkeypatch.setattr(
        "veya.remote.tool_adapter._worker_command",
        lambda *a, **k: (["echo", "codex-ok"], {}),
    )
    state2, binding2, _ = _bound_run(tmp_path / "cx", substrate="codex")
    result_exec = await plane.execute(_request(state2, binding2), project_root=str(tmp_path / "cx"))
    assert isinstance(result_exec, CanonicalGoalExecutionResult)
    assert result_exec.admitted_executor == "codex"
    assert result_exec.substrate == "codex"


async def test_codex_local2_identity_not_confused(tmp_path: Path):
    state, binding, execution_id = _bound_run(tmp_path, substrate="local2")
    assert binding["executor_id"] == "codex"
    assert binding["substrate_id"] == "local2"
    assert binding["executor_kind"]
    assert binding["substrate_kind"] == "native_substrate"

    result = await CanonicalExecutionPlane().execute(
        _request(state, binding), project_root=str(tmp_path)
    )
    assert result.admitted_executor == "codex"
    assert result.substrate == "local2"
    # The completion receipt names both roles; local2 never becomes the executor.
    receipts = load_durable_receipts(str(tmp_path), state.goal_id)
    completion = [r for r in receipts if r.get("provider_run_id") == execution_id]
    assert completion and completion[-1]["executor_id"] == "codex"
    assert completion[-1]["substrate_id"] == "local2"


# ── 3: default substrate rule ───────────────────────────────────────────


async def test_default_substrate_rule(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "veya.remote.tool_adapter._worker_command",
        lambda *a, **k: (["echo", "codex-default"], {}),
    )
    state, binding, _ = _bound_run(tmp_path, substrate="")
    assert binding["substrate_id"] == "codex"
    assert binding["substrate_kind"] == "cli_worker"
    result = await CanonicalExecutionPlane().execute(
        _request(state, binding), project_root=str(tmp_path)
    )
    assert result.substrate == "codex"


# ── 4 + 5: ownership and binding conflicts are refused ──────────────────


async def test_bot_mismatch_is_rejected(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, bot_id="bot-a")
    request = _request(state, binding, bot_id="bot-b")
    with pytest.raises(CrossBotAccessDenied):
        await CanonicalExecutionPlane().execute(request, project_root=str(tmp_path))


async def test_executor_substrate_conflict_with_binding_is_rejected(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2")
    bad = dataclasses.replace(_request(state, binding), substrate="codex")
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(bad, project_root=str(tmp_path))


# ── 6: unregistered adapter fails, no fallback ──────────────────────────


async def test_unregistered_adapter_fails_without_fallback(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2")
    plane = CanonicalExecutionPlane(adapters=[])
    with pytest.raises(ExecutionAdapterError):
        await plane.execute(_request(state, binding), project_root=str(tmp_path))


# ── 7: success references real durable evidence ─────────────────────────


async def test_success_references_durable_evidence(tmp_path: Path):
    state, binding, execution_id = _bound_run(tmp_path, substrate="local2")
    result = await CanonicalExecutionPlane().execute(
        _request(state, binding), project_root=str(tmp_path)
    )
    assert result.execution_id == execution_id
    assert result.status is GoalStatus.completed

    receipts = load_durable_receipts(str(tmp_path), state.goal_id)
    assert any(
        r.get("binding_id") == binding["binding_id"] and r.get("provider_run_id") == execution_id
        for r in receipts
    )
    activities, _ = ActivityRecorder(str(tmp_path)).stream(state.goal_id, cursor=0)
    assert any(a.sequence == result.activity.sequence for a in activities)
    validate_result_evidence(result, project_root=str(tmp_path))


# ── 8: missing / wrongly-associated evidence is invalid ─────────────────


async def test_missing_or_wrong_evidence_is_not_valid(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2")
    result = await CanonicalExecutionPlane().execute(
        _request(state, binding), project_root=str(tmp_path)
    )
    fabricated = dataclasses.replace(
        result,
        receipt=dataclasses.replace(result.receipt, binding_id="bind-does-not-exist"),
    )
    with pytest.raises(ExecutionContractError):
        validate_result_evidence(fabricated, project_root=str(tmp_path))


# ── 9: adapter failure is not converted to success ──────────────────────


async def test_adapter_failure_is_not_converted_to_success(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2", objective="false")
    result = await CanonicalExecutionPlane().execute(
        _request(state, binding, objective="false"), project_root=str(tmp_path)
    )
    assert result.status is GoalStatus.failed
    assert result.status is not GoalStatus.completed


# ── 10: authorities unchanged ───────────────────────────────────────────


async def test_authorities_remain_single_source(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2")
    result = await CanonicalExecutionPlane().execute(
        _request(state, binding), project_root=str(tmp_path)
    )
    # The status is exactly the GoalRun authority's status, not an adapter claim.
    reloaded = load_goal_run(str(tmp_path), state.goal_id)
    assert result.status is reloaded.status
    # Activity and receipt were written by their existing authorities.
    activities, _ = ActivityRecorder(str(tmp_path)).stream(state.goal_id, cursor=0)
    assert activities
    assert load_durable_receipts(str(tmp_path), state.goal_id)


# ── 11: a fake adapter cannot be mistaken for production evidence ───────


class _FakeAdapter:
    name = "fake"

    def supports(self, binding_record):
        return True

    async def run(self, request, *, state, project_root, binding_record, timeout_s=600.0):
        # Deliberately does nothing: it must not be able to produce a "success".
        return None


async def test_fake_adapter_is_not_mistaken_for_evidence(tmp_path: Path):
    state, binding, _ = _bound_run(tmp_path, substrate="local2")
    plane = CanonicalExecutionPlane(adapters=[_FakeAdapter()])
    with pytest.raises(ExecutionContractError):
        await plane.execute(_request(state, binding), project_root=str(tmp_path))
