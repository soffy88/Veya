"""No-Progress and Oscillation Detectors (spec §15, §16).

Invariants:
- No fixed max_rounds; detection is based on semantic progress, accepted evidence, and state delta.
- Oscillation detection detects A→B→A→B loops and terminates runaway loops.
- Recommends actionable recovery: REPLAN, RETASK, WAIT, ESCALATE, ABORT.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import DecisionType, EscalationReason


@dataclass
class NoProgressSignal:
    has_no_progress: bool
    reason: str = ""
    recommended_decision: DecisionType | None = None
    escalation_reason: EscalationReason | None = None


class NoProgressDetector:
    """Detects lack of semantic forward progress across autonomous rounds (spec §15)."""

    def __init__(self, repeat_threshold: int = 3):
        self._repeat_threshold = repeat_threshold
        self._action_history: list[str] = []
        self._failure_history: list[str] = []
        self._evidence_history: list[int] = []

    def record_step(
        self, action_signature: str, error_detail: str | None, evidence_count: int
    ) -> None:
        self._action_history.append(action_signature)
        if error_detail:
            self._failure_history.append(error_detail)
        else:
            self._failure_history.append("")
        self._evidence_history.append(evidence_count)

    def check(self) -> NoProgressSignal:
        if len(self._action_history) < self._repeat_threshold:
            return NoProgressSignal(has_no_progress=False)

        recent_actions = self._action_history[-self._repeat_threshold :]
        recent_failures = self._failure_history[-self._repeat_threshold :]
        recent_evidence = self._evidence_history[-self._repeat_threshold :]

        # 1. Identical action repeated without new evidence
        if len(set(recent_actions)) == 1 and recent_evidence[-1] == recent_evidence[0]:
            return NoProgressSignal(
                has_no_progress=True,
                reason=f"IDENTICAL_ACTION_REPEATED: '{recent_actions[0]}' executed {self._repeat_threshold} times without new evidence",
                recommended_decision=DecisionType.RETASK,
            )

        # 2. Same failure repeated
        non_empty_failures = [f for f in recent_failures if f]
        if len(non_empty_failures) >= self._repeat_threshold and len(set(non_empty_failures)) == 1:
            return NoProgressSignal(
                has_no_progress=True,
                reason=f"REPEATED_SAME_FAILURE: '{non_empty_failures[0]}' failed {self._repeat_threshold} times",
                recommended_decision=DecisionType.REPLAN,
            )

        # 3. Zero semantic delta over multiple steps
        if len(self._evidence_history) >= self._repeat_threshold * 2:
            longer_window = self._evidence_history[-(self._repeat_threshold * 2) :]
            if longer_window[-1] == longer_window[0]:
                return NoProgressSignal(
                    has_no_progress=True,
                    reason="ZERO_SEMANTIC_PROGRESS: No new verified evidence over extended window",
                    recommended_decision=DecisionType.ESCALATE,
                    escalation_reason=EscalationReason.REPEATED_FAILURE,
                )

        return NoProgressSignal(has_no_progress=False)


@dataclass
class OscillationSignal:
    is_oscillating: bool
    reason: str = ""
    cycle_detected: list[str] = field(default_factory=list)


class OscillationDetector:
    """Detects cyclical state oscillation A→B→A→B or executor hopping (spec §16)."""

    def __init__(self, window_size: int = 8):
        self._window: deque[str] = deque(maxlen=window_size)

    def record_signature(self, signature: str) -> None:
        self._window.append(signature)

    def check(self) -> OscillationSignal:
        items = list(self._window)
        n = len(items)
        if n < 4:
            return OscillationSignal(is_oscillating=False)

        # Check 2-cycle: A, B, A, B
        if items[-1] == items[-3] and items[-2] == items[-4] and items[-1] != items[-2]:
            return OscillationSignal(
                is_oscillating=True,
                reason=f"OSCILLATION_DETECTED: 2-cycle alternating between '{items[-2]}' and '{items[-1]}'",
                cycle_detected=[items[-2], items[-1]],
            )

        # Check 3-cycle: A, B, C, A, B, C
        if (
            n >= 6
            and items[-1] == items[-4]
            and items[-2] == items[-5]
            and items[-3] == items[-6]
            and len({items[-1], items[-2], items[-3]}) == 3
        ):
            return OscillationSignal(
                is_oscillating=True,
                reason=f"OSCILLATION_DETECTED: 3-cycle between {items[-3:]}",
                cycle_detected=items[-3:],
            )

        return OscillationSignal(is_oscillating=False)
