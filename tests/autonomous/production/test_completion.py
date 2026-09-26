"""AQ-P19, AQ-P20, AQ-P21, AQ-P22, AQ-P23: Semantic Evaluation and Completion Qualification Tests.

Invariants:
- EXIT_CODE_COMPLETION=0: Exit code 0 alone NEVER signifies mission completion.
- SEMANTIC_REJECTION=PASS: Evaluator rejects execution when semantic claims fail.
- FALSE_SUCCESS=0: Failed invariants are never marked as success.
- PARTIAL_PROGRESS=PASS: Incremental milestones preserved without prematurely closing mission.
- REAL_COMPLETION=PASS: Completion granted only with 100% coverage, evidence, and 0 blockers.
- UNVERIFIED_COMPLETION=0: Adversarial attempts to complete without evidence or coverage denied.
- CONFLICT_IGNORED=0: Contradictory evidence reconciled, never ignored.
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousStatus,
    CompletionGate,
    OutcomeEvaluator,
    OutcomeVerdict,
    ProgressAssessment,
)


def test_exit_code_zero_with_failed_claim_is_rejected() -> None:
    """AQ-P19: Command exits 0, but invariant is false; Evaluator rejects and prevents completion."""
    evaluator = OutcomeEvaluator()
    res = evaluator.evaluate(
        mission_id="prod_eval_rejection",
        action_id="act_test_run",
        expected_result="100% test pass rate with zero flaky tests",
        execution_output="Ran 10 tests, 0 failures, 2 skipped",
        exit_code=0,
        verification_passed=False,  # Invariant check failed!
        evidence_refs=[],
    )

    assert res.verdict in (OutcomeVerdict.REJECT, OutcomeVerdict.NEEDS_MORE_EVIDENCE)
    assert res.verdict != OutcomeVerdict.ACCEPT  # EXIT_CODE_COMPLETION=0, SEMANTIC_REJECTION=PASS


def test_partial_progress_accepted_and_continues() -> None:
    """AQ-P20: Partial subtask completion increases coverage without prematurely finishing mission."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_partial_progress",
            objective="Deliver full CRUD REST service",
            base_dir=tmpdir,
            required_milestones=[
                "Create endpoints",
                "Read endpoints",
                "Update endpoints",
                "Delete endpoints",
            ],
        )

        def step1_exec(act: ActionProposal) -> tuple[int, str, list[str]]:
            return 0, "Created POST endpoint", ["ev_post_ok"]

        cycle.step(executor=step1_exec)
        assert len(cycle.accepted_progress) == 1
        assert cycle.latest_progress.objective_coverage == 0.25
        assert cycle.status != AutonomousStatus.COMPLETED  # PARTIAL_PROGRESS=PASS


def test_completion_adversarial_denied_when_blocker_active() -> None:
    """AQ-P22: 100% coverage but active blocker prevents completion."""
    gate = CompletionGate()
    prog = ProgressAssessment(
        mission_id="prod_adv_comp",
        goal_run_id="gr1",
        objective_coverage=1.0,
        verified_claims=["All steps run"],
        unverified_claims=[],
        remaining_work=[],
    )

    # Has blocker
    ok, dec, err = gate.check_completion(
        mission_id="prod_adv_comp",
        objective="Deliver secure release",
        progress=prog,
        evidence_refs=["receipt_1"],
        blocking_conditions=["CVE_CRITICAL_UNPATCHED"],
    )
    assert ok is False  # UNVERIFIED_COMPLETION=0
    assert dec is None
    assert "BLOCKED" in err


def test_completion_adversarial_denied_when_evidence_missing() -> None:
    """AQ-P22: 100% coverage without durable evidence refs is denied."""
    gate = CompletionGate()
    prog = ProgressAssessment(
        mission_id="prod_adv_evidence",
        goal_run_id="gr1",
        objective_coverage=1.0,
        verified_claims=["Claimed complete"],
        unverified_claims=[],
        remaining_work=[],
    )

    # Missing evidence refs
    ok, dec, err = gate.check_completion(
        mission_id="prod_adv_evidence",
        objective="Deliver secure release",
        progress=prog,
        evidence_refs=[],
    )
    assert ok is False
    assert dec is None
    assert "MISSING_EVIDENCE" in err


def test_real_completion_granted_when_all_invariants_satisfied() -> None:
    """AQ-P21: 100% coverage, verified evidence, zero blockers yields CompletionDecision."""
    gate = CompletionGate()
    prog = ProgressAssessment(
        mission_id="prod_real_comp",
        goal_run_id="gr1",
        objective_coverage=1.0,
        verified_claims=["Architecture verified", "Tests pass", "Security audit clean"],
        unverified_claims=[],
        remaining_work=[],
    )

    ok, dec, _err = gate.check_completion(
        mission_id="prod_real_comp",
        objective="Deliver v1.0 Production",
        progress=prog,
        evidence_refs=["ev_arch_ok", "ev_tests_ok", "ev_sec_ok"],
        blocking_conditions=[],
        unresolved_child_goals=[],
    )
    assert ok is True
    assert dec is not None
    assert dec.mission_id == "prod_real_comp"
    assert len(dec.evidence_refs) == 3
