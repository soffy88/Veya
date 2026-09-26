"""The standard Jev question set (spec §15 decision classes).

One fan-out per iteration keeps the fast path to a single provider call.
"""

from __future__ import annotations

from .models import JevQuestion, QuestionKind


def standard_questions() -> list[JevQuestion]:
    """Evidence/risk/retry/failure/safety questions the loop asks each round."""

    return [
        JevQuestion(
            "evidence_sufficient",
            QuestionKind.score,
            criteria=["insufficient", "sufficient"],
        ),
        JevQuestion(
            "regression_risk",
            QuestionKind.score,
            criteria=["no added risk", "high regression risk"],
        ),
        JevQuestion(
            "retry_worthwhile",
            QuestionKind.choice,
            criteria={"yes": "a retry is likely to fix it", "no": "retrying will not help"},
        ),
        JevQuestion(
            "same_failure",
            QuestionKind.choice,
            criteria={"yes": "same failure as before", "no": "a different failure"},
        ),
        JevQuestion(
            "safe_to_continue",
            QuestionKind.choice,
            criteria={"yes": "safe to continue", "no": "stop and escalate"},
        ),
        JevQuestion(
            "needs_supervisor_review",
            QuestionKind.choice,
            criteria={"yes": "supervisor must decide", "no": "no supervisor needed"},
        ),
    ]


__all__ = ["standard_questions"]
