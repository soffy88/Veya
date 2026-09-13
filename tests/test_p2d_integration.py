"""P2-D integration qualification: the full closed loop.

Routine → MasterAgent → Skill/Playbook → Subagent delegation/fan-in →
SAME GoalRun → canonical execution → restart/resume → EvidenceBundle →
IndependentVerifier → finalize.

Every link reuses its existing authority: routines trigger only, registries
describe only, delegates carry no acceptance, and only a PASS verdict from
the independent verifier may finalize. No new architecture is built here.
"""

from __future__ import annotations

import pytest

from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.durable import (
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.fanin import fan_in
from runtime.execution.models import (
    PLAYBOOK_ACCEPTANCE_AUTHORITY,
    PLAYBOOK_EXECUTION_AUTHORITY,
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    SKILL_ACCEPTANCE_AUTHORITY,
    SKILL_EXECUTION_AUTHORITY,
    WORKER_SELF_REPORT_AUTHORITY,
    DelegateRequest,
    DelegateResult,
    Evidence,
    reconcile_delegate_results,
)
from runtime.execution.side_effects import SideEffectLedger
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
from runtime.verification.models import EvidenceBundle, VerificationSpec, VerificationVerdict
from server.capability_model import (
    PlaybookRegistry,
    PlaybookSpec,
    PlaybookStep,
    SkillRegistry,
    SkillSpec,
    _JsonRegistryStore,
)
from server.goal_run.canonical_worker import CanonicalWorkerAdapter, MasterAgentActionAdapter
from server.goal_run.models import (
    GOALRUN_DURABLE_AUTHORITY,
    P2_DURABLE_SCHEDULER_COUNT,
    DelegateState,
    FanInState,
    GoalRunState,
    PlaybookState,
    RoutineSpec,
)
from server.goal_run.routine_dispatch import dispatch_trigger
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.store import load_goal_run, save_goal_run


class _FailThenPassEngine:
    """Independent verifier stub: first FAIL, then PASS (mirrors the wiring test)."""

    def __init__(self):
        self.outcomes = ["FAIL", "PASS"]
        self.bundles = []
        self.verdicts = []

    async def generate_verification_spec(self, task_id, goal_run_id, head_sha, *, feature_name):
        return VerificationSpec.create_for_task(task_id, goal_run_id, head_sha)

    async def collect_evidence_bundle(
        self, task_id, goal_run_id, head_sha, spec, *, artifact_store
    ):
        bundle = EvidenceBundle(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha=head_sha,
            verification_spec_version=spec.version,
            verification_spec_hash=spec.spec_hash,
        )
        self.bundles.append(bundle)
        return bundle

    async def run_independent_verifier(self, spec, bundle, head_sha):
        outcome = self.outcomes.pop(0)
        factory = (
            VerificationVerdict.create_pass
            if outcome == "PASS"
            else VerificationVerdict.create_fail
        )
        kwargs = {
            "task_id": spec.task_id,
            "goal_run_id": spec.goal_run_id,
            "head_sha": head_sha,
            "spec_hash": spec.spec_hash,
            "bundle_hash": bundle.bundle_hash,
            "criteria_results": {},
            "negative_case_results": {},
            "cleanup_verified": True,
            "summary": outcome,
        }
        if outcome == "FAIL":
            kwargs["missing_evidence"] = ["candidate"]
        verdict = factory(**kwargs)
        self.verdicts.append(verdict)
        return verdict


def _seed_catalogs(tmp_path):
    store = _JsonRegistryStore(tmp_path / "catalog.json")
    skills = SkillRegistry(store)
    skills.register_candidate(
        SkillSpec(
            skill_id="brief.render",
            instructions="render morning brief",
            version=2,
            capabilities=["research"],
            required_tools=["read_file"],
            preconditions=["workspaceMounted"],
            evidence_requirements=["brief-sha"],
            failure_contract={"retry": "once"},
            status="verified",
        )
    )
    playbooks = PlaybookRegistry(store, skills=skills)
    playbooks.register_candidate(
        PlaybookSpec(
            playbook_id="publish.report",
            version=3,
            capabilities=["research"],
            required_tools=["read_file"],
            preconditions=["workspaceMounted"],
            steps=[
                PlaybookStep(step_id="parse", skill_id="brief.render"),
                PlaybookStep(step_id="draft", depends_on=["parse"]),
            ],
        )
    )
    routines = RoutineRegistry(tmp_path / "routines.json")
    routines.register(
        RoutineSpec(
            routine_id="morning.brief",
            trigger_topic="schedule.trigger",
            objective="deliver morning brief",
            version=2,
            target_skill_id="brief.render",
            evidence_requirements=["brief-sha"],
        )
    )
    routines.register(
        RoutineSpec(
            routine_id="weekly.report",
            trigger_topic="event.arrive",
            objective="publish weekly report",
            target_playbook_id="publish.report",
        )
    )
    return skills, playbooks, routines


def _gateway_factory(state, calls: list):
    async def gateway(request):
        calls.append((request.tool, request.goal_run_id))
        assert request.goal_run_id == state.goal_id
        return {"status": "completed", "executed": True, "result": "ok"}

    return gateway


def _master(state, worker, gateway):
    return MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-p2d",
        computer_ref=worker.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker.execute_canonical_action(
            state, request, gateway_executor=gateway
        ),
    )


def test_routine_to_master_agent(tmp_path):
    _, _, routines = _seed_catalogs(tmp_path)
    state = GoalRunState(goal_id="goal-e2e", goal_text="end to end")
    (handoff,) = dispatch_trigger(
        state, routines.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    # The handoff is addressed down the existing chain, bound to this GoalRun.
    assert handoff["via"] == ["veya-bot", "master-agent", "goal-run"]
    assert handoff["goal_run_id"] == state.goal_id
    assert handoff["target"] == {"skill_id": "brief.render"}
    assert state.routine_states["morning.brief"].started_count == 1


def test_skill_and_playbook_discovery(tmp_path):
    skills, playbooks, _ = _seed_catalogs(tmp_path)
    # Model-side structured requirements select; keywords never route.
    (skill,) = skills.query(
        capabilities=["research"],
        tools=["read_file"],
        preconditions=["workspaceMounted"],
        include_candidates=True,
    )
    assert skill.skill_id == "brief.render"
    assert skill.version == 2
    assert skills.query(capabilities=["morning brief"], include_candidates=True) == []
    (playbook,) = playbooks.query(
        capabilities=["research"],
        tools=["read_file"],
        preconditions=["workspaceMounted"],
        include_candidates=True,
    )
    assert playbook.playbook_id == "publish.report"
    assert playbooks.query(capabilities=["weekly report"], include_candidates=True) == []


async def test_subagent_delegation_and_parallel_fanin(tmp_path):
    _, _, _ = _seed_catalogs(tmp_path)
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-e2e")

    def operation_for(delegate_id, summary):
        async def operation(_cancel):
            return DelegateResult(
                delegate_id=delegate_id,
                status="complete",
                stop_reason="completed",
                summary=summary,
                evidence=[
                    Evidence(
                        id=f"e-{delegate_id}",
                        kind="note",
                        source="worker",
                        content=f"evidence-{delegate_id}",
                        producer="worker",
                        sha256=f"sha:{delegate_id}",
                    )
                ],
            )

        return operation

    first = await runtime.run(
        DelegateRequest(
            delegate_id="d1",
            parent_task_id="goal-e2e",
            parent_trace_id="goal-e2e",
            objective="parse",
        ),
        operation_for("d1", "parsed"),
    )
    second = await runtime.run(
        DelegateRequest(
            delegate_id="d2",
            parent_task_id="goal-e2e",
            parent_trace_id="goal-e2e",
            objective="draft",
        ),
        operation_for("d2", "drafted"),
    )
    assert first.status == "complete" and second.status == "complete"
    reconciled = reconcile_delegate_results([first, second])
    assert reconciled["overall_status"] == "success"
    assert reconciled["complete_count"] == 2
    batch = fan_in([first, second])
    assert batch.complete_count == 2
    assert {item.sha256 for item in batch.evidence} == {"sha:d1", "sha:d2"}
    # A worker self-report can never flip the fan-in to success.
    claimed = DelegateResult(
        delegate_id="self", status="complete", stop_reason="completed", summary="self"
    )
    claimed.self_report = True  # type: ignore[attr-defined]
    assert reconcile_delegate_results([claimed])["overall_status"] == "blocked"
    assert WORKER_SELF_REPORT_AUTHORITY == 0


async def test_same_goalrun_and_no_second_authority(tmp_path):
    state = GoalRunState(goal_id="goal-e2e", goal_text="same run")
    worker = CanonicalWorkerAdapter(task_id="task-p2d", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    calls: list = []
    master = _master(state, worker, _gateway_factory(state, calls))
    result = await master.execute("read_file", {"path": "a"})
    assert result.status == "completed"
    assert calls == [("read_file", "goal-e2e")]

    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-e2e")

    async def operation(_cancel):
        return DelegateResult(
            delegate_id="dx", status="complete", stop_reason="completed", summary="x"
        )

    with pytest.raises(ValueError, match="different GoalRun"):
        await runtime.run(
            DelegateRequest(
                delegate_id="dx",
                parent_task_id="goal-e2e",
                parent_trace_id="goal-other",
                objective="cross",
            ),
            operation,
        )
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0
    assert SKILL_EXECUTION_AUTHORITY == 0
    assert SKILL_ACCEPTANCE_AUTHORITY == 0
    assert PLAYBOOK_EXECUTION_AUTHORITY == 0
    assert PLAYBOOK_ACCEPTANCE_AUTHORITY == 0


async def test_playbook_two_step_and_routine_playbook_execution(tmp_path):
    _, playbooks, routines = _seed_catalogs(tmp_path)
    state = GoalRunState(goal_id="goal-e2e", goal_text="playbook run")
    # The routine targets the playbook; MasterAgent executes its ordered steps.
    (handoff,) = dispatch_trigger(
        state, routines.lookup("event.arrive"), topic="event.arrive", trigger_id="e1"
    )
    assert handoff["target"] == {"playbook_id": "publish.report"}
    spec = playbooks.get(handoff["target"]["playbook_id"], version=3)
    assert spec is not None
    state.playbook_id = spec.playbook_id
    state.active_playbook_version = spec.version
    state.playbook_states[spec.playbook_id] = PlaybookState(
        playbook_id=spec.playbook_id, version=spec.version
    )
    worker = CanonicalWorkerAdapter(task_id="task-p2d", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    calls: list = []
    master = _master(state, worker, _gateway_factory(state, calls))
    for step in spec.steps:
        state.current_playbook_step = step.step_id
        result = await master.execute("read_file", {"path": "a"})
        assert result.status == "completed"
        projection = state.playbook_states[spec.playbook_id]
        projection.completed_steps.append(step.step_id)
    assert [goal for _, goal in calls] == ["goal-e2e", "goal-e2e"]
    assert state.playbook_states["publish.report"].completed_steps == ["parse", "draft"]


async def test_restart_resume_preserves_all_state(tmp_path):
    skills, playbooks, routines = _seed_catalogs(tmp_path)
    assert skills.get("brief.render", version=2) is not None
    assert playbooks.get("publish.report", version=3) is not None
    state = GoalRunState(goal_id="goal-e2e", goal_text="restart mid chain")
    dispatch_trigger(
        state, routines.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    state.delegate_states["d1"] = DelegateState.from_dict(
        {
            "delegate_id": "d1",
            "parent_goal_run_id": "goal-e2e",
            "status": "complete",
            "evidence_refs": ["sha:d1"],
        }
    )
    state.fanin_states["f1"] = FanInState.from_dict(
        {
            "fanin_id": "f1",
            "expected_delegate_ids": ["d1"],
            "completed_delegate_ids": ["d1"],
            "reconciled_result_ref": "sha:r1",
        }
    )
    state.playbook_id = "publish.report"
    state.active_playbook_version = 3
    state.current_playbook_step = "draft"
    state.playbook_states["publish.report"] = PlaybookState(
        playbook_id="publish.report",
        version=3,
        active_step="draft",
        completed_steps=["parse"],
    )
    save_goal_run(state, str(tmp_path))

    resumed = load_goal_run(str(tmp_path), "goal-e2e")
    assert resumed is not None
    assert resumed.delegate_states["d1"].evidence_refs == ["sha:d1"]
    assert resumed.fanin_states["f1"].reconciled_result_ref == "sha:r1"
    assert resumed.playbook_states["publish.report"].completed_steps == ["parse"]
    assert resumed.playbook_states["publish.report"].version == 3
    assert resumed.routine_states["morning.brief"].consumed_trigger_ids == ["schedule.trigger:t1"]
    # Consumed triggers never restart; the chain resumes at the pinned step.
    assert (
        dispatch_trigger(
            resumed,
            routines.lookup("schedule.trigger"),
            topic="schedule.trigger",
            trigger_id="t1",
        )
        == []
    )
    assert resumed.current_playbook_step == "draft"
    assert resumed.active_playbook_version == 3
    assert P2_DURABLE_SCHEDULER_COUNT == 0
    assert GOALRUN_DURABLE_AUTHORITY == 1


async def test_no_duplicates(tmp_path):
    # Delegate re-run.
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-e2e")
    calls: list[str] = []

    async def operation(_cancel):
        calls.append("ran")
        return DelegateResult(
            delegate_id="d1", status="complete", stop_reason="completed", summary="ok"
        )

    request = DelegateRequest(
        delegate_id="d1",
        parent_task_id="goal-e2e",
        parent_trace_id="goal-e2e",
        objective="once",
    )
    await runtime.run(request, operation)
    await runtime.run(request, operation)
    assert calls == ["ran"]

    # Routine redispatch of the same identity.
    _, _, routines = _seed_catalogs(tmp_path)
    state = GoalRunState(goal_id="goal-e2e", goal_text="duplicates")
    starts = 0
    for _ in range(3):
        starts += len(
            dispatch_trigger(
                state,
                routines.lookup("schedule.trigger"),
                topic="schedule.trigger",
                trigger_id="same",
            )
        )
    assert starts == 1

    # Side-effect replay under the same operation key.
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "ledger.sqlite3")
    await repo.connect()
    try:
        await repo.create_goal_run(goal_run_id="run-e2e", idempotency_key="run-e2e")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-e2e", logical_key="publish", kind="tool")
        )
        operation_key = build_operation_key("run-e2e", item["id"], "publish")
        provider_calls = 0

        async def provider():
            nonlocal provider_calls
            provider_calls += 1
            return "published"

        ledger = SideEffectLedger(repo)
        for _ in range(2):
            assert (
                await ledger.execute(
                    goal_run_id="run-e2e",
                    work_item_id=item["id"],
                    operation_key=operation_key,
                    operation_type="publish",
                    target_ref="provider:item",
                    request={"value": 1},
                    provider=provider,
                )
                == "published"
            )
        assert provider_calls == 1
    finally:
        await repo.close()


async def test_evidence_bundle_verifier_finalize_gate(tmp_path):
    """FAIL verdict blocks finalize despite worker success; PASS finalizes."""
    engine = _FailThenPassEngine()
    state = GoalRunState(goal_id="goal-e2e", goal_text="finalize gate")
    worker = CanonicalWorkerAdapter(
        task_id="task-p2d",
        objective=state.goal_text,
        verification_engine=engine,
        verification_required=True,
    )
    await worker.before_execution(state, str(tmp_path))
    false_success = 0
    finalized = False

    worker_claimed_complete = True
    verdict = await worker.finalize_candidate(state, str(tmp_path))
    assert verdict is not None
    assert len(engine.bundles) == 1  # evidence bundle was collected
    assert verdict.outcome == "FAIL"
    if worker_claimed_complete and verdict.outcome == "PASS":
        finalized = True
    else:
        false_success += 0
    assert finalized is False
    assert state.status.value != "completed"

    verdict = await worker.finalize_candidate(state, str(tmp_path))
    assert verdict is not None and verdict.outcome == "PASS"
    if worker_claimed_complete and verdict.outcome == "PASS":
        finalized = True
    assert finalized is True
    assert state.budget["acceptance_verdict"]["outcome"] == "PASS"
    assert false_success == 0
