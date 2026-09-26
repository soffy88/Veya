from __future__ import annotations

import pytest

from runtime.doctrine import (
    ActionContext,
    AgentRoleContract,
    AutonomyDecision,
    AutonomyLevel,
    AutonomyPolicy,
    CorrectionRecord,
    PlaybookRule,
    RoutinePolicy,
    RoutineTelemetry,
    RuleCandidate,
    TeamPlaybook,
    anti_overfit,
    assert_pinned_versions,
    attach_role_contract,
    compose_context,
    enforce_role_action,
    generalize_correction,
    promote_skill,
)
from runtime.doctrine_store import DoctrineStore
from runtime.verification import (
    RequiredEvidence,
    VerificationFlow,
    VerificationGate,
    VerificationInvariant,
    VerificationProfile,
)
from veya.supervision.models import Mission, ReviewDecision, SupervisorReview
from veya.supervision.retask import plan_retask


def test_verification_profile_gate_requires_all_machine_proof() -> None:
    profile = VerificationProfile(
        id="vp-1",
        version="3",
        flows=[VerificationFlow(id="smoke")],
        invariants=[VerificationInvariant(id="no-failure", expression="failures == 0")],
        evidence_requirements=[
            RequiredEvidence(id="test-result", kind="test_result", description="tests", source="ci")
        ],
    )
    gate = VerificationGate(profile)
    failed = gate.evaluate(flows_run=[], invariant_results={}, proof_artifacts=[])
    assert not failed.passed
    assert "missing flow: smoke" in failed.unresolved_failures
    passed = gate.evaluate(
        flows_run=["smoke"],
        invariant_results={"no-failure": True},
        proof_artifacts=[{"id": "test-result", "type": "TEST_RESULT"}],
    )
    assert passed.passed
    assert passed.profile_version == "3"


def test_verification_spec_can_pin_profile_without_replacing_existing_spec() -> None:
    from runtime.verification import VerificationSpec

    spec = VerificationSpec.create_for_task(
        "task-1",
        "goal-1",
        "head",
        verification_profile_id="vp-1",
        verification_profile_version="3",
    )
    assert spec.verification_profile_id == "vp-1"
    assert spec.verify_immutable()


def test_mission_pins_and_old_documents_are_compatible() -> None:
    old = Mission.from_dict({"mission_id": "m-old", "goal": "inspect"})
    assert old.autonomy_level == "draft"
    assert old.autonomy_policy_version == "1.0"
    old.pin_doctrine(
        verification_profile_id="vp-1",
        verification_profile_version="3",
        autonomy_level="autopilot",
        autonomy_policy_version="2",
        role_id="builder",
        role_contract_version="4",
        playbook_id="team",
        playbook_version="7",
    )
    restored = Mission.from_dict(old.to_dict())
    assert restored.verification_profile_version == "3"
    assert restored.autonomy_level == "autopilot"
    assert restored.role_contract_version == "4"
    assert restored.playbook_version == "7"


def test_autonomy_is_orthogonal_and_hard_gates_win() -> None:
    policy = AutonomyPolicy(version="2", maximum_level=AutonomyLevel.full_autopilot)
    assert (
        policy.evaluate(AutonomyLevel.investigate, ActionContext("modify")) is AutonomyDecision.deny
    )
    assert (
        policy.evaluate(
            AutonomyLevel.full_autopilot,
            ActionContext("production_deploy", production_effect=True, verification_available=True),
        )
        is AutonomyDecision.require_human
    )
    assert policy.require_verification(AutonomyLevel.autopilot, False) is AutonomyLevel.draft
    assert (
        policy.downgrade(AutonomyLevel.full_autopilot, "verification failed")
        is AutonomyLevel.autopilot
    )
    with pytest.raises(ValueError):
        policy.downgrade(AutonomyLevel.autopilot, "")


def test_role_contract_extends_existing_registry_without_granting_tools() -> None:
    class ExistingRegistry:
        def __init__(self) -> None:
            self.entries = {"builder": {"name": "builder", "func": object()}}

        def get(self, type_: str, name: str):
            return self.entries.get(name) if type_ == "agent" else None

    registry = ExistingRegistry()
    role = AgentRoleContract(
        role_id="builder",
        owns=("implementation",),
        does_not_own=("production deployment",),
        allowed_tools=("read_repo",),
    )
    attach_role_contract(registry, role)
    assert registry.entries["builder"]["role_contract"] == role
    assert registry.entries["builder"].get("allowed_tools") is None
    assert role.scope_violation("production deployment")
    with pytest.raises(PermissionError, match="ROLE_SCOPE_VIOLATION"):
        enforce_role_action(
            role,
            responsibility="production deployment",
            tool_name="read_repo",
            tool_authorized=True,
        )
    with pytest.raises(PermissionError, match="TOOL_AUTHORITY_DENIED"):
        enforce_role_action(
            role,
            responsibility="implementation",
            tool_name="write_repo",
            tool_authorized=True,
        )


def test_playbook_is_version_pinned_and_composed_by_reference() -> None:
    playbook = TeamPlaybook(
        playbook_id="team",
        version="3",
        rules=(PlaybookRule("r1", "verify before close", enforcement="RUNTIME_GATE"),),
    )
    role = AgentRoleContract(role_id="reviewer", version="8")
    context = compose_context(playbook=playbook, role=role)
    assert context.playbook_version == "3"
    assert context.rule_ids == ("r1",)
    assert context.role_contract_version == "8"
    assert_pinned_versions(expected={"playbook_version": "3"}, actual={"playbook_version": "3"})
    with pytest.raises(RuntimeError, match="POLICY_VERSION_CONFLICT"):
        assert_pinned_versions(expected={"playbook_version": "3"}, actual={"playbook_version": "4"})


def test_correction_candidate_anti_overfit_and_skill_promotion() -> None:
    assert not anti_overfit("When file X failed on Sept 21")[0]
    assert anti_overfit("Always collect machine proof before acceptance")[0]
    candidate = RuleCandidate(
        id="skill-1",
        source_corrections=("c1",),
        proposed_principle="Collect machine proof before acceptance",
        suggested_target="skill",
    )
    with pytest.raises(ValueError, match="PROMOTION_DENIED"):
        promote_skill(
            candidate,
            successful_executions=0,
            has_verification=True,
            has_error_handling=True,
            has_approval_boundary=True,
            has_io_contract=True,
        )
    skill = promote_skill(
        candidate,
        successful_executions=1,
        has_verification=True,
        has_error_handling=True,
        has_approval_boundary=True,
        has_io_contract=True,
    )
    assert skill.id == "skill-1"
    candidate_from_correction = generalize_correction(
        [
            CorrectionRecord(
                id="c1",
                mission_id="m1",
                execution_id="e1",
                category="verification",
                observed_failure="missing proof",
                correction="collect proof",
            )
        ],
        candidate_id="candidate-1",
        proposed_principle="Collect machine proof before acceptance",
    )
    assert candidate_from_correction.source_corrections == ("c1",)
    with pytest.raises(ValueError, match="OVERFIT_RULE"):
        promote_skill(
            RuleCandidate(
                id="bad",
                source_corrections=("c1",),
                proposed_principle="When file X failed on Sept 21",
                suggested_target="skill",
            ),
            successful_executions=1,
            has_verification=True,
            has_error_handling=True,
            has_approval_boundary=True,
            has_io_contract=True,
        )


def test_routine_requires_reason_for_schedule_and_is_silent_on_noop() -> None:
    with pytest.raises(ValueError):
        RoutinePolicy("r", "role", "schedule").validate()
    RoutinePolicy("r", "role", "event").validate()
    RoutinePolicy(
        "r2", "role", "schedule", event_source_available=False, schedule_reason="legacy source"
    ).validate()
    telemetry = RoutineTelemetry(runs=4, noop_runs=3, estimated_cost=2.0, useful_outputs=1)
    assert telemetry.noop_ratio == 0.75
    assert telemetry.cost_per_useful_output == 2.0


def test_doctrine_records_survive_restart(tmp_path) -> None:
    store = DoctrineStore(tmp_path)
    correction = {
        "id": "c1",
        "mission_id": "m1",
        "execution_id": "e1",
        "category": "verification",
        "observed_failure": "missing proof",
        "correction": "collect proof",
    }
    store.append_correction(correction)
    store.save_routine_state("r1", {"cursor": "e7", "runs": 2})
    restarted = DoctrineStore(tmp_path)
    assert restarted.list_records("corrections")[0]["id"] == "c1"
    assert restarted.load_routine_state("r1")["cursor"] == "e7"


def test_pinned_mission_cannot_accept_without_verification_gate() -> None:
    mission = Mission(mission_id="m1", goal="build")
    mission.pin_doctrine(verification_profile_id="vp-1", verification_profile_version="1")
    review = SupervisorReview(
        mission_id="m1", iteration=0, supervisor="internal", decision=ReviewDecision.accept
    )
    outcome = plan_retask(review, mission=mission)
    assert outcome.mission_status.value == "BLOCKED"
    assert "verification gate" in outcome.reason
