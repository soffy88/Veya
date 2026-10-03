"""P0/P1 named release gates as executable assertions.

The spec names its release gates as identifiers (e.g. ``WORKSPACE_REVISION=PASS``,
``FALSE_SUCCESS=0``). Previously these names existed only in prose, so nothing
could fail when a gate regressed. Each test here asserts one gate against real
implementation and test evidence, so a regression turns a gate red.

Gates whose contract is not yet implemented are asserted as ``xfail(strict=False)``
with the reason recorded, rather than being silently omitted or falsely claimed.
"""

from __future__ import annotations

import inspect

import pytest

from runtime.coding.workspace_contracts import (
    AuthorityType,
    ConflictClass,
    ConflictStrategy,
    ProjectionState,
    WorkspaceAuthority,
    classify_conflict,
    new_projection,
    new_revision,
    resolve_conflict,
)
from server.completion_proposal import CompletionDecisionValue, decide
from server.failure_semantics import TerminalState, is_success, reconcile_parent
from server.goal_run.continuation import (
    ContinuationTriggerManager,
    ContinuationTriggerStore,
    TriggerType,
)
from server.no_progress import NoProgressVerdict, detect_no_progress
from server.refinement import RefinementTarget, is_immutable_target

# ── INV / authority gates ────────────────────────────────────────────────


def test_SINGLE_GOAL_AUTHORITY():
    """GoalRun is the sole goal/execution authority; no forbidden engine exists."""
    from server.goal_run import runner  # noqa: F401

    forbidden = (
        "SecondGoalEngine",
        "PlanManager",
        "HookExecutionManager",
        "SchedulerExecutionManager",
        "TeamExecutionManager",
        "SkillExecutionManager",
        "BackgroundAgentExecutionManager",
    )
    import server.goal_run as pkg

    names = {n for n in dir(pkg) if not n.startswith("_")}
    assert not any(f in names for f in forbidden), sorted(names)


def test_SINGLE_EXECUTION_AUTHORITY():
    """DurableJobManager is a projection over GoalRun, never a rival authority."""
    from veya.remote.execution import DurableJobManager

    doc = (inspect.getdoc(DurableJobManager) or "").lower()
    assert "projection" in doc
    assert "goalrun" in doc


def test_SINGLE_SKILL_REGISTRY():
    """Exactly one canonical skill write authority is declared."""
    from server import skill_authority

    assert skill_authority.SKILL_AUTHORITY_ROLE_AUTHORITATIVE == "AUTHORITATIVE"
    assert skill_authority.CANONICAL_SKILL_AUTHORITY.endswith("VeyaSkillHub")


# ── config / policy gates ────────────────────────────────────────────────


def test_CONFIG_EXPLAIN():
    """explain() returns the full explainability contract."""
    from config.authority import explain

    out = explain("llm.provider")
    for field in ("effective_value", "effective_source", "precedence_chain", "reason"):
        assert field in out, field


def test_POLICY_EXPLAINABILITY():
    from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

    out = PolicyResolver().explain(
        PolicyRequest(actor="a", tool="file.read", args={}, workspace="/tmp", cwd="/tmp")
    )
    assert "decision" in out and "why" in out


def test_HIGHER_DENY_CANNOT_BE_OVERRIDDEN():
    from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

    decision = PolicyResolver().resolve(
        PolicyRequest(
            actor="a", tool="shell.exec", args={"command": "rm -rf /"}, workspace="/tmp", cwd="/tmp"
        ),
        consume_approval=False,
    )
    assert decision.decision.value in {"DENY", "APPROVAL_REQUIRED"}


def test_SERVER_ISSUED_APPROVAL_ONLY():
    """A client `approved=true` with no server approval_id is forgery."""
    from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

    decision = PolicyResolver().resolve(
        PolicyRequest(
            actor="a",
            tool="shell.exec",
            args={"command": "id"},
            workspace="/tmp",
            cwd="/tmp",
            client_approved=True,
        ),
        consume_approval=False,
    )
    assert decision.decision.value == "DENY"
    assert decision.constraining_layer == "client-approval-guard"


# ── lifecycle / workspace gates ──────────────────────────────────────────


def test_EVENT_SCHEMA_VERSIONED():
    from server.lifecycle_events import SCHEMA_VERSION, build_event, validate_lifecycle_event

    event = build_event("run.started", session_id="s1")
    assert event.schema_version == SCHEMA_VERSION
    validate_lifecycle_event(event)


def test_SSE_IS_PROJECTION():
    """SSE can never source a replay; the durable journal must."""
    from server.lifecycle_events import LifecycleEventBus

    bus = LifecycleEventBus.__new__(LifecycleEventBus)
    with pytest.raises(RuntimeError, match="forbidden"):
        bus.replay_from_sse_history(["id: x\ndata: {}\n\n"])


def test_WORKSPACE_REVISION():
    rev = new_revision("ws", commit_sha="abc", monotonic=3)
    assert rev.commit_sha == "abc" and rev.monotonic == 3


def test_WORKSPACE_PROJECTION():
    authority = WorkspaceAuthority(
        workspace_id="ws",
        canonical_root="/repo",
        authority_type=AuthorityType.GIT,
        current_revision=new_revision("ws", commit_sha="abc"),
    )
    projection = new_projection("ws", authority.current_revision, path="/wt")
    assert projection.state is ProjectionState.READY
    assert projection.base_revision.revision_id == authority.current_revision.revision_id


def test_CONCURRENT_WRITER_CONFLICT():
    """A projection based on a superseded revision is stale, never silently applied."""
    from runtime.coding.workspace_contracts import is_stale

    rev1 = new_revision("ws", commit_sha="a")
    WorkspaceAuthority(
        workspace_id="ws",
        canonical_root="/repo",
        authority_type=AuthorityType.GIT,
        current_revision=rev1,
    )
    stale_projection = new_projection("ws", rev1)
    moved = WorkspaceAuthority(
        workspace_id="ws",
        canonical_root="/repo",
        authority_type=AuthorityType.GIT,
        current_revision=new_revision("ws", commit_sha="b", monotonic=2),
    )
    assert is_stale(stale_projection, moved)


def test_SILENT_LAST_WRITE_WINS_is_zero():
    """A stale projection must never reconcile to MATCH/APPLIED."""
    from runtime.coding.workspace_contracts import reconcile_projection

    rev1 = new_revision("ws", commit_sha="a")
    projection = new_projection("ws", rev1)
    moved = WorkspaceAuthority(
        workspace_id="ws",
        canonical_root="/repo",
        authority_type=AuthorityType.GIT,
        current_revision=new_revision("ws", commit_sha="b", monotonic=2),
    )
    state, reason = reconcile_projection(projection, moved)
    assert state is not ProjectionState.READY
    assert reason.startswith("DIVERGED")


def test_CONFLICT_STRATEGY_MAPPING():
    assert resolve_conflict(ConflictClass.GIT_DIVERGENCE) is ConflictStrategy.REBASE
    assert resolve_conflict(ConflictClass.LEASE_CONFLICT) is ConflictStrategy.RETRY
    assert resolve_conflict(ConflictClass.AUTHORITY_CHANGED) is ConflictStrategy.HUMAN_REVIEW
    assert classify_conflict(git_diverged=True) is ConflictClass.GIT_DIVERGENCE


# ── completion / no-progress gates ───────────────────────────────────────


def test_FALSE_SUCCESS_is_zero():
    """No non-success input may yield ACCEPT, and budget exhaustion never does."""
    assert decide("g", all_passed=False).outcome is CompletionDecisionValue.NEEDS_MORE_WORK
    assert decide("g", all_passed=True, budget_exhausted=True).outcome is not (
        CompletionDecisionValue.ACCEPT
    )
    assert decide("g", all_passed=True, blockers=["x"]).outcome is not (
        CompletionDecisionValue.ACCEPT
    )


def test_PRODUCT_MAX_ROUNDS_TERMINATION_is_zero():
    """No product mainline module terminates on a fixed round count.

    Checked over the AST rather than raw text so that a docstring *describing*
    the removed behaviour (or naming the gate) is not mistaken for a live
    termination bound. Only real code references to `max_rounds` fail here.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for rel in ("server", "runtime", "veya"):
        for path in sorted((root / rel).rglob("*.py")):
            if "__pycache__" in path.parts or "3O" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
                    name = node.arg
                if name == "max_rounds":
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


def test_NO_PROGRESS_yields_BLOCKED_not_success():
    verdict = detect_no_progress(cycles_without_progress=99, threshold=5)
    assert verdict is NoProgressVerdict.NO_PROGRESS_DETECTED
    assert not is_success(TerminalState.BLOCKED)


def test_STALE_CHILD_DOES_NOT_YIELD_PARENT_SUCCESS():
    parent = reconcile_parent([TerminalState.SUCCEEDED, TerminalState.BUDGET_EXHAUSTED])
    assert parent is TerminalState.BUDGET_EXHAUSTED
    assert not is_success(parent)


# ── continuation / idempotency gates ─────────────────────────────────────


def test_SCHEDULE_CLAIM_IDEMPOTENCY(tmp_path):
    manager = ContinuationTriggerManager(
        store=ContinuationTriggerStore(path=tmp_path / "triggers.json")
    )
    manager.create_trigger(
        goal_run_id="g",
        trigger_type=TriggerType.SCHEDULE,
        schedule={"interval_seconds": 60},
        next_due_at=0.0,
    )
    assert manager.claim_next(goal_run_id="g") is not None
    assert manager.claim_next(goal_run_id="g") is None


def test_CROSS_SESSION_OWNER_GUARD():
    """Approval is bound to a principal; another principal cannot consume it."""
    from veya.remote.approval import ApprovalStore
    from veya.remote.models import RiskClass

    store = ApprovalStore()
    record = store.create_approval(
        principal="alice",
        capability_id="git.push",
        normalized_operation="git push origin main",
        cwd="/repo",
        workspace="/repo",
        risk_class=RiskClass.P2_ROOT_MUTATION,
    )
    ok, code, _msg = store.verify_and_consume(
        record.approval_id,
        principal="mallory",
        capability_id="git.push",
        normalized_operation="git push origin main",
        cwd="/repo",
        workspace="/repo",
    )
    assert ok is False
    assert code is not None


# ── refinement gates ─────────────────────────────────────────────────────


def test_UNQUALIFIED_SELF_MODIFICATION_is_zero():
    """Immutable/base authority cannot be a refinement target."""
    assert is_immutable_target(RefinementTarget.ROUTING_POLICY) is True
