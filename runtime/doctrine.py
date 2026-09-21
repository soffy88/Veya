"""P15 operating doctrine contracts.

This module contains deterministic policy primitives only.  It deliberately
does not execute tools, own Mission state, or create a second registry or
orchestrator.  Mission persistence remains ``veya.supervision`` and the
existing 3O registries remain the registry authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AutonomyLevel(StrEnum):
    investigate = "investigate"
    draft = "draft"
    autopilot = "autopilot"
    full_autopilot = "full_autopilot"


class AutonomyDecision(StrEnum):
    allow = "ALLOW"
    require_review = "REQUIRE_REVIEW"
    require_human = "REQUIRE_HUMAN"
    deny = "DENY"


_LEVEL_ORDER = {
    AutonomyLevel.investigate: 0,
    AutonomyLevel.draft: 1,
    AutonomyLevel.autopilot: 2,
    AutonomyLevel.full_autopilot: 3,
}
_HARD_HUMAN_ACTIONS = {
    "production_deploy",
    "database_destructive_migration",
    "credential_create",
    "credential_rotate",
    "credential_reveal",
    "permission_escalation",
    "billing_payment",
    "irreversible_delete",
}


@dataclass(frozen=True)
class ActionContext:
    action_type: str
    environment: str = "development"
    blast_radius: str = "low"
    data_sensitivity: str = "normal"
    destructive: bool = False
    permission_change: bool = False
    credential_use: bool = False
    production_effect: bool = False
    verification_available: bool = False


@dataclass(frozen=True)
class AutonomyPolicy:
    id: str = "veya-default-autonomy"
    version: str = "1.0"
    maximum_level: AutonomyLevel = AutonomyLevel.draft

    def evaluate(self, level: AutonomyLevel, action: ActionContext) -> AutonomyDecision:
        """Evaluate policy only; callers must still check real tool authority."""
        if _LEVEL_ORDER[level] > _LEVEL_ORDER[self.maximum_level]:
            return AutonomyDecision.require_review
        if action.action_type in _HARD_HUMAN_ACTIONS:
            return AutonomyDecision.require_human
        if action.production_effect or action.permission_change:
            return AutonomyDecision.require_human
        if action.destructive and not action.verification_available:
            return AutonomyDecision.require_human
        if level is AutonomyLevel.investigate and action.action_type in {
            "modify",
            "commit",
            "merge",
            "deploy",
            "delete",
        }:
            return AutonomyDecision.deny
        if level is AutonomyLevel.draft and action.action_type in {"merge", "deploy"}:
            return AutonomyDecision.require_review
        if level in {AutonomyLevel.autopilot, AutonomyLevel.full_autopilot}:
            if not action.verification_available:
                return AutonomyDecision.require_review
            return AutonomyDecision.allow
        return AutonomyDecision.allow

    def require_verification(self, requested: AutonomyLevel, available: bool) -> AutonomyLevel:
        """Downgrade before execution when no machine-readable verification exists."""
        if requested in {AutonomyLevel.autopilot, AutonomyLevel.full_autopilot} and not available:
            return AutonomyLevel.draft
        return requested

    def downgrade(self, current: AutonomyLevel, reason: str) -> AutonomyLevel:
        """Runtime may only move down; reason is required for an auditable event."""
        if not reason.strip():
            raise ValueError("autonomy downgrade requires a reason")
        if current is AutonomyLevel.full_autopilot:
            return AutonomyLevel.autopilot
        if current is AutonomyLevel.autopilot:
            return AutonomyLevel.draft
        if current is AutonomyLevel.draft:
            return AutonomyLevel.investigate
        return current


@dataclass(frozen=True)
class AgentRoleContract:
    role_id: str
    version: str = "1.0"
    description: str = ""
    owns: tuple[str, ...] = ()
    does_not_own: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    sources_of_truth: tuple[str, ...] = ()
    approval_required_for: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    verification_profiles: tuple[str, ...] = ()
    allowed_autonomy_levels: tuple[AutonomyLevel, ...] = (
        AutonomyLevel.investigate,
        AutonomyLevel.draft,
    )
    context_policy: str = "relevant-only"

    def scope_violation(self, responsibility: str) -> bool:
        return responsibility in set(self.does_not_own)

    def allows_autonomy(self, level: AutonomyLevel) -> bool:
        return level in set(self.allowed_autonomy_levels)


def attach_role_contract(registry: Any, contract: AgentRoleContract) -> None:
    """Extend an existing 3O AgentRegistry entry with role metadata.

    The registry remains the authority.  This helper refuses to invent an
    agent entry and stores only a contract reference/metadata on an existing
    entry, so role text cannot grant capabilities.
    """
    entry = registry.get("agent", contract.role_id)
    if entry is None:
        raise KeyError(f"agent is not registered: {contract.role_id}")
    entry["role_contract"] = contract


def enforce_role_action(
    contract: AgentRoleContract,
    *,
    responsibility: str,
    tool_name: str,
    tool_authorized: bool,
) -> None:
    """Apply role scope and real tool authority as fail-closed checks."""
    if contract.scope_violation(responsibility):
        raise PermissionError("ROLE_SCOPE_VIOLATION")
    if tool_name not in set(contract.allowed_tools) or not tool_authorized:
        raise PermissionError("TOOL_AUTHORITY_DENIED")


def assert_pinned_versions(
    *,
    expected: dict[str, str],
    actual: dict[str, str],
) -> None:
    """Reject a silent policy switch after Mission start."""
    for key, version in expected.items():
        if version and actual.get(key, "") != version:
            raise RuntimeError(f"POLICY_VERSION_CONFLICT:{key}")


@dataclass(frozen=True)
class PlaybookRule:
    rule_id: str
    principle: str
    applies_when: tuple[str, ...] = ()
    does_not_apply_when: tuple[str, ...] = ()
    severity: str = "medium"
    enforcement: str = "GUIDANCE"
    source: str = ""


@dataclass(frozen=True)
class TeamPlaybook:
    playbook_id: str
    version: str = "1.0"
    scope: str = "project"
    rules: tuple[PlaybookRule, ...] = ()
    authority: str = "authorized_policy_owner"
    provenance: str = ""
    effective_from: str = ""
    supersedes: str = ""

    def rule(self, rule_id: str) -> PlaybookRule | None:
        return next((item for item in self.rules if item.rule_id == rule_id), None)


@dataclass(frozen=True)
class ComposedContext:
    playbook_id: str = ""
    playbook_version: str = ""
    rule_ids: tuple[str, ...] = ()
    role_contract_version: str = ""


def compose_context(
    *,
    playbook: TeamPlaybook | None,
    role: AgentRoleContract | None,
    applicable_rule_ids: list[str] | None = None,
) -> ComposedContext:
    """Compose references, not a second prompt/policy authority."""
    if playbook is None:
        return ComposedContext(role_contract_version=role.version if role else "")
    wanted = set(applicable_rule_ids or [rule.rule_id for rule in playbook.rules])
    known = {rule.rule_id for rule in playbook.rules}
    return ComposedContext(
        playbook_id=playbook.playbook_id,
        playbook_version=playbook.version,
        rule_ids=tuple(sorted(wanted & known)),
        role_contract_version=role.version if role else "",
    )


@dataclass(frozen=True)
class CorrectionRecord:
    id: str
    mission_id: str
    execution_id: str
    category: str
    observed_failure: str
    correction: str
    evidence: tuple[dict[str, Any], ...] = ()
    created_at: str = ""


@dataclass(frozen=True)
class RuleCandidate:
    id: str
    source_corrections: tuple[str, ...]
    proposed_principle: str
    applies_when: tuple[str, ...] = ()
    does_not_apply_when: tuple[str, ...] = ()
    suggested_target: str = "playbook"
    confidence: float = 0.0
    status: str = "candidate"


_OVERFIT_PATTERNS = (
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b", re.I),
    re.compile(r"\b(?:file|path)\s+[\w./-]+\b", re.I),
    re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"),
    re.compile(r"\b(?:error|status)[-_ ]?\d{3}\b", re.I),
)


def anti_overfit(principle: str) -> tuple[bool, list[str]]:
    findings = [pattern.pattern for pattern in _OVERFIT_PATTERNS if pattern.search(principle)]
    return not findings and bool(principle.strip()), findings


def generalize_correction(
    corrections: list[CorrectionRecord],
    *,
    candidate_id: str,
    proposed_principle: str,
    suggested_target: str = "playbook",
) -> RuleCandidate:
    """Create a reviewable candidate; never activate policy automatically."""
    if not corrections:
        raise ValueError("RULE_CANDIDATE_DENIED: no correction evidence")
    if not anti_overfit(proposed_principle)[0]:
        raise ValueError("OVERFIT_RULE=BLOCKED")
    return RuleCandidate(
        id=candidate_id,
        source_corrections=tuple(item.id for item in corrections),
        proposed_principle=proposed_principle,
        suggested_target=suggested_target,
        confidence=min(1.0, len(corrections) / 3),
    )


@dataclass(frozen=True)
class SkillContract:
    id: str
    version: str
    applies_when: tuple[str, ...]
    does_not_apply_when: tuple[str, ...]
    inputs: tuple[str, ...]
    required_access: tuple[str, ...]
    steps: tuple[str, ...]
    error_handling: tuple[str, ...]
    approval_gates: tuple[str, ...]
    verification: tuple[str, ...]
    outputs: tuple[str, ...]


def promote_skill(
    candidate: RuleCandidate,
    *,
    successful_executions: int,
    has_verification: bool,
    has_error_handling: bool,
    has_approval_boundary: bool,
    has_io_contract: bool,
) -> SkillContract:
    if candidate.suggested_target != "skill":
        raise ValueError("PROMOTION_DENIED: candidate target is not skill")
    if not anti_overfit(candidate.proposed_principle)[0]:
        raise ValueError("OVERFIT_RULE=BLOCKED")
    if successful_executions < 1 or not all(
        (has_verification, has_error_handling, has_approval_boundary, has_io_contract)
    ):
        raise ValueError("PROMOTION_DENIED: incomplete skill qualification")
    return SkillContract(
        id=candidate.id,
        version="1.0",
        applies_when=candidate.applies_when,
        does_not_apply_when=candidate.does_not_apply_when,
        inputs=("context",),
        required_access=(),
        steps=(candidate.proposed_principle,),
        error_handling=("stop and report bounded failure",),
        approval_gates=("canonical authority",),
        verification=("required machine-readable verification",),
        outputs=("structured result",),
    )


@dataclass(frozen=True)
class RoutinePolicy:
    id: str
    owner_role_id: str
    trigger: str
    inputs: tuple[str, ...] = ()
    output_policy: str = "useful-output-only"
    approval_policy: str = "canonical-authority"
    failure_policy: str = "bounded-retry-then-escalate"
    noop_policy: str = "NOOP"
    cost_budget: float | None = None
    schedule_reason: str = ""
    maximum_frequency: str = ""
    event_source_available: bool = True

    def validate(self) -> None:
        if self.trigger == "schedule" and not self.schedule_reason:
            raise ValueError("scheduled routine requires reason_for_frequency")
        if self.trigger == "schedule" and self.event_source_available:
            raise ValueError("event-first policy requires a reason no event source is available")
        if self.noop_policy != "NOOP":
            raise ValueError("routine must be silent when there is no useful work")


@dataclass
class RoutineTelemetry:
    runs: int = 0
    noop_runs: int = 0
    model_tokens: int = 0
    tool_calls: int = 0
    estimated_cost: float = 0.0
    useful_outputs: int = 0
    failures: int = 0

    @property
    def noop_ratio(self) -> float:
        return self.noop_runs / self.runs if self.runs else 0.0

    @property
    def cost_per_useful_output(self) -> float | None:
        return self.estimated_cost / self.useful_outputs if self.useful_outputs else None


__all__ = [
    "ActionContext",
    "AgentRoleContract",
    "AutonomyDecision",
    "AutonomyLevel",
    "AutonomyPolicy",
    "ComposedContext",
    "CorrectionRecord",
    "PlaybookRule",
    "RoutinePolicy",
    "RoutineTelemetry",
    "RuleCandidate",
    "SkillContract",
    "TeamPlaybook",
    "anti_overfit",
    "assert_pinned_versions",
    "attach_role_contract",
    "compose_context",
    "enforce_role_action",
    "generalize_correction",
    "promote_skill",
]
