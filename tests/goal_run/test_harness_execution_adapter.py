"""C3 Step 4 ABI revision — strict harness evidence + single-owner authorization.

Harness evidence is resolved against the authoritative persisted GoalRun/task
record. A taskgraph file existing is not execution evidence. ``execute_leaf``
owns admission/harness authorization; the adapter does not repeat it. No
production caller is migrated.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import ClassVar

import pytest

from runtime.bot_scope import DEFAULT_BOT_ID, CrossBotAccessDenied
from server.goal_run.execution_abi import (
    CanonicalExecutionPlane,
    CanonicalGoalExecutionRequest,
    CanonicalGoalExecutionResult,
    ExecutionAdapter,
    ExecutionBindingRef,
    ExecutionContractError,
    ExecutionPlaneMode,
    HarnessEvidenceReference,
    HarnessEvidenceSource,
    HarnessExecutionAdapter,
    Local2SubstrateAdapter,
    validate_harness_evidence,
    validate_result,
)
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode
from server.goal_run.store import load_goal_run, save_goal_run
from server.goal_run.trust_plane import (
    append_trust_plane_records,
    record_task_verification,
)
from veya.remote.executor_adapters import register_default_adapters


@pytest.fixture(autouse=True)
def _reset():
    register_default_adapters()
    yield


def _goal_run(
    tmp_path: Path,
    *,
    bot_id: str = DEFAULT_BOT_ID,
    tasks: tuple[str, ...] = ("t1",),
    assignee: str = "dsh",
) -> GoalRunState:
    state = GoalRunState(
        goal_id="goal-harness-1",
        goal_text="do work",
        bot_id=bot_id,
        status=GoalStatus.running,
    )
    for task_id in tasks:
        state.tasks[task_id] = TaskNode(
            id=task_id,
            title=f"Task {task_id}",
            instruction="do work",
            acceptance=[],
            depends_on=[],
            assignee=assignee,
        )
    save_goal_run(state, str(tmp_path))
    return state


def _persist_delegate(
    tmp_path: Path,
    state: GoalRunState,
    *,
    task_id: str = "t1",
    delegate_id: str | None = None,
    goal_run_id: str | None = None,
    artifacts: tuple[str, ...] = (),
) -> None:
    s = load_goal_run(str(tmp_path), state.goal_id)
    assert s is not None
    task = s.tasks[task_id]
    task.delegate_result = {
        "delegate_id": delegate_id if delegate_id is not None else task_id,
        "goal_run_id": goal_run_id if goal_run_id is not None else state.goal_id,
        "status": "complete",
        "summary": "persisted",
    }
    task.artifacts = list(artifacts)
    save_goal_run(s, str(tmp_path))


def _harness_request(
    state: GoalRunState,
    *,
    bot_id: object = ...,
    admitted: str = "dsh",
    substrate: str = "dsh",
    objective: str = "do work",
    mode: ExecutionPlaneMode = ExecutionPlaneMode.harness,
    binding: ExecutionBindingRef | None = None,
    task_id: str = "t1",
) -> CanonicalGoalExecutionRequest:
    return CanonicalGoalExecutionRequest(
        goal_run_id=state.goal_id,
        task_id=task_id,
        bot_id=state.bot_id if bot_id is ... else bot_id,  # type: ignore[arg-type]
        admitted_executor=admitted,
        substrate=substrate,
        objective=objective,
        binding=binding,
        mode=mode,
    )


def _result(
    state: GoalRunState,
    *,
    task_id: str = "t1",
    source: str = str(HarnessEvidenceSource.task_delegate_result),
    locator: str = "",
    artifacts: tuple[str, ...] = (),
    goal_run_id: str | None = None,
) -> CanonicalGoalExecutionResult:
    return CanonicalGoalExecutionResult(
        execution_id=f"harness:{state.goal_id}:{task_id}",
        status=GoalStatus.running,
        admitted_executor="dsh",
        substrate="dsh",
        harness_evidence=HarnessEvidenceReference(
            goal_run_id=goal_run_id if goal_run_id is not None else state.goal_id,
            task_id=task_id,
            source=source,
            evidence_locator=locator,
            artifact_refs=tuple(artifacts),
        ),
    )


def _taskgraph(tmp_path: Path, state: GoalRunState) -> str:
    return str(tmp_path / ".veya-project" / "goal-runs" / state.goal_id / "taskgraph.json")


@pytest.fixture
def fake_harness(tmp_path: Path, monkeypatch):
    artifact = tmp_path / "harness_artifact.txt"
    artifact.write_text("harness produced this", encoding="utf-8")

    from server.capability_model import harness_registry

    class _Resp:
        status = "completed"
        summary = "harness ok"
        block_reason = None
        artifacts: ClassVar[list[str]] = [str(artifact)]

    async def _fake_execute(
        harness_id, *, store, task_id, request, project_root=None, understand_prefix=""
    ):
        return _Resp()

    monkeypatch.setattr(harness_registry, "execute", _fake_execute)
    return artifact


# ── strict evidence: taskgraph existence is not execution evidence ──────


def test_empty_unexecuted_task_is_rejected(tmp_path):
    state = _goal_run(tmp_path)  # no delegate_result persisted
    result = _result(state, locator=_taskgraph(tmp_path, state))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            result, request=_harness_request(state), project_root=str(tmp_path)
        )


def test_other_tasks_delegate_result_does_not_validate_this_task(tmp_path):
    state = _goal_run(tmp_path, tasks=("t1", "t2"))
    _persist_delegate(tmp_path, state, task_id="t2")  # only t2 has evidence
    result = _result(state, task_id="t1", locator=_taskgraph(tmp_path, state))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            result, request=_harness_request(state), project_root=str(tmp_path)
        )


def test_mismatched_goal_run_id_is_rejected(tmp_path):
    state = _goal_run(tmp_path)
    _persist_delegate(tmp_path, state, goal_run_id="some-other-goal")
    result = _result(state, locator=_taskgraph(tmp_path, state))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            result, request=_harness_request(state), project_root=str(tmp_path)
        )


def test_mismatched_delegate_id_is_rejected(tmp_path):
    state = _goal_run(tmp_path)
    _persist_delegate(tmp_path, state, delegate_id="someone-else")
    result = _result(state, locator=_taskgraph(tmp_path, state))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            result, request=_harness_request(state), project_root=str(tmp_path)
        )


def test_unpersisted_artifact_cannot_validate(tmp_path):
    state = _goal_run(tmp_path)
    _persist_delegate(tmp_path, state, artifacts=())  # authoritative set is empty
    rogue = tmp_path / "rogue.txt"
    rogue.write_text("not attributable", encoding="utf-8")
    result = _result(state, locator=_taskgraph(tmp_path, state), artifacts=(str(rogue),))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            result, request=_harness_request(state), project_root=str(tmp_path)
        )


def test_artifact_references_must_match_authoritative_set(tmp_path):
    state = _goal_run(tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("ok", encoding="utf-8")
    _persist_delegate(tmp_path, state, artifacts=(str(good),))
    # extra artifact not in the authoritative set -> reject
    extra = tmp_path / "extra.txt"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            _result(state, locator=_taskgraph(tmp_path, state), artifacts=(str(good), str(extra))),
            request=_harness_request(state),
            project_root=str(tmp_path),
        )
    # exact match -> pass
    validate_harness_evidence(
        _result(state, locator=_taskgraph(tmp_path, state), artifacts=(str(good),)),
        request=_harness_request(state),
        project_root=str(tmp_path),
    )


def test_valid_persisted_delegate_evidence_passes(tmp_path):
    state = _goal_run(tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("ok", encoding="utf-8")
    _persist_delegate(tmp_path, state, artifacts=(str(good),))
    validate_harness_evidence(
        _result(state, locator=_taskgraph(tmp_path, state), artifacts=(str(good),)),
        request=_harness_request(state),
        project_root=str(tmp_path),
    )


def test_trust_plane_evidence_passes_only_when_verified(tmp_path):
    state = _goal_run(tmp_path)
    # Not verified -> no VerifiedState -> reject.
    claim, ev, evals, _none = record_task_verification(
        task_id="t1",
        goal_id=state.goal_id,
        actor="dsh",
        statement="not verified",
        target_refs=[],
        verify_passed=False,
        verify_summary="fail",
        diff_text="",
        review_findings=None,
    )
    append_trust_plane_records(
        str(tmp_path),
        state.goal_id,
        claim=claim,
        evidences=ev,
        evaluations=evals,
        verified_state=None,
    )
    ref = _result(state, source=str(HarnessEvidenceSource.trust_plane_verified_state))
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(ref, request=_harness_request(state), project_root=str(tmp_path))

    # Verified -> VerifiedState present -> pass.
    claim2, ev2, evals2, verified = record_task_verification(
        task_id="t1",
        goal_id=state.goal_id,
        actor="dsh",
        statement="verified",
        target_refs=[],
        verify_passed=True,
        verify_summary="ok",
        diff_text="",
        review_findings=None,
    )
    assert verified is not None
    append_trust_plane_records(
        str(tmp_path),
        state.goal_id,
        claim=claim2,
        evidences=ev2,
        evaluations=evals2,
        verified_state=verified,
    )
    validate_harness_evidence(ref, request=_harness_request(state), project_root=str(tmp_path))


def test_missing_and_unknown_evidence_fails_closed(tmp_path):
    state = _goal_run(tmp_path)
    # No reference at all.
    bare = CanonicalGoalExecutionResult(
        execution_id=f"harness:{state.goal_id}:t1",
        status=GoalStatus.running,
        admitted_executor="dsh",
        substrate="dsh",
    )
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(bare, request=_harness_request(state), project_root=str(tmp_path))
    # Unknown source.
    _persist_delegate(tmp_path, state)
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            _result(state, source="made.up.source"),
            request=_harness_request(state),
            project_root=str(tmp_path),
        )
    # Task not in the persisted GoalRun.
    with pytest.raises(ExecutionContractError):
        validate_harness_evidence(
            _result(state, task_id="ghost"),
            request=_harness_request(state, task_id="ghost"),
            project_root=str(tmp_path),
        )


# ── adapter: single-owner authorization, refusal is fail-closed ─────────


async def test_adapter_delegates_authorization_to_execute_leaf_exactly_once(
    tmp_path, fake_harness, monkeypatch
):
    state = _goal_run(tmp_path)
    calls = {"leaf": 0}

    import server.goal_run.leaf as leaf_mod

    orig = leaf_mod.execute_leaf_with_memory

    async def _spy(*a, **k):
        calls["leaf"] += 1
        return await orig(*a, **k)

    monkeypatch.setattr(leaf_mod, "execute_leaf_with_memory", _spy)
    await CanonicalExecutionPlane().execute(_harness_request(state), project_root=str(tmp_path))
    assert calls["leaf"] == 1


def test_adapter_no_longer_pre_runs_authorization():
    import inspect

    from server.goal_run import execution_abi

    source = inspect.getsource(execution_abi.HarnessExecutionAdapter)
    assert "_require_admitted_executor" not in source
    assert "_require_registered_harness" not in source
    assert not hasattr(execution_abi, "_require_registered_harness")


async def test_blocked_leaf_cannot_become_success(tmp_path, monkeypatch):
    state = _goal_run(tmp_path, assignee="ghost")  # not admitted -> execute_leaf blocks
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, admitted="ghost", substrate="ghost"),
            project_root=str(tmp_path),
        )
    # No task evidence was fabricated for a refused execution.
    persisted = load_goal_run(str(tmp_path), state.goal_id)
    assert not persisted.tasks["t1"].delegate_result


async def test_harness_mode_does_not_fabricate_receipt_or_activity(tmp_path, fake_harness):
    state = _goal_run(tmp_path)
    result = await CanonicalExecutionPlane().execute(
        _harness_request(state), project_root=str(tmp_path)
    )
    assert result.activity is None
    assert result.receipt is None
    assert result.harness_evidence is not None
    assert result.harness_evidence.source == "task.delegate_result"
    assert not (
        tmp_path / ".veya-project" / "goal-runs" / state.goal_id / "execution_receipts.jsonl"
    ).exists()
    activities, _ = ActivityRecorder(str(tmp_path)).stream(state.goal_id, cursor=0)
    assert activities == []


async def test_harness_end_to_end_evidence_resolves(tmp_path, fake_harness):
    state = _goal_run(tmp_path)
    result = await CanonicalExecutionPlane().execute(
        _harness_request(state), project_root=str(tmp_path)
    )
    ref = result.harness_evidence
    assert ref.artifact_refs == (str(fake_harness),)
    assert Path(ref.evidence_locator).exists()
    # The persisted record actually carries the delegate result.
    persisted = load_goal_run(str(tmp_path), state.goal_id)
    assert persisted.tasks["t1"].delegate_result["delegate_id"] == "t1"
    assert persisted.tasks["t1"].delegate_result["goal_run_id"] == state.goal_id
    # Evidence is not a completion decision.
    assert result.status is persisted.status is GoalStatus.running


# ── mode / identity / binding guards (retained) ─────────────────────────


async def test_harness_mode_rejects_unadmitted_executor(tmp_path):
    state = _goal_run(tmp_path, assignee="ghost")
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, admitted="ghost", substrate="ghost"),
            project_root=str(tmp_path),
        )


async def test_harness_mode_rejects_missing_harness(tmp_path):
    state = _goal_run(tmp_path, assignee="codex")  # admitted, no harness
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, admitted="codex", substrate="codex"),
            project_root=str(tmp_path),
        )


async def test_substrate_mode_rejects_missing_binding(tmp_path):
    state = _goal_run(tmp_path)
    request = _harness_request(
        state, admitted="codex", substrate="local2", mode=ExecutionPlaneMode.substrate, binding=None
    )
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(request, project_root=str(tmp_path))


async def test_harness_request_may_not_carry_binding(tmp_path):
    state = _goal_run(tmp_path)
    request = _harness_request(state, binding=ExecutionBindingRef("b", "a", "dsh", "dsh"))
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(request, project_root=str(tmp_path))


async def test_missing_bot_identity_differs_from_explicit_default(tmp_path, fake_harness):
    state = _goal_run(tmp_path, bot_id=DEFAULT_BOT_ID)
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, bot_id=None), project_root=str(tmp_path)
        )
    result = await CanonicalExecutionPlane().execute(
        _harness_request(state, bot_id=DEFAULT_BOT_ID), project_root=str(tmp_path)
    )
    assert result.harness_evidence is not None


async def test_explicit_default_cannot_claim_another_bots_run(tmp_path):
    state = _goal_run(tmp_path, bot_id="bot-x")
    with pytest.raises(CrossBotAccessDenied):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, bot_id=DEFAULT_BOT_ID), project_root=str(tmp_path)
        )


async def test_cross_bot_access_remains_denied(tmp_path):
    state = _goal_run(tmp_path, bot_id="bot-a")
    with pytest.raises(CrossBotAccessDenied):
        await CanonicalExecutionPlane().execute(
            _harness_request(state, bot_id="bot-b"), project_root=str(tmp_path)
        )


async def test_unknown_mode_fails_closed(tmp_path):
    state = _goal_run(tmp_path)
    request = dataclasses.replace(_harness_request(state), mode="mystery")  # type: ignore[arg-type]
    with pytest.raises(ExecutionContractError):
        await CanonicalExecutionPlane().execute(request, project_root=str(tmp_path))


def test_adapters_distinct_and_share_the_abi():
    harness = HarnessExecutionAdapter()
    substrate = Local2SubstrateAdapter()
    assert harness.name != substrate.name
    assert isinstance(harness, ExecutionAdapter)
    assert isinstance(substrate, ExecutionAdapter)
    assert harness.supports({"substrate_kind": "native_substrate"}) is False


def test_harness_result_rejects_fabricated_substrate_receipt():
    from server.goal_run.execution_abi import ActivityReference, ExecutionReceiptReference

    request = CanonicalGoalExecutionRequest(
        goal_run_id="g",
        task_id="t",
        bot_id="b",
        admitted_executor="dsh",
        substrate="dsh",
        objective="o",
        mode=ExecutionPlaneMode.harness,
    )
    result = CanonicalGoalExecutionResult(
        execution_id="harness:g:t",
        status=GoalStatus.running,
        admitted_executor="dsh",
        substrate="dsh",
        activity=ActivityReference("g", 1),
        receipt=ExecutionReceiptReference("g", "a", "b", "e"),
    )
    with pytest.raises(ExecutionContractError):
        validate_result(result, request=request)


def test_no_production_caller_references_the_harness_adapter_yet():
    importers = {
        str(p)
        for p in Path("server").rglob("*.py")
        if "HarnessExecutionAdapter" in p.read_text(encoding="utf-8")
    }
    assert importers == {"server/goal_run/execution_abi.py"}, importers


def test_ordinary_goalrun_path_remains_unmigrated():
    for rel in (
        "server/goal_run/runner.py",
        "server/goal_run/planner.py",
        "server/goal_run/leaf.py",
    ):
        source = Path(rel).read_text(encoding="utf-8")
        assert "CanonicalExecutionPlane" not in source
        assert "execution_abi" not in source
