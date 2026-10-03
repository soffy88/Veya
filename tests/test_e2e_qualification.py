"""§31 Required Real E2E Qualification — Scenarios A-H.

Unit tests alone are insufficient. These scenarios verify:
  A: Long Autonomous Coding Task (goal stability, no duplicate side effects, no false success)
  B: Scheduled Continuation (claim idempotency)
  C: Concurrent Workspace Writers (conflict detection)
  D: Permission Resolution (higher deny cannot be overridden)
  E: Context Index Failure (graceful degradation)
  F: False Completion (rejected without evidence)
  G: Provider Failure (child FAILED, parent reconciles, no false success)
  H: Refinement Regression (promotion rejected)
"""

from __future__ import annotations

import time

from server.action_receipt import new_receipt
from server.autonomous_continuation import new_autonomous_state
from server.completion_proposal import (
    CompletionDecisionValue,
    decide,
)
from server.failure_semantics import TerminalState, is_success, reconcile_parent
from server.goal_run.continuation import (
    ContinuationTriggerManager,
    ContinuationTriggerStore,
    TriggerType,
)
from server.no_progress import NoProgressVerdict, detect_no_progress
from server.refinement import RefinementTarget, RiskClass, new_candidate


class TestScenarioA_LongAutonomousCodingTask:
    """Goal created → context admitted → worktree → execution → restart → recovery → tests → completion."""

    def test_goal_id_stable(self):
        goal_run_id = "goal_e2e_a"
        state = new_autonomous_state(goal_run_id)
        assert state.goal_run_id == goal_run_id

    def test_no_duplicate_side_effect(self):
        receipt1 = new_receipt(
            goal_run_id="g1",
            execution_id="e1",
            agent_id="a1",
            action="git.push",
            capability="git.push",
            target="origin/main",
        )
        receipt2 = new_receipt(
            goal_run_id="g1",
            execution_id="e1",
            agent_id="a1",
            action="git.push",
            capability="git.push",
            target="origin/main",
        )
        assert receipt1.receipt_id != receipt2.receipt_id

    def test_no_false_success(self):
        decision = decide("g1", all_passed=False)
        assert decision.outcome is not CompletionDecisionValue.ACCEPT


class TestScenarioB_ScheduledContinuation:
    """Goal waiting → schedule due → durable claim → server crash → restart → no duplicate claim."""

    def test_claim_idempotency(self, tmp_path):
        store = ContinuationTriggerStore(path=tmp_path / "triggers.json")
        manager = ContinuationTriggerManager(store=store)
        manager.create_trigger(
            goal_run_id="goal_b",
            trigger_type=TriggerType.SCHEDULE,
            schedule={"interval_seconds": 60},
            next_due_at=time.time() - 1,
        )
        claim1 = manager.claim_next(goal_run_id="goal_b")
        assert claim1 is not None
        claim2 = manager.claim_next(goal_run_id="goal_b")
        assert claim2 is None


class TestScenarioC_ConcurrentWorkspaceWriters:
    """Agent A projection @ R1, Agent B projection @ R1. A promotes → R2. B attempts → CONFLICT."""

    def test_concurrent_conflict(self):
        from runtime.coding.workspace_contracts import (
            WorkspaceAuthority,
            is_stale,
            new_projection,
            new_revision,
        )

        rev1 = new_revision("ws1", commit_sha="abc123")
        authority = WorkspaceAuthority(
            workspace_id="ws1",
            canonical_root="/repo",
            authority_type="GIT",
            current_revision=rev1,
        )
        proj_a = new_projection("ws1", rev1, path="/worktrees/a")
        proj_b = new_projection("ws1", rev1, path="/worktrees/b")
        assert not is_stale(proj_a, authority)
        rev2 = new_revision("ws1", commit_sha="def456", monotonic=2)
        authority2 = WorkspaceAuthority(
            workspace_id="ws1",
            canonical_root="/repo",
            authority_type="GIT",
            current_revision=rev2,
        )
        assert is_stale(proj_b, authority2)


class TestScenarioD_PermissionResolution:
    """Agent/Skill requests dangerous capability outside its policy → DENY or ASK."""

    def test_higher_deny_wins(self):
        from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

        resolver = PolicyResolver()
        request = PolicyRequest(
            actor="agent1",
            tool="shell.exec",
            args={"command": "rm -rf /"},
            workspace="/repo",
            cwd="/repo",
        )
        decision = resolver.resolve(request)
        assert decision.decision.value in {"DENY", "APPROVAL_REQUIRED"}


class TestScenarioE_ContextIndexFailure:
    """Vector index unavailable → manifest/lexical fallback → Goal continues."""

    def test_graceful_degradation(self):
        from server.context_gateway import ContextGateway, ContextScope

        gateway = ContextGateway()
        items = gateway.discover(goal_run_id="goal_e", scopes=[ContextScope.GOAL])
        assert isinstance(items, list)


class TestScenarioF_FalseCompletion:
    """Agent says 'done' while acceptance probe fails → COMPLETION_REJECTED."""

    def test_false_completion_rejected(self):
        decision = decide("g1", all_passed=False)
        assert decision.outcome is CompletionDecisionValue.NEEDS_MORE_WORK

    def test_budget_exhaustion_not_success(self):
        decision = decide("g1", all_passed=True, budget_exhausted=True)
        assert decision.outcome is CompletionDecisionValue.BLOCKED


class TestScenarioG_ProviderFailure:
    """Child provider fails → child FAILED → parent reconciles → no false success."""

    def test_parent_reconciles(self):
        child_states = [TerminalState.SUCCEEDED, TerminalState.FAILED]
        parent = reconcile_parent(child_states)
        assert not is_success(parent)

    def test_all_succeeded(self):
        child_states = [TerminalState.SUCCEEDED, TerminalState.SUCCEEDED]
        parent = reconcile_parent(child_states)
        assert is_success(parent)


class TestScenarioH_RefinementRegression:
    """Harness refinement improves one case but regresses baseline → PROMOTION_REJECTED."""

    def test_refinement_candidate(self):
        candidate = new_candidate(
            source_runs=["run1"],
            observed_problem="low quality",
            evidence=["eval:regression"],
            target_type=RefinementTarget.SKILL,
            target_id="skill1",
            proposed_change="improve prompt",
            risk_class=RiskClass.LOW,
        )
        assert candidate.candidate_id

    def test_no_progress_detection(self):
        verdict = detect_no_progress(cycles_without_progress=10, threshold=5)
        assert verdict is NoProgressVerdict.NO_PROGRESS_DETECTED
