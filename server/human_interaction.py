"""§21 Human Interaction Model — approval is one subtype of a broader model.

Types: APPROVAL, CLARIFICATION, SELECTION, REVIEW, OVERRIDE, TAKEOVER.
This prevents each frontend feature from inventing its own pause/resume semantics.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class InteractionType(StrEnum):
    APPROVAL = "APPROVAL"
    CLARIFICATION = "CLARIFICATION"
    SELECTION = "SELECTION"
    REVIEW = "REVIEW"
    OVERRIDE = "OVERRIDE"
    TAKEOVER = "TAKEOVER"


class InteractionStatus(StrEnum):
    PENDING = "PENDING"
    RESPONDED = "RESPONDED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class HumanInteractionRequest:
    """A request for human interaction during execution."""

    interaction_id: str
    type: InteractionType
    goal_run_id: str
    execution_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)
    deadline: float | None = None
    status: InteractionStatus = InteractionStatus.PENDING
    response: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    responded_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HumanInteractionRequest:
        data = dict(data)
        if isinstance(data.get("type"), str):
            data["type"] = InteractionType(data["type"])
        if isinstance(data.get("status"), str):
            data["status"] = InteractionStatus(data["status"])
        return cls(**data)


def new_interaction(
    *,
    interaction_type: InteractionType,
    goal_run_id: str,
    execution_id: str | None = None,
    payload: dict[str, Any] | None = None,
    options: list[str] | None = None,
    deadline: float | None = None,
) -> HumanInteractionRequest:
    """Create a new human interaction request."""
    return HumanInteractionRequest(
        interaction_id=str(uuid.uuid4()),
        type=interaction_type,
        goal_run_id=goal_run_id,
        execution_id=execution_id,
        payload=dict(payload or {}),
        options=list(options or []),
        deadline=deadline,
    )


def respond(
    request: HumanInteractionRequest,
    response: dict[str, Any],
) -> HumanInteractionRequest:
    """Record a response to an interaction request."""
    return HumanInteractionRequest(
        **{
            **request.to_dict(),
            "status": InteractionStatus.RESPONDED,
            "response": response,
            "responded_at": time.time(),
        }
    )


def is_expired(request: HumanInteractionRequest, *, now: float | None = None) -> bool:
    """Check if an interaction request has expired."""
    if request.deadline is None:
        return False
    return (now or time.time()) > request.deadline
