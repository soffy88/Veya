"""P2: evidence layer, independent internal review, retask/completion authority."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from veya.supervision import (
    ExecutionReport,
    InternalSupervisor,
    Mission,
    MissionStatus,
    ReviewDecision,
    SupervisorReview,
    SupervisorUnavailable,
    apply_review,
    build_execution_report,
    plan_retask,
)
from veya.supervision.store import MissionStore


def _mission(**kw) -> Mission:
    return Mission(mission_id="m-1", goal="ship it", acceptance_criteria=["tests pass"], **kw)


def _goalrun_state():
    node_ok = types.SimpleNamespace(
        title="write code",
        assignee="opencode",
        status="completed",
        acceptance=["unit passes"],
        verify_summary="passed",
        artifacts=["out/a.txt"],
        evidence=[{"kind": "command", "exit_code": 0}],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )
    node_bad = types.SimpleNamespace(
        title="run dsh",
        assignee="dsh",
        status="blocked",
        acceptance=["env ok"],
        verify_summary=None,
        artifacts=[],
        evidence=[],
        retries=2,
        block_reason="missing binary",
        unfinished_work=["install tool"],
    )
    return types.SimpleNamespace(
        goal_id="goalrun-1",
        status="partial_completed",
        tasks={"t1": node_ok, "t2": node_bad},
        final_summary="partial",
        unfinished_work=["install tool"],
    )


# ── evidence ────────────────────────────────────────────────────────────
def test_evidence_projects_goalrun_state() -> None:
    report = build_execution_report(_mission(), _goalrun_state(), iteration=2, checkpoint_id="ck-1")
    assert report.mission_id == "m-1" and report.goalrun_id == "goalrun-1"
    assert report.checkpoint_id == "ck-1"
    assert {c["task_id"] for c in report.changes} == {"t1", "t2"}
    assert report.artifacts == [{"task_id": "t1", "path": "out/a.txt"}]
    assert [t["task_id"] for t in report.tests] == ["t1", "t2"]
    assert [f["task_id"] for f in report.failures] == ["t2"]
    assert "t2" in [b["task_id"] for b in report.blocked_items]
    # mission-level unfinished work also blocks completion
    assert any(b["reason"] == "install tool" for b in report.blocked_items)
    assert report.deviations and report.deviations[0]["retries"] == 2
    assert report.proposed_next_action == "revise"


# ── reviewer isolation + parsing ────────────────────────────────────────
def test_review_context_is_isolated() -> None:
    supervisor = InternalSupervisor(llm=None)
    ctx = supervisor.build_review_context(
        _mission(), build_execution_report(_mission(), _goalrun_state(), iteration=1)
    )
    assert set(ctx.payload()) == {
        "mission_id",
        "goal",
        "acceptance_criteria",
        "constraints",
        "report",
        "evidence",
    }
    import json

    payload = json.dumps(ctx.payload())
    assert "self_assessment" not in payload and "executor_chain" not in payload


async def test_review_requires_a_model() -> None:
    supervisor = InternalSupervisor(llm=None)
    with pytest.raises(SupervisorUnavailable):
        await supervisor.review(
            _mission(), build_execution_report(_mission(), _goalrun_state(), iteration=1)
        )


async def test_review_parses_valid_decision() -> None:
    async def llm(_: str) -> str:
        return '{"decision": "REVISE", "reason": "missing test", "next_task": "add a test"}'

    supervisor = InternalSupervisor(llm=llm)
    review = await supervisor.review(
        _mission(), build_execution_report(_mission(), _goalrun_state(), iteration=1)
    )
    assert review.decision is ReviewDecision.revise
    assert review.next_task == "add a test"


async def test_invalid_review_output_escalates_never_accepts() -> None:
    async def llm(_: str) -> str:
        return "looks good to me!"

    supervisor = InternalSupervisor(llm=llm)
    review = await supervisor.review(
        _mission(), build_execution_report(_mission(), _goalrun_state(), iteration=1)
    )
    assert review.decision is ReviewDecision.escalate


# ── retask / completion authority ───────────────────────────────────────
def test_executor_cannot_complete_with_unresolved_failures() -> None:
    report = build_execution_report(_mission(), _goalrun_state(), iteration=1)
    review = SupervisorReview(
        mission_id="m-1", iteration=1, supervisor="internal", decision=ReviewDecision.done
    )
    outcome = plan_retask(review, mission=_mission(), report=report)
    assert outcome.mission_status is MissionStatus.blocked


def test_accept_and_done_map_to_terminal_states() -> None:
    clean = ExecutionReport(
        mission_id="m-1",
        iteration=1,
        objective="x",
        status="completed",
        evidence_chain=[{"category": "runtime", "kind": "verified"}],
    )
    accept = SupervisorReview(
        mission_id="m-1", iteration=1, supervisor="internal", decision=ReviewDecision.accept
    )
    assert (
        plan_retask(accept, mission=_mission(), report=clean).mission_status
        is MissionStatus.accepted
    )
    done = SupervisorReview(
        mission_id="m-1", iteration=1, supervisor="internal", decision=ReviewDecision.done
    )
    accepted = _mission()
    accepted.status = MissionStatus.accepted
    assert plan_retask(done, mission=accepted, report=clean).mission_status is MissionStatus.done


def test_revise_produces_next_task() -> None:
    review = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task={"worker": "opencode", "objective": "fix the failing test"},  # type: ignore[arg-type]
        acceptance_delta=["regression test added"],
    )
    outcome = plan_retask(review, mission=_mission(), iteration=1)
    assert outcome.mission_status is MissionStatus.retasking
    assert outcome.next_task is not None and outcome.next_task.objective == "fix the failing test"
    assert outcome.next_task.acceptance == ["regression test added"]


def _worker_report(worker: str, *, required: list[str] | None = None) -> ExecutionReport:
    return ExecutionReport(
        mission_id="m-1",
        iteration=0,
        objective="goal",
        status="completed",
        runtime_evidence=[
            {
                "kind": "l1_execution",
                "subtask_id": f"child-{worker}",
                "worker": worker.upper(),
                "execution_id": f"old-{worker}",
                "status": "COMPLETED",
                "required_artifacts": list(required or []),
                "dependency_context": {"source": "canonical"},
            }
        ],
    )


@pytest.mark.parametrize("worker", ["dsh", "pi", "grok", "opencode"])
def test_retask_preserves_explicit_worker_and_artifact_obligations(worker: str) -> None:
    required = [f"case_a/{worker}.txt"]
    raw = {
        "task_id": f"retask-{worker}",
        "worker": worker,
        "objective": "produce the corrected deliverable",
    }
    review = SupervisorReview(
        mission_id="m-1",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task=raw,  # type: ignore[arg-type]
    )

    outcome = plan_retask(
        review, mission=_mission(), report=_worker_report(worker, required=required)
    )

    assert outcome.mission_status is MissionStatus.retasking
    assert outcome.next_task is not None
    assert outcome.next_task.inputs["worker_type"] == worker
    assert outcome.next_task.inputs["required_artifacts"] == required
    assert outcome.retask_lineage["worker_type"] == worker
    assert outcome.retask_lineage["required_artifacts"] == required
    assert outcome.retask_lineage["original_child_execution_id"] == f"old-{worker}"


def test_retask_required_artifacts_support_explicit_correction_override() -> None:
    raw = {
        "worker": "dsh",
        "objective": "produce corrected output",
        "required_artifacts": ["case_a/corrected.txt"],
    }
    review = SupervisorReview(
        mission_id="m-1",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task=raw,  # type: ignore[arg-type]
    )

    outcome = plan_retask(
        review,
        mission=_mission(),
        report=_worker_report("dsh", required=["case_a/original.txt"]),
    )

    assert outcome.next_task is not None
    assert outcome.next_task.inputs["required_artifacts"] == ["case_a/corrected.txt"]


def test_retask_preserves_materialized_dependency_context() -> None:
    report = _worker_report("dsh", required=["case_a/dsh.txt"])
    report.artifacts = [
        {
            "kind": "artifact",
            "subtask_id": "a-hicode",
            "materialized_path": "/workspace/.veya/artifacts/ex-a/case_a/a.txt",
            "relative_path": "case_a/a.txt",
            "hash": "a" * 64,
            "size": 1,
        },
        {
            "kind": "artifact",
            "subtask_id": "b-pi",
            "materialized_path": "/workspace/.veya/artifacts/ex-b/case_a/b.txt",
            "relative_path": "case_a/b.txt",
            "hash": "b" * 64,
            "size": 1,
        },
    ]
    raw = {
        "worker": "dsh",
        "objective": "correct the DSH output",
        "depends_on": ["a-hicode", "b-pi"],
        "required_artifacts": ["case_a/corrected.txt"],
    }
    review = SupervisorReview(
        mission_id="m-1",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task=raw,  # type: ignore[arg-type]
    )

    outcome = plan_retask(review, mission=_mission(), report=report)

    assert outcome.next_task is not None
    dependencies = outcome.next_task.inputs["dependency_artifacts"]
    assert [item["subtask_id"] for item in dependencies] == ["a-hicode", "b-pi"]
    assert outcome.retask_lineage["dependency_context"]["depends_on"] == [
        "a-hicode",
        "b-pi",
    ]


def test_retask_missing_worker_fails_closed() -> None:
    review = SupervisorReview(
        mission_id="m-1",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task={"objective": "worker is not specified"},  # type: ignore[arg-type]
    )

    outcome = plan_retask(review, mission=_mission())

    assert outcome.mission_status is MissionStatus.blocked
    assert outcome.reason == "RETASK_BLOCKED_WORKER_UNRESOLVED"
    assert outcome.next_task is None


def test_apply_review_persists_retask_worker_and_artifacts(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = store.save(_mission())
    raw = {
        "task_id": "dsh-correction",
        "worker": "dsh",
        "objective": "produce corrected DSH output",
    }
    review = SupervisorReview(
        mission_id="m-1",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task=raw,  # type: ignore[arg-type]
    )
    outcome = apply_review(
        store,
        mission,
        review,
        iteration=0,
        report=_worker_report("dsh", required=["case_a/dsh.txt"]),
    )

    assert outcome.mission_status is MissionStatus.retasking
    pending = store.load("m-1").authority["pending_task_retask"]  # type: ignore[union-attr]
    assert pending["worker_type"] == "dsh"
    assert pending["required_artifacts"] == ["case_a/dsh.txt"]
    assert pending["dependency_context"] == {"source": "canonical"}


def test_structured_next_task_is_normalized_without_rewriting_review(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = store.save(_mission())
    raw = {
        "task_id": "child-fix",
        "worker": "OPENCODE",
        "objective": "create the correction artifact",
        "acceptance": ["artifact exists"],
    }
    review = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task=raw,  # type: ignore[arg-type]
    )
    review.raw_review = {"raw_text": json.dumps(raw), "parsed": raw}
    outcome = apply_review(store, mission, review, iteration=1)
    assert outcome.mission_status is MissionStatus.retasking
    persisted = store.latest_review("m-1")
    assert persisted is not None and persisted.next_task == raw
    pending = store.load("m-1").authority["pending_task_retask"]  # type: ignore[union-attr]
    assert pending["raw_next_task"] == raw
    assert pending["normalized_next_task"] == raw["objective"]
    assert pending["source_review_id"]
    assert pending["retask_child_execution_id"] is None
    raw_records = store.raw_reviews("m-1")
    assert raw_records[0]["raw_review"]["parsed"] == raw
    assert raw_records[0]["normalized_review"]["next_task"] == raw


def test_invalid_structured_next_task_fails_closed(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = store.save(_mission())
    review = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task={"task_id": "missing-objective"},  # type: ignore[arg-type]
    )
    outcome = apply_review(store, mission, review, iteration=1)
    assert outcome.mission_status is MissionStatus.blocked
    assert outcome.reason == "RETASK_BLOCKED_INVALID_NEXT_TASK"
    assert "pending_task_retask" not in (store.load("m-1").authority)  # type: ignore[union-attr]


def test_budget_exhaustion_blocks() -> None:
    mission = _mission()
    mission.budget.max_iterations = 2
    review = SupervisorReview(
        mission_id="m-1",
        iteration=2,
        supervisor="internal",
        decision=ReviewDecision.retry,
        next_task="again",
    )
    assert plan_retask(review, mission=mission, iteration=2).mission_status is MissionStatus.blocked


def test_escalation_owner_vs_external() -> None:
    owner = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.escalate,
        reason="credential required to continue",
    )
    outcome = plan_retask(owner, mission=_mission(), iteration=1)
    assert outcome.mission_status is MissionStatus.waiting_owner
    assert outcome.escalation_code is not None

    external = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.escalate,
        reason="unparseable expert disagreement",
    )
    assert plan_retask(external, mission=_mission(), iteration=1).mission_status is (
        MissionStatus.waiting_external_supervisor
    )


def test_apply_review_persists_and_transitions(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = store.save(_mission())
    review = SupervisorReview(
        mission_id="m-1",
        iteration=1,
        supervisor="internal",
        decision=ReviewDecision.retry,
        next_task={"worker": "opencode", "objective": "try again"},  # type: ignore[arg-type]
    )
    outcome = apply_review(store, mission, review, iteration=0)
    assert outcome.mission_status is MissionStatus.retasking
    assert store.load("m-1").status is MissionStatus.retasking  # type: ignore[union-attr]
    topics = [e["topic"] for e in store.events("m-1")]
    assert "REVIEW_COMPLETED" in topics and "RETASK_CREATED" in topics
    assert store.latest_review("m-1").decision is ReviewDecision.retry  # type: ignore[union-attr]


def test_accept_rejects_unsatisfied_artifact_requirement() -> None:
    report = ExecutionReport(
        mission_id="m-1",
        iteration=1,
        objective="x",
        status="completed",
        runtime_evidence=[
            {
                "kind": "l1_execution",
                "artifact_requirement": "UNSATISFIED",
                "missing_required_artifacts": ["case_a/hicode.txt"],
            }
        ],
    )
    review = SupervisorReview(
        mission_id="m-1", iteration=1, supervisor="internal", decision=ReviewDecision.accept
    )
    outcome = plan_retask(review, mission=_mission(), report=report)
    assert outcome.mission_status is MissionStatus.blocked
    assert "artifact requirements" in outcome.reason
