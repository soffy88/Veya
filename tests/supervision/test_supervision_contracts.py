"""Canonical supervision contracts: one Mission, one state machine, one review."""

from __future__ import annotations

from veya.supervision import (
    TERMINAL_STATUSES,
    EscalationCode,
    ExecutionReport,
    LineageEntry,
    Mission,
    MissionBudget,
    MissionStatus,
    ReviewDecision,
    SupervisionMode,
    SupervisorReview,
    classify_escalation,
    fallback_mode,
    preferred_mode,
    requires_owner,
    switch_direction,
)


def test_mission_roundtrip_preserves_contract() -> None:
    mission = Mission(
        mission_id="m-1",
        goal="ship the thing",
        supervision_mode=SupervisionMode.auto,
        acceptance_criteria=["tests pass"],
        workspace="/repo",
        budget=MissionBudget(max_iterations=3, max_jev_calls=5),
    )
    restored = Mission.from_dict(mission.to_dict())
    assert restored.mission_id == "m-1"
    assert restored.supervision_mode is SupervisionMode.auto
    assert restored.acceptance_criteria == ["tests pass"]
    assert restored.budget.max_iterations == 3


def test_single_state_machine_has_all_required_states() -> None:
    expected = {
        "CREATED",
        "ROUTING_SUPERVISOR",
        "DESIGNING",
        "PLANNING",
        "EXECUTING",
        "COLLECTING_EVIDENCE",
        "FAST_DECISION",
        "REVIEWING",
        "RETASKING",
        "ACCEPTED",
        "DONE",
        "BLOCKED",
        "WAITING_EXTERNAL_SUPERVISOR",
        "WAITING_OWNER",
        "FAILED",
        "CANCELLED",
    }
    assert {str(s) for s in MissionStatus} == expected
    assert {str(s) for s in TERMINAL_STATUSES} == {"DONE", "FAILED", "CANCELLED"}


def test_review_decision_is_the_only_action_vocabulary() -> None:
    assert {str(d) for d in ReviewDecision} == {
        "ACCEPT",
        "CONTINUE",
        "REVISE",
        "RETRY",
        "ROLLBACK",
        "ESCALATE",
        "DONE",
    }


def test_execution_report_and_review_roundtrip() -> None:
    report = ExecutionReport(
        mission_id="m-1",
        iteration=2,
        objective="fix bug",
        status="succeeded",
        tests=[{"name": "unit", "status": "passed"}],
        proposed_next_action="review",
    )
    assert ExecutionReport.from_dict(report.to_dict()).tests[0]["status"] == "passed"

    review = SupervisorReview(
        mission_id="m-1",
        iteration=2,
        supervisor="internal",
        decision=ReviewDecision.revise,
        next_task="add a regression test",
        required_evidence=["test output"],
    )
    restored = SupervisorReview.from_dict(review.to_dict())
    assert restored.decision is ReviewDecision.revise
    assert restored.next_task == "add a regression test"


def test_lineage_records_switch_semantics() -> None:
    entry = LineageEntry(
        iteration=3,
        from_supervisor="external",
        to_supervisor="internal",
        reason="design frozen",
        trigger="design_frozen",
    )
    assert entry.to_dict()["from"] == "external"
    assert entry.to_dict()["to"] == "internal"


# ── escalation policy (spec §23) ────────────────────────────────────────
def test_ordinary_engineering_failures_never_escalate() -> None:
    for trigger in (
        "test_failure",
        "lint_failure",
        "build_failure",
        "implementation_bug",
        "provider_transient_error",
        "known_migration_issue",
        "retryable_deployment_failure",
    ):
        assert classify_escalation(trigger) is None
        assert requires_owner(trigger) is False


def test_owner_only_conditions_escalate() -> None:
    assert classify_escalation("credential") is EscalationCode.owner_credential_required
    assert classify_escalation("irreversible") is EscalationCode.irreversible_external_action
    assert classify_escalation("production_destructive") is (
        EscalationCode.production_destructive_action
    )
    assert classify_escalation("authority_conflict") is (
        EscalationCode.unresolvable_authority_conflict
    )


def test_unknown_trigger_does_not_bother_the_owner() -> None:
    assert classify_escalation("something-new") is None


# ── routing / switching (spec §12/§13) ─────────────────────────────────
def test_initial_mode_bias() -> None:
    assert preferred_mode(["architecture_redesign"]) == "external"
    assert preferred_mode(["known_bug", "test_lint_repair"]) == "internal"
    assert preferred_mode([]) == "internal"


def test_switch_direction_is_symmetric_and_bounded() -> None:
    assert switch_direction("architecture_ambiguity", "internal") == "external"
    assert switch_direction("design_frozen", "external") == "internal"
    assert switch_direction("design_frozen", "internal") is None
    assert switch_direction("nonsense", "internal") is None


def test_external_unavailable_waits_unless_policy_allows_fallback() -> None:
    assert fallback_mode("external", external_available=False) == "external"
    assert (
        fallback_mode(
            "external",
            external_available=False,
            policy={"external_fallback_to_internal": True},
        )
        == "internal"
    )
    assert fallback_mode("external", external_available=True) == "external"
