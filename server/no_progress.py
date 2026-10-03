"""§25 No-Progress Protection — semantic progress detection.

Removal of max_rounds does NOT mean infinite uncontrolled loops.
Repeated cycles with no meaningful progress yield NO_PROGRESS_DETECTED → BLOCKED.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class ProgressSignal(StrEnum):
    NEW_TASK_COMPLETION = "NEW_TASK_COMPLETION"
    NEW_EVIDENCE = "NEW_EVIDENCE"
    NEW_WORKSPACE_MUTATION = "NEW_WORKSPACE_MUTATION"
    NEW_ARTIFACT = "NEW_ARTIFACT"
    VERIFICATION_IMPROVEMENT = "VERIFICATION_IMPROVEMENT"
    RESOLVED_BLOCKER = "RESOLVED_BLOCKER"
    STATE_TRANSITION = "STATE_TRANSITION"


class NoProgressVerdict(StrEnum):
    PROGRESSING = "PROGRESSING"
    NO_PROGRESS_DETECTED = "NO_PROGRESS_DETECTED"


@dataclass
class ProgressTracker:
    """Tracks semantic progress signals across execution cycles."""

    goal_run_id: str
    signals: list[tuple[float, ProgressSignal]] = field(default_factory=list)
    cycle_count: int = 0
    last_progress_at: float = field(default_factory=time.time)
    max_idle_cycles: int = 5

    def record(self, signal: ProgressSignal) -> None:
        """Record a progress signal."""
        now = time.time()
        self.signals.append((now, signal))
        self.last_progress_at = now
        self.cycle_count += 1

    def tick(self) -> NoProgressVerdict:
        """Advance one cycle. Returns NO_PROGRESS_DETECTED if stalled."""
        self.cycle_count += 1
        idle_cycles = self.cycle_count - len(self.signals)
        if idle_cycles >= self.max_idle_cycles:
            return NoProgressVerdict.NO_PROGRESS_DETECTED
        return NoProgressVerdict.PROGRESSING

    def reset(self) -> None:
        """Reset the tracker (e.g., after a state transition)."""
        self.signals.clear()
        self.cycle_count = 0
        self.last_progress_at = time.time()


def detect_no_progress(
    *,
    cycles_without_progress: int,
    threshold: int = 5,
) -> NoProgressVerdict:
    """Detect if execution is making no progress."""
    if cycles_without_progress >= threshold:
        return NoProgressVerdict.NO_PROGRESS_DETECTED
    return NoProgressVerdict.PROGRESSING
