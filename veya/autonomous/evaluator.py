"""Outcome Evaluation, Progress Tracking, and Completion Gate (spec §12, §13, §14).

Invariants:
- Evaluator only recommends outcome; MasterAgent remains sole semantic authority.
- Execution exit_code == 0 alone NEVER completes a mission (EXIT_CODE_COMPLETION=0).
- Completion requires verified evidence, cleared blockers, and satisfied objective (UNVERIFIED_COMPLETION=0).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .models import (
    CompletionDecision,
    EvaluationResult,
    OutcomeVerdict,
    ProgressAssessment,
)


class OutcomeEvaluator:
    """Evaluate action execution outcomes against semantic criteria (spec §12)."""

    def evaluate(
        self,
        mission_id: str,
        action_id: str,
        expected_result: str,
        execution_output: str,
        exit_code: int = 0,
        effect_receipt: dict[str, Any] | None = None,
        verification_passed: bool | None = None,
        evidence_refs: list[str] | None = None,
    ) -> EvaluationResult:
        evidence = list(evidence_refs or [])
        verified_claims: list[str] = []

        # If verification command was executed and failed -> REJECT
        if verification_passed is False:
            return EvaluationResult(
                evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
                mission_id=mission_id,
                action_id=action_id,
                verdict=OutcomeVerdict.REJECT,
                reason="VERIFICATION_COMMAND_FAILED",
                evidence_refs=evidence,
                verified_claims=[],
                unverified_claims=[expected_result],
                created_at=time.time(),
            )

        # If exit code was non-zero -> REJECT
        if exit_code != 0:
            return EvaluationResult(
                evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
                mission_id=mission_id,
                action_id=action_id,
                verdict=OutcomeVerdict.REJECT,
                reason=f"EXECUTION_NON_ZERO_EXIT_CODE: {exit_code}",
                evidence_refs=evidence,
                verified_claims=[],
                unverified_claims=[expected_result],
                created_at=time.time(),
            )

        # If evidence is absent -> NEEDS_MORE_EVIDENCE
        if not evidence and not effect_receipt:
            return EvaluationResult(
                evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
                mission_id=mission_id,
                action_id=action_id,
                verdict=OutcomeVerdict.NEEDS_MORE_EVIDENCE,
                reason="NO_DURABLE_EVIDENCE_PRODUCED",
                evidence_refs=[],
                verified_claims=[],
                unverified_claims=[expected_result],
                created_at=time.time(),
            )

        # Verified success
        verified_claims.append(expected_result)
        return EvaluationResult(
            evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            action_id=action_id,
            verdict=OutcomeVerdict.ACCEPT,
            reason="EXECUTION_VERIFIED_BY_EVIDENCE",
            evidence_refs=evidence,
            verified_claims=verified_claims,
            unverified_claims=[],
            created_at=time.time(),
        )


class CompletionGate:
    """Enforces completion contracts before mission termination (spec §14)."""

    def check_completion(
        self,
        mission_id: str,
        objective: str,
        progress: ProgressAssessment,
        evidence_refs: list[str],
        blocking_conditions: list[str] | None = None,
        unresolved_child_goals: list[str] | None = None,
        known_limitations: list[str] | None = None,
        remaining_nonblocking: list[str] | None = None,
    ) -> tuple[bool, CompletionDecision | None, str]:
        # 1. Critical blockers
        if blocking_conditions:
            return (
                False,
                None,
                f"BLOCKED: {len(blocking_conditions)} unresolved blocking conditions",
            )

        # 2. Unresolved mandatory children
        if unresolved_child_goals:
            return (
                False,
                None,
                f"UNRESOLVED_CHILDREN: {len(unresolved_child_goals)} child goals remaining",
            )

        # 3. Objective coverage
        if progress.objective_coverage < 1.0:
            return (
                False,
                None,
                f"INCOMPLETE_COVERAGE: objective coverage is {progress.objective_coverage:.1%}, required 100%",
            )

        # 4. Required evidence presence
        if not evidence_refs:
            return (
                False,
                None,
                "MISSING_EVIDENCE: completion requires at least one verified evidence reference",
            )

        # 5. Regressions
        if progress.regressions:
            return (
                False,
                None,
                f"REGRESSIONS_DETECTED: {progress.regressions}",
            )

        decision = CompletionDecision(
            completion_id=f"comp_{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            completion_reason=f"Objective '{objective}' verified complete with durable evidence.",
            evidence_refs=list(evidence_refs),
            known_limitations=list(known_limitations or []),
            remaining_nonblocking_items=list(remaining_nonblocking or []),
            completed_at=time.time(),
        )
        return True, decision, "OK"
