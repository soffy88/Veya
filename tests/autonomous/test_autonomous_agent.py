"""Comprehensive tests for Veya Autonomous Agent V1 (spec §0-§55)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from cli.autonomous_cli import run_autonomous_cli
from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousDecision,
    AutonomousPlannerAdapter,
    AutonomousRiskGate,
    AutonomousState,
    AutonomousStatus,
    BudgetController,
    CompletionGate,
    DecisionPreconditions,
    DecisionType,
    EscalationManager,
    EscalationReason,
    NoProgressDetector,
    Observation,
    ObservationJournal,
    ObservationSource,
    ObservationStatus,
    OscillationDetector,
    OutcomeEvaluator,
    OutcomeVerdict,
    ProgressAssessment,
    RiskLevel,
    SituationAssessor,
    WaitConditionManager,
    WaitType,
    reconcile_context,
)


def test_models_serialization() -> None:
    state = AutonomousState(
        mission_id="m1",
        goal_run_id="gr1",
        cycle_id="c1",
        state=AutonomousStatus.ACTING,
        objective="Test objective",
    )
    d = state.to_dict()
    assert d["mission_id"] == "m1"
    assert d["state"] == "ACTING"
    loaded = AutonomousState.from_dict(d)
    assert loaded.state == AutonomousStatus.ACTING
    assert loaded.objective == "Test objective"

    dec = AutonomousDecision(
        decision_id="d1",
        mission_id="m1",
        decision_type=DecisionType.ACT,
        reason="Test reason",
    )
    d_dict = dec.to_dict()
    assert d_dict["decision_type"] == "ACT"
    loaded_dec = AutonomousDecision.from_dict(d_dict)
    assert loaded_dec.decision_type == DecisionType.ACT


def test_observation_journal_and_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        jfile = Path(tmpdir) / "journal.jsonl"
        journal = ObservationJournal(jfile)

        obs1 = journal.append(
            mission_id="m1",
            source=ObservationSource.USER,
            source_ref="user",
            kind="GOAL",
            summary="Deploy system",
            dedup_key="key1",
        )
        assert obs1.observation_id.startswith("obs_")

        # Duplicate dedup_key returns existing
        obs1_dup = journal.append(
            mission_id="m1",
            source=ObservationSource.USER,
            source_ref="user",
            kind="GOAL",
            summary="Deploy system duplicate",
            dedup_key="key1",
        )
        assert obs1_dup.observation_id == obs1.observation_id

        # Mark stale
        obs2 = journal.append(
            mission_id="m1",
            source=ObservationSource.TOOL,
            source_ref="probe",
            kind="PORT_SCAN",
            summary="Port 80 closed",
            status=ObservationStatus.STALE,
        )

        obs3 = journal.append(
            mission_id="m1",
            source=ObservationSource.EXECUTION,
            source_ref="runner",
            kind="CHECK",
            summary="Port 80 open",
            status=ObservationStatus.CONFLICTING,
        )

        reconciled = reconcile_context([obs1, obs2, obs3])
        assert len(reconciled.current) == 1
        assert len(reconciled.stale) == 1
        assert len(reconciled.conflicting) == 1


def test_situation_assessor() -> None:
    assessor = SituationAssessor()
    obs1 = Observation(
        observation_id="o1",
        mission_id="m1",
        source=ObservationSource.TOOL,
        source_ref="t1",
        kind="STATUS",
        summary="Service is running",
        status=ObservationStatus.CURRENT,
    )
    obs2 = Observation(
        observation_id="o2",
        mission_id="m1",
        source=ObservationSource.TOOL,
        source_ref="t2",
        kind="RISK",
        summary="Disk usage 95%",
        status=ObservationStatus.CURRENT,
    )
    reconciled = reconcile_context([obs1, obs2])
    assessment = assessor.assess(
        mission_id="m1",
        cycle_id="c1",
        reconciled_context=reconciled,
        active_goals=["Maintain service"],
        blocking_conditions=["DISK_FULL"],
    )
    assert len(assessment.known_facts) == 1
    assert "DISK_FULL" in assessment.blocking_conditions
    assert len(assessment.risks) >= 1


def test_decision_preconditions() -> None:
    pre = DecisionPreconditions()
    # Fails if objective is empty
    ok, errs = pre.check_preconditions(DecisionType.ACT, objective_valid=False)
    assert not ok
    assert "OBJECTIVE_INVALID" in errs

    # Fails if budget not available
    ok, errs = pre.check_preconditions(DecisionType.ACT, budget_available=False)
    assert not ok
    assert "BUDGET_EXHAUSTED" in errs

    # COMPLETE fails without required evidence
    ok, errs = pre.check_preconditions(DecisionType.COMPLETE, required_evidence_available=False)
    assert not ok
    assert "REQUIRED_EVIDENCE_UNAVAILABLE" in errs


def test_reconciler_and_replan_preserves_progress() -> None:
    from veya.autonomous.reconciler import GoalReconciler

    reconciler = GoalReconciler()
    # Retask
    retask = reconciler.retask_subtask(
        mission_id="m1",
        goal_run_id="gr1",
        subtask_id="st1",
        decision_id="dec1",
        reason="Runner failed with OOM",
        previous_executor="standard",
        new_executor="high_memory",
        previous_instructions="Run build",
        new_instructions="Run build with 16GB heap",
    )
    assert retask.new_attempt["executor"] == "high_memory"

    # Replan preserving progress
    replan = reconciler.replan(
        mission_id="m1",
        goal_run_id="gr1",
        decision_id="dec2",
        preserved_progress=["Step 1 passed", "Step 2 passed"],
        invalidated_assumptions=["Docker daemon accessible"],
        cancelled_future_steps=["Run docker build"],
        new_goal_graph=[{"goal": "Build without docker"}],
    )
    assert len(replan.preserved_progress) == 2
    assert "Docker daemon accessible" in replan.invalidated_assumptions

    # Mission revision
    rev = reconciler.revise_mission(
        mission_id="m1",
        new_objective="Build static binary instead of container",
        reason="Docker unavailable",
    )
    assert rev.objective == "Build static binary instead of container"
    assert rev.parent_revision_id is None


def test_evaluator_and_completion_gate() -> None:
    evaluator = OutcomeEvaluator()
    # Exit code non-zero -> REJECT
    res_fail = evaluator.evaluate(
        mission_id="m1",
        action_id="a1",
        expected_result="Test passed",
        execution_output="AssertionError",
        exit_code=1,
    )
    assert res_fail.verdict == OutcomeVerdict.REJECT

    # Exit code 0 alone with no evidence -> NEEDS_MORE_EVIDENCE (exit 0 alone never completes)
    res_no_ev = evaluator.evaluate(
        mission_id="m1",
        action_id="a1",
        expected_result="Test passed",
        execution_output="ok",
        exit_code=0,
        evidence_refs=[],
    )
    assert res_no_ev.verdict == OutcomeVerdict.NEEDS_MORE_EVIDENCE

    # Exit code 0 with evidence -> ACCEPT
    res_ok = evaluator.evaluate(
        mission_id="m1",
        action_id="a1",
        expected_result="Test passed",
        execution_output="ok",
        exit_code=0,
        evidence_refs=["receipt_123"],
    )
    assert res_ok.verdict == OutcomeVerdict.ACCEPT

    # Completion gate
    gate = CompletionGate()
    prog_inc = ProgressAssessment(
        progress_id="p1",
        mission_id="m1",
        goal_run_id="gr1",
        objective_coverage=0.5,
    )
    ok, dec, msg = gate.check_completion("m1", "Deploy", prog_inc, evidence_refs=["e1"])
    assert not ok
    assert "INCOMPLETE_COVERAGE" in msg

    prog_full = ProgressAssessment(
        progress_id="p2",
        mission_id="m1",
        goal_run_id="gr1",
        objective_coverage=1.0,
    )
    ok, dec, msg = gate.check_completion("m1", "Deploy", prog_full, evidence_refs=["e1"])
    assert ok
    assert dec is not None
    assert dec.mission_id == "m1"


def test_no_progress_and_oscillation_detectors() -> None:
    npd = NoProgressDetector(repeat_threshold=3)
    npd.record_step("EXECUTE", "Error 404", 0)
    assert not npd.check().has_no_progress
    npd.record_step("EXECUTE", "Error 404", 0)
    assert not npd.check().has_no_progress
    npd.record_step("EXECUTE", "Error 404", 0)
    sig = npd.check()
    assert sig.has_no_progress
    assert sig.recommended_decision in (DecisionType.REPLAN, DecisionType.RETASK)

    # Oscillation 2-cycle A-B-A-B
    od = OscillationDetector(window_size=8)
    od.record_signature("PLAN_A")
    od.record_signature("PLAN_B")
    od.record_signature("PLAN_A")
    od.record_signature("PLAN_B")
    osc_sig = od.check()
    assert osc_sig.is_oscillating
    assert "OSCILLATION_DETECTED" in osc_sig.reason


def test_wait_and_escalation_managers() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        wfile = Path(tmpdir) / "waits.jsonl"
        efile = Path(tmpdir) / "escalations.jsonl"

        wm = WaitConditionManager(wfile)
        wcond = wm.register_wait(
            mission_id="m1",
            wait_type=WaitType.EVENT,
            predicate="BUILD_COMPLETE",
        )
        assert not wm.check_all_satisfied()["m1"][0][1]
        wm.resolve_wait(wcond.condition_id)
        assert wm.check_all_satisfied()["m1"][0][1]

        em = EscalationManager(efile)
        req = em.escalate(
            mission_id="m1",
            decision_id="d1",
            reason=EscalationReason.RISK_THRESHOLD,
            question="Proceed with production deployment?",
        )
        assert not req.resolved
        ok = em.resolve_escalation(req.escalation_id, "Approved by owner")
        assert ok
        assert em.get_escalation(req.escalation_id).resolved


def test_risk_gate_and_budget() -> None:
    rg = AutonomousRiskGate()
    lvl, _rsn = rg.assess_action("COMMAND", {"command": "rm -rf /"})
    assert lvl == RiskLevel.REQUIRES_OWNER

    bc = BudgetController(max_cost=10.0, max_actions=2)
    assert bc.can_execute()
    bc.consume(action_cost=5.0)
    assert bc.can_execute()
    bc.consume(action_cost=5.0)
    assert not bc.can_execute()


def test_planner_adapter() -> None:
    adapter = AutonomousPlannerAdapter()
    proposal = adapter.propose_plan("m1", "Build web app")
    assert proposal.mission_id == "m1"
    assert len(proposal.goals) >= 1
    valid, _msg = adapter.evaluate_proposal(proposal)
    assert valid

    accepted = adapter.accept_proposal(
        proposal, modifications={"assumptions": ["Updated assumption"]}
    )
    assert accepted.assumptions == ["Updated assumption"]


def test_autonomous_cycle_step_and_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="test_mission_1",
            objective="Deploy microservice",
            base_dir=tmpdir,
        )

        def mock_executor(action: ActionProposal) -> tuple[int, str, list[str]]:
            return 0, "Build and deployment succeeded", ["evidence_receipt_1"]

        # Step 1: Execute step towards objective
        st1 = cycle.step(executor=mock_executor)
        assert len(st1.accepted_progress) == 1
        assert cycle.status in (
            AutonomousStatus.ACTING,
            AutonomousStatus.VERIFYING,
            AutonomousStatus.PLANNING,
            AutonomousStatus.OBSERVING,
        )

        # Step 2: Second step completes coverage
        st2 = cycle.step(executor=mock_executor)
        assert len(st2.accepted_progress) == 2

        # Step 3: Complete mission
        st3 = cycle.step()
        assert st3.state == AutonomousStatus.COMPLETED
        assert cycle.completion_decision is not None


def test_autonomous_cli() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        old_root = os.environ.get("VEYA_PROJECT_ROOT")
        os.environ["VEYA_PROJECT_ROOT"] = tmpdir
        try:
            cycle = AutonomousCycle(
                mission_id="cli_mission",
                objective="CLI Verification",
                base_dir=tmpdir,
            )
            cycle.step()

            # Status
            assert run_autonomous_cli(["status", "cli_mission", "--json"]) == 0
            # Observations
            assert run_autonomous_cli(["observations", "cli_mission", "--json"]) == 0
            # Decisions
            assert run_autonomous_cli(["decisions", "cli_mission", "--json"]) == 0
            # Progress
            assert run_autonomous_cli(["progress", "cli_mission", "--json"]) == 0
            # Waits
            assert run_autonomous_cli(["waits", "cli_mission", "--json"]) == 0
            # Escalations
            assert run_autonomous_cli(["escalations", "cli_mission", "--json"]) == 0
        finally:
            if old_root is not None:
                os.environ["VEYA_PROJECT_ROOT"] = old_root
            else:
                os.environ.pop("VEYA_PROJECT_ROOT", None)
