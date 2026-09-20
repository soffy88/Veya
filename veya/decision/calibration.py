"""Confidence calibration for the Jev plane (spec §25).

A low-confidence Jev decision on a critical question must not be treated as an
answer: it escalates to the current supervisor. Missing confidence is treated as
low confidence (fail safe toward review, never toward silent acceptance).
"""

from __future__ import annotations

from .models import JevDecision

DEFAULT_LOW_CONFIDENCE = 0.5


def is_low_confidence(decision: JevDecision, *, threshold: float = DEFAULT_LOW_CONFIDENCE) -> bool:
    confidence = decision.confidence
    return confidence is None or confidence < threshold


def needs_supervisor(decision: JevDecision, *, threshold: float = DEFAULT_LOW_CONFIDENCE) -> bool:
    """True when the supervisor must decide instead of trusting Jev."""

    return is_low_confidence(decision, threshold=threshold)


__all__ = ["DEFAULT_LOW_CONFIDENCE", "is_low_confidence", "needs_supervisor"]
