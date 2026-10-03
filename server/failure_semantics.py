"""§24 Failure Semantics — every execution terminates into an explicit semantic state.

Forbidden: timeout→success, budget exhausted→success, max rounds→success,
provider failure→success, stale child→parent success.
"""

from __future__ import annotations

from enum import StrEnum


class TerminalState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"
    INTERRUPTED = "INTERRUPTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICTED = "CONFLICTED"


_NON_SUCCESS_STATES = frozenset({
    TerminalState.FAILED,
    TerminalState.CANCELLED,
    TerminalState.TIMED_OUT,
    TerminalState.BLOCKED,
    TerminalState.INTERRUPTED,
    TerminalState.BUDGET_EXHAUSTED,
    TerminalState.POLICY_DENIED,
    TerminalState.CONFLICTED,
})


def is_success(state: TerminalState) -> bool:
    """Only SUCCEEDED is success. Everything else is not."""
    return state is TerminalState.SUCCEEDED


def is_non_success(state: TerminalState) -> bool:
    """Check if a terminal state is NOT success."""
    return state in _NON_SUCCESS_STATES


def reconcile_parent(child_states: list[TerminalState]) -> TerminalState:
    """Derive parent state from child states.

    Parent convergence MUST derive from child semantics.
    Any non-success child makes the parent non-success.
    """
    if not child_states:
        return TerminalState.FAILED
    if all(s is TerminalState.SUCCEEDED for s in child_states):
        return TerminalState.SUCCEEDED
    if any(s is TerminalState.BUDGET_EXHAUSTED for s in child_states):
        return TerminalState.BUDGET_EXHAUSTED
    if any(s is TerminalState.POLICY_DENIED for s in child_states):
        return TerminalState.POLICY_DENIED
    if any(s is TerminalState.CONFLICTED for s in child_states):
        return TerminalState.CONFLICTED
    if any(s is TerminalState.TIMED_OUT for s in child_states):
        return TerminalState.TIMED_OUT
    if any(s is TerminalState.CANCELLED for s in child_states):
        return TerminalState.CANCELLED
    if any(s is TerminalState.BLOCKED for s in child_states):
        return TerminalState.BLOCKED
    return TerminalState.FAILED
