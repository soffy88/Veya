"""Canonical Intelligence Loop qualification suite.

Verifies the single end-to-end intelligence loop:
Mission -> Canonical Planner -> Versioned DAG -> L2 Orchestrator -> L1 Workers
-> Evidence Normalizer -> Deterministic Verifiers -> Independent Verification Authority
-> JEV (semantic ambiguity only) -> ACCEPT / RETASK / REPLAN / ESCALATE.

Strict invariants checked:
- WORKER_SUCCESS_IS_ACCEPTANCE=NO
- L2_COMPLETION_IS_ACCEPTANCE=NO
- LLM_SELF_REPORT_IS_EVIDENCE=NO
- FALSE_SUCCESS=0
- DUPLICATE_SIDE_EFFECTS=0
- SINGLE_COMPLETION_AUTHORITY=YES
- DURABLE_RECOVERY=PASS
- JEV_CALL_COUNT=0 on happy path
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from runtime.verification.engine import VerificationEngine
from veya.decision import JevAnswer, JevDecision, QuestionKind
from veya.remote.executor_health import ExecutorHealthRegistry, ProviderFailureClass
from veya.remote.models import ExecutorHealth
from veya.supervision import MissionStore, SupervisionRouter
from veya.supervision.loop import MissionLoop
from veya.supervision.models import (
    ExecutionReport,
    MissionStatus,
    ReviewDecision,
    SupervisorReview,
)
from veya.supervision.orchestrated import Subtask, SubtaskResult, run_plan
from veya.supervision.planner_adapter import decompose
from veya.supervision.retask import apply_review


class TrackingJev:
    def __init__(self, confidence: float = 0.95, should_fail: bool = False) -> None:
        self.confidence = confidence
        self.should_fail = should_fail
        self.calls = 0

    async def decide(self, state: Any, questions: Any) -> JevDecision:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("429 Too Many Requests: provider quota exhausted")
        return JevDecision(
            answers={
                "safe_to_continue": JevAnswer(
                    "safe_to_continue",
                    QuestionKind.choice,
                    choice="yes",
                    confidence=self.confidence,
                )
            }
        )


def _setup_loop(
    tmp_path: Path,
    *,
    runner_func: Any,
    jev: Any = None,
    mode: str = "internal",
    verification_engine: Any = None,
    planner: Any = None,
) -> tuple[MissionLoop, MissionStore, str]:
    store = MissionStore(tmp_path)
    router = SupervisionRouter(store)
    loop = MissionLoop(
        store=store,
        router=router,
        runner=runner_func,
        jev=jev,
        verification_engine=verification_engine,
        planner=planner,
    )
    mid = (
        loop.facade()
        .create(goal="Build service", supervision_mode=mode, workspace=str(tmp_path))
        .mission_id
    )
    return loop, store, mid


# ── Case A: Happy Path ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_a_happy_path(tmp_path: Path) -> None:
    """Happy Path: deterministic verifier PASS, JEV_CALL_COUNT=0, mission completes."""
    v_engine = VerificationEngine(tmp_path)
    jev = TrackingJev(confidence=0.99)

    out_file = tmp_path / "result.txt"
    out_file.write_text("success content")

    async def runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="completed",
            artifacts=[{"path": "result.txt", "verified": True}],
            tests=[{"command": "pytest", "passed": 1, "failed": 0}],
            runtime_evidence=[
                {
                    "kind": "l1_execution",
                    "worker": "opencode",
                    "status": "COMPLETED",
                    "execution_id": "e-1",
                }
            ],
            evidence_chain=[{"category": "runtime", "kind": "l1_execution"}],
        )

    class AutoAcceptSupervisor:
        async def review(self, m: Any, r: Any) -> SupervisorReview:
            return SupervisorReview(
                mission_id=m.mission_id,
                iteration=r.iteration,
                supervisor="internal",
                decision=ReviewDecision.accept,
                reason="all criteria verified",
            )

    loop, store, mid = _setup_loop(
        tmp_path,
        runner_func=runner,
        jev=jev,
        mode="internal",
        verification_engine=v_engine,
    )
    loop.internal = AutoAcceptSupervisor()  # type: ignore[assignment]

    snap = await loop.step(mid)
    assert snap["status"] in ("DONE", "ACCEPTED")
    assert jev.calls == 0  # CRITICAL: happy path must have JEV_CALL_COUNT=0!
    m = store.load(mid)
    assert m is not None and m.status in (MissionStatus.done, MissionStatus.accepted)
    # Check verification event recorded
    events = [e for e in store.events(mid) if e["topic"] == "VERIFICATION_COMPLETED"]
    assert len(events) == 1
    assert events[0]["passed"] is True


# ── Case B: Worker Self-Report vs Verifier Missing Artifact ────────────────────
@pytest.mark.asyncio
async def test_case_b_adversarial_worker_missing_artifact(tmp_path: Path) -> None:
    """Adversarial worker claiming SUCCESS when artifact is missing is rejected."""
    v_engine = VerificationEngine(tmp_path)
    jev = TrackingJev()

    # Note: missing_result.txt does NOT exist on disk
    async def runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="completed",  # Worker claims completed!
            artifacts=[],  # Evidence normalizer found no file on disk!
            failures=[
                {
                    "kind": "artifact_missing",
                    "path": "missing_result.txt",
                    "reason": "executor claimed this file but it is absent on disk",
                }
            ],
            blocked_items=[{"reason": "missing required artifact missing_result.txt"}],
            runtime_evidence=[
                {
                    "kind": "l1_execution",
                    "worker": "codex",
                    "status": "COMPLETED",
                    "execution_id": "e-adv",
                }
            ],
        )

    class FooledSupervisor:
        async def review(self, m: Any, r: Any) -> SupervisorReview:
            # Even if LLM supervisor tries to blindly accept worker's claim:
            return SupervisorReview(
                mission_id=m.mission_id,
                iteration=r.iteration,
                supervisor="internal",
                decision=ReviewDecision.accept,
                reason="worker claims it succeeded",
            )

    loop, store, mid = _setup_loop(
        tmp_path,
        runner_func=runner,
        jev=jev,
        mode="internal",
        verification_engine=v_engine,
    )
    loop.internal = FooledSupervisor()  # type: ignore[assignment]

    snap = await loop.step(mid)
    # MUST be blocked, NEVER accepted
    assert snap["status"] == "BLOCKED"
    m = store.load(mid)
    assert m is not None and m.status is MissionStatus.blocked
    # Verification authority refused completion
    assert jev.calls == 0


# ── Case C: Deterministic Test Failure ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_c_deterministic_test_failure(tmp_path: Path) -> None:
    """Deterministic test failure results in FAIL verdict and retasking/blocked without JEV."""
    v_engine = VerificationEngine(tmp_path)
    jev = TrackingJev()

    async def runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="failed",
            tests=[{"command": "pytest", "passed": 5, "failed": 2}],
            failures=[{"task_id": "test", "reason": "2 tests failed"}],
            blocked_items=[{"reason": "test suite failed"}],
            runtime_evidence=[
                {
                    "kind": "l1_execution",
                    "worker": "opencode",
                    "status": "FAILED",
                    "execution_id": "e-fail",
                }
            ],
        )

    class RetaskSupervisor:
        async def review(self, m: Any, r: Any) -> SupervisorReview:
            return SupervisorReview(
                mission_id=m.mission_id,
                iteration=r.iteration,
                supervisor="internal",
                decision=ReviewDecision.retry,
                reason="test failed, retry",
                next_task={"worker": "opencode", "objective": "fix the test"},
            )

    loop, _store, mid = _setup_loop(
        tmp_path,
        runner_func=runner,
        jev=jev,
        mode="internal",
        verification_engine=v_engine,
    )
    loop.internal = RetaskSupervisor()  # type: ignore[assignment]

    snap = await loop.step(mid)
    assert snap["status"] == "RETASKING"
    assert jev.calls == 0  # Deterministic failure: no JEV call!


# ── Case D: Ambiguous Semantic Claim ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_d_ambiguous_semantic_claim(tmp_path: Path) -> None:
    """Ambiguous claim triggers JEV evaluation."""
    v_engine = VerificationEngine(tmp_path)
    jev = TrackingJev(confidence=0.92)

    async def runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="completed",
            artifacts=[],
            runtime_evidence=[
                {
                    "kind": "semantic_ambiguity",
                    "reason": "semantic requirement requires human/LLM adjudication",
                    "ambiguous": True,
                }
            ],
        )

    loop, store, mid = _setup_loop(
        tmp_path,
        runner_func=runner,
        jev=jev,
        mode="auto",
        verification_engine=v_engine,
    )

    await loop.step(mid)
    assert jev.calls == 1  # Ambiguity present: JEV invoked!
    report = store.get_report(mid, 0)
    assert report is not None and len(report.jev_decisions) == 1


# ── Case E: JEV Provider Failure (Fail-Closed) ─────────────────────────────────
@pytest.mark.asyncio
async def test_case_e_jev_failure_fail_closed(tmp_path: Path) -> None:
    """JEV provider failure (e.g. 429) fails closed and does not allow acceptance."""
    v_engine = VerificationEngine(tmp_path)
    jev = TrackingJev(should_fail=True)  # Will raise 429

    async def runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="completed",
            runtime_evidence=[
                {"kind": "ambiguity", "reason": "unclear semantic match", "ambiguous": True}
            ],
        )

    loop, store, mid = _setup_loop(
        tmp_path,
        runner_func=runner,
        jev=jev,
        mode="auto",
        verification_engine=v_engine,
    )

    snap = await loop.step(mid)
    # Fail-closed: escalated or blocked, NEVER accepted
    assert snap["status"] in ("WAITING_EXTERNAL_SUPERVISOR", "BLOCKED")
    m = store.load(mid)
    assert m is not None and m.status != MissionStatus.accepted


# ── Case F: Worker Quota Exhaustion & Re-route (No Silent Substitution) ────────
@pytest.mark.asyncio
async def test_case_f_worker_quota_exhausted_planning(tmp_path: Path) -> None:
    """Health registry marks Codex unavailable; planner perceives it and avoids silent substitution."""
    registry = ExecutorHealthRegistry()
    registry.record_failure(
        "codex",
        failure_class=ProviderFailureClass.PROVIDER_UNAVAILABLE,
        detail="429 quota exhausted",
    )
    assert registry.get_health("codex") == ExecutorHealth.UNAVAILABLE

    class MockMission:
        goal = "Write backend service"
        mission_id = "m-quota"

    # LLM returning plan with codex
    async def mock_llm(_msgs: list[dict[str, str]]) -> str:
        return '{"subtasks":[{"id":"t1","goal":"write api","worker":"codex","dependencies":[]}]}'

    # When codex is marked unavailable in health registry, decompose must fail or omit it
    with pytest.raises(Exception) as exc_info:
        await decompose(
            MockMission(),
            workspace=str(tmp_path),
            available_workers=["codex", "opencode", "pi"],
            health_registry=registry,
            llm=mock_llm,
        )
    assert "unavailable worker 'codex'" in str(exc_info.value) or "re-route/replan required" in str(
        exc_info.value
    )


# ── Case G: Mid-Mission Interruption & Resume (No Duplicate Side Effects) ──────
@pytest.mark.asyncio
async def test_case_g_interruption_and_resume_recovery(tmp_path: Path) -> None:
    """Interrupted execution resumes without repeating completed subtasks."""
    subtask1 = Subtask(task_id="sub1", objective="init db", worker="opencode")
    subtask2 = Subtask(
        task_id="sub2", objective="migrate db", worker="opencode", depends_on=("sub1",)
    )

    side_effects: list[str] = []

    async def dispatch(s: Subtask) -> SubtaskResult:
        side_effects.append(s.task_id)
        return SubtaskResult(task_id=s.task_id, worker=s.worker, status="COMPLETED")

    # Step 1: Run only subtask1
    res1 = await dispatch(subtask1)
    assert side_effects == ["sub1"]

    # Step 2: Resume with completed_subtasks containing sub1
    completed_subtasks = {"sub1": res1}
    report = await run_plan(
        [subtask1, subtask2],
        dispatch,
        mission_id="m-resume",
        iteration=0,
        objective="setup db",
        completed_subtasks=completed_subtasks,
    )
    assert report.status == "completed"
    # sub1 was NOT re-dispatched!
    assert side_effects == ["sub1", "sub2"]
    assert side_effects.count("sub1") == 1  # ZERO duplicate side effects!


# ── Case H: Mission Cancel Propagation ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_h_cancel_propagation(tmp_path: Path) -> None:
    """Cancel propagation stops downstream wave dispatches (POST_CANCEL_DISPATCH=0)."""
    subtask1 = Subtask(task_id="sub1", objective="phase 1", worker="opencode")
    subtask2 = Subtask(task_id="sub2", objective="phase 2", worker="codex", depends_on=("sub1",))

    cancelled = False

    def is_cancelled() -> bool:
        return cancelled

    dispatched = []

    async def dispatch(s: Subtask) -> SubtaskResult:
        nonlocal cancelled
        dispatched.append(s.task_id)
        # Cancel triggers during subtask1
        cancelled = True
        return SubtaskResult(task_id=s.task_id, worker=s.worker, status="COMPLETED")

    report = await run_plan(
        [subtask1, subtask2],
        dispatch,
        mission_id="m-cancel",
        iteration=0,
        objective="two-phase task",
        is_cancelled=is_cancelled,
    )
    assert dispatched == ["sub1"]
    sub2_evidence = next(
        (
            item
            for item in report.runtime_evidence
            if item.get("subtask_id") == "sub2" and item.get("kind") == "l1_execution"
        ),
        None,
    )
    assert sub2_evidence is not None and sub2_evidence.get("status") == "CANCELLED"


# ── Case I: Plan Revision Lineage ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_i_plan_revision_lineage(tmp_path: Path) -> None:
    """Plan revision records full lineage and preserves immutable history."""
    store = MissionStore(tmp_path)
    router = SupervisionRouter(store)

    async def dummy_runner(m: Any) -> ExecutionReport:
        return ExecutionReport(
            mission_id=m.mission_id,
            iteration=0,
            objective=m.goal,
            status="failed",
            failures=[{"reason": "subtask crashed"}],
        )

    async def planner_v2(_m: Any) -> list[dict[str, Any]]:
        return [
            {"task_id": "task_replan_1", "objective": "new approach", "worker": "opencode"},
        ]

    loop = MissionLoop(
        store=store,
        router=router,
        runner=dummy_runner,
        planner=planner_v2,
    )
    mid = loop.facade().create(goal="Complex task", workspace=str(tmp_path)).mission_id
    m = store.load(mid)
    assert m is not None
    m.authority["plan"] = [{"task_id": "old_task_1", "worker": "codex"}]
    m.authority["plan_version"] = 1
    store.save(m)

    # Request plan revision
    review = SupervisorReview(
        mission_id=mid,
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        reason="worker failed, replanning",
        correction_scope="PLAN",
    )
    apply_review(store, m, review, iteration=0)

    # Step to execute plan preparation
    await loop.step(mid)
    updated_m = store.load(mid)
    assert updated_m is not None
    assert updated_m.authority["plan_version"] == 2
    assert "last_plan_revision" in updated_m.authority
    rev = updated_m.authority["last_plan_revision"]
    assert rev["previous_plan_version"] == 1
    assert rev["new_plan_version"] == 2
    assert rev["reason"] == "worker failed, replanning"
    assert "old_task_1" in rev["invalidated_tasks"]
    assert "task_replan_1" in rev["new_tasks"]


# ── Case J: Multi-Worker Concurrent L2 Execution ───────────────────────────────
@pytest.mark.asyncio
async def test_case_j_multi_worker_concurrency(tmp_path: Path) -> None:
    """L2 concurrently schedules 3+ distinct L1 workers safely without collision."""
    subtasks = [
        Subtask(task_id="t_agy", objective="antigravity work", worker="antigravity"),
        Subtask(task_id="t_codex", objective="codex work", worker="codex"),
        Subtask(task_id="t_hicode", objective="hicode work", worker="opencode"),
    ]

    active_executions: list[str] = []
    max_concurrent = 0

    async def dispatch(s: Subtask) -> SubtaskResult:
        nonlocal max_concurrent
        active_executions.append(s.worker)
        max_concurrent = max(max_concurrent, len(active_executions))
        await asyncio.sleep(0.02)
        active_executions.remove(s.worker)
        return SubtaskResult(
            task_id=s.task_id,
            worker=s.worker,
            status="COMPLETED",
            execution_id=f"exec-{s.worker}",
            evidence=[{"kind": "artifact", "relative_path": f"out/{s.worker}.txt"}],
        )

    report = await run_plan(
        subtasks,
        dispatch,
        mission_id="m-concurrent",
        iteration=0,
        objective="3-way parallel",
    )
    assert report.status == "completed"
    assert len(report.changes) == 3
    assert max_concurrent >= 2  # Concurrent execution occurred
    assert not report.failures
    assert not report.blocked_items
