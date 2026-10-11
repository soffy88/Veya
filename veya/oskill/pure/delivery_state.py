"""3O-PURE — derive_delivery_state: delivered / started / unconfirmed.

State derivation for NANO-W6 addendum K. Pure over inputs: ``delivered`` is
positive evidence the brief/steer reached the target's inbox/session;
``has_start_evidence`` is positive evidence the model began (first token /
tool call / event from that run). No start signal stays ``unconfirmed`` —
delivery is never assumed.
"""

from __future__ import annotations

STATE_DELIVERED = "delivered"
STATE_STARTED = "started"
STATE_UNCONFIRMED = "unconfirmed"


def derive_delivery_state(*, delivered: bool, has_start_evidence: bool) -> str:
    """→ "delivered" | "started" | "unconfirmed"."""

    if delivered:
        return STATE_DELIVERED
    if has_start_evidence:
        return STATE_STARTED
    return STATE_UNCONFIRMED


__all__ = ["derive_delivery_state", "STATE_DELIVERED", "STATE_STARTED", "STATE_UNCONFIRMED"]
