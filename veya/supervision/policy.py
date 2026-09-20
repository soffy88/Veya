"""Escalation and routing policy for the supervision runtime (spec §12/§23/§26).

Pure data + pure functions. No I/O, no model calls. The escalation rule is the
important one: only owner-only / irreversible / credential / policy blockers may
interrupt a human; ordinary engineering failures must be solved by Veya.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import EscalationCode, SupervisionMode

# Triggers that MAY interrupt the owner (spec §23).
ESCALATION_TRIGGERS: dict[str, EscalationCode] = {
    "owner_credential_required": EscalationCode.owner_credential_required,
    "credential": EscalationCode.owner_credential_required,
    "secret": EscalationCode.owner_credential_required,
    "irreversible_external_action": EscalationCode.irreversible_external_action,
    "irreversible": EscalationCode.irreversible_external_action,
    "legal_policy_confirmation": EscalationCode.legal_policy_confirmation_required,
    "policy_confirmation": EscalationCode.legal_policy_confirmation_required,
    "production_destructive": EscalationCode.production_destructive_action,
    "destructive_production": EscalationCode.production_destructive_action,
    "authority_conflict": EscalationCode.unresolvable_authority_conflict,
    "resource_owner_input": EscalationCode.resource_owner_input_required,
}

# Triggers that MUST NOT interrupt the owner: Veya retries, repairs or routes
# around them (spec §23).
SELF_HANDLED_TRIGGERS: frozenset[str] = frozenset(
    {
        "test_failure",
        "lint_failure",
        "build_failure",
        "implementation_bug",
        "provider_transient_error",
        "known_migration_issue",
        "retryable_deployment_failure",
        "timeout",
        "flaky_test",
        "missing_optional_dependency",
    }
)


def classify_escalation(trigger: str) -> EscalationCode | None:
    """Return the escalation code for *trigger*, or None when Veya must self-handle.

    Unknown triggers are treated as self-handled (never bother the owner on an
    unrecognized condition); owner-only conditions must match explicitly.
    """

    key = (trigger or "").strip().lower()
    if not key or key in SELF_HANDLED_TRIGGERS:
        return None
    return ESCALATION_TRIGGERS.get(key)


def requires_owner(trigger: str) -> bool:
    return classify_escalation(trigger) is not None


# ── routing hints (spec §12) ────────────────────────────────────────────
@dataclass(frozen=True)
class RouterHints:
    """Task characteristics that bias the initial supervision mode."""

    prefer_external: frozenset[str] = frozenset(
        {
            "architecture_redesign",
            "cross_project_design",
            "canonical_authority_choice",
            "large_refactor",
            "security_sensitive_design",
            "release_qualification",
            "ambiguous_requirements",
            "high_blast_radius",
            "independent_review_valuable",
        }
    )
    prefer_internal: frozenset[str] = frozenset(
        {
            "clear_implementation",
            "known_bug",
            "routine_regression",
            "test_lint_repair",
            "bounded_refactor",
            "dependency_update",
            "repetitive_migration",
            "well_defined_acceptance",
            "long_unattended_execution",
        }
    )

    def score(self, characteristics: list[str]) -> tuple[int, int]:
        """Return (external_score, internal_score) for the given characteristics."""

        keys = {(c or "").strip().lower() for c in characteristics}
        return len(keys & self.prefer_external), len(keys & self.prefer_internal)


DEFAULT_HINTS = RouterHints()


def preferred_mode(characteristics: list[str], *, hints: RouterHints | None = None) -> str:
    """Deterministic starting bias; the router may refine with risk/budget."""

    hints = hints or DEFAULT_HINTS
    external, internal = hints.score(characteristics)
    if external > internal:
        return str(SupervisionMode.external)
    if internal > external:
        return str(SupervisionMode.internal)
    return str(SupervisionMode.internal)  # default: unattended-capable


# ── switch triggers (spec §13) ──────────────────────────────────────────
INTERNAL_TO_EXTERNAL_TRIGGERS: frozenset[str] = frozenset(
    {
        "architecture_ambiguity",
        "canonical_conflict",
        "high_impact_decision",
        "repeated_failure",
        "review_disagreement",
        "jev_low_confidence_critical",
        "security_boundary_change",
        "acceptance_ambiguity",
    }
)

EXTERNAL_TO_INTERNAL_TRIGGERS: frozenset[str] = frozenset(
    {
        "design_frozen",
        "remaining_work_mechanical",
        "long_execution",
        "external_supervisor_unavailable",
        "repetitive_fixes",
        "cost_latency_optimization",
    }
)


def switch_direction(trigger: str, current: str) -> str | None:
    """Return the supervisor to switch to for *trigger*, or None to stay."""

    key = (trigger or "").strip().lower()
    if current == str(SupervisionMode.internal) and key in INTERNAL_TO_EXTERNAL_TRIGGERS:
        return str(SupervisionMode.external)
    if current == str(SupervisionMode.external) and key in EXTERNAL_TO_INTERNAL_TRIGGERS:
        return str(SupervisionMode.internal)
    return None


def fallback_mode(
    requested: str, *, external_available: bool, policy: dict[str, Any] | None = None
) -> str:
    """External requested but unavailable -> internal only if policy allows it."""

    if external_available:
        return requested
    policy = policy or {}
    if str(policy.get("external_fallback_to_internal", "")).lower() in {"1", "true", "yes"}:
        return (
            str(SupervisionMode.internal)
            if requested == str(SupervisionMode.external)
            else requested
        )
    return requested
