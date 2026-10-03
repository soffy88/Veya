"""P2-02 Computer Use Runtime — canonical loop for computer-use actions.

Canonical loop:
  Observation → ActionProposal → PolicyCheck → Approval if required →
  Action → PostObservation → Verification

Computer-use success requires observed postcondition, not merely successful
click/keystroke dispatch.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ActionProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    POLICY_CHECKED = "POLICY_CHECKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class VerificationResult(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class Observation:
    """A screenshot/UI state observation."""

    observation_id: str
    timestamp: float = field(default_factory=time.time)
    screenshot_ref: str = ""
    ui_state: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class ActionProposal:
    """A proposed computer-use action."""

    proposal_id: str
    action_type: str
    target: str
    arguments: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    expected_outcome: str = ""


@dataclass(frozen=True)
class PostObservation:
    """Observation after action execution."""

    observation_id: str
    proposal_id: str
    timestamp: float = field(default_factory=time.time)
    screenshot_ref: str = ""
    ui_state: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class ComputerUseResult:
    """Result of a computer-use action with verification."""

    result_id: str
    proposal_id: str
    status: ActionProposalStatus
    verification: VerificationResult
    pre_observation: Observation | None = None
    post_observation: PostObservation | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_observation(*, description: str = "", screenshot_ref: str = "") -> Observation:
    return Observation(
        observation_id=str(uuid.uuid4()),
        description=description,
        screenshot_ref=screenshot_ref,
    )


def new_proposal(
    *,
    action_type: str,
    target: str,
    arguments: dict[str, Any] | None = None,
    expected_outcome: str = "",
) -> ActionProposal:
    return ActionProposal(
        proposal_id=str(uuid.uuid4()),
        action_type=action_type,
        target=target,
        arguments=dict(arguments or {}),
        expected_outcome=expected_outcome,
    )


def verify_postcondition(
    proposal: ActionProposal,
    post: PostObservation,
) -> VerificationResult:
    """Verify that the postcondition matches the expected outcome.

    Computer-use success requires observed postcondition, not merely
    successful click/keystroke dispatch.
    """
    if not post.ui_state and not post.description:
        return VerificationResult.INCONCLUSIVE
    if proposal.expected_outcome and proposal.expected_outcome in post.description:
        return VerificationResult.PASSED
    if post.ui_state.get("success") is True:
        return VerificationResult.PASSED
    if post.ui_state.get("error"):
        return VerificationResult.FAILED
    return VerificationResult.INCONCLUSIVE
