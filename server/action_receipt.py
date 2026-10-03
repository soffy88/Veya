"""P1-08 Action Receipt — tamper-evident evidence for high-impact actions.

A receipt proves attribution/integrity/ordering, not correctness or actual
policy enforcement; those remain separate concerns.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ReceiptPolicy(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class ActionReceipt:
    """Tamper-evident receipt for a high-impact action."""

    receipt_id: str
    goal_run_id: str
    execution_id: str
    agent_id: str
    action: str
    capability: str
    target: str
    policy_decision_id: str = ""
    approval_id: str | None = None
    request_digest: str = ""
    result_digest: str = ""
    timestamp: float = field(default_factory=time.time)
    previous_receipt_digest: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_digest(self) -> str:
        """Compute the digest of this receipt for chain integrity."""
        data = {
            "receipt_id": self.receipt_id,
            "goal_run_id": self.goal_run_id,
            "execution_id": self.execution_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "capability": self.capability,
            "target": self.target,
            "policy_decision_id": self.policy_decision_id,
            "approval_id": self.approval_id,
            "request_digest": self.request_digest,
            "result_digest": self.result_digest,
            "timestamp": self.timestamp,
            "previous_receipt_digest": self.previous_receipt_digest,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


def requires_receipt(
    *,
    side_effect_class: str,
    is_deployment: bool = False,
    is_git_push: bool = False,
    is_external_communication: bool = False,
    is_production_mutation: bool = False,
    is_approval_protected: bool = False,
) -> bool:
    """Determine if an action requires a receipt based on its risk profile."""
    if side_effect_class in {"EXTERNAL_MUTATION", "PRIVILEGED", "IRREVERSIBLE"}:
        return True
    if is_deployment or is_git_push or is_external_communication:
        return True
    return is_production_mutation or is_approval_protected


def new_receipt(
    *,
    goal_run_id: str,
    execution_id: str,
    agent_id: str,
    action: str,
    capability: str,
    target: str,
    policy_decision_id: str = "",
    approval_id: str | None = None,
    request_digest: str = "",
    result_digest: str = "",
    previous_receipt_digest: str | None = None,
) -> ActionReceipt:
    """Create a new action receipt."""
    receipt = ActionReceipt(
        receipt_id=str(uuid.uuid4()),
        goal_run_id=goal_run_id,
        execution_id=execution_id,
        agent_id=agent_id,
        action=action,
        capability=capability,
        target=target,
        policy_decision_id=policy_decision_id,
        approval_id=approval_id,
        request_digest=request_digest,
        result_digest=result_digest,
        previous_receipt_digest=previous_receipt_digest,
    )
    return ActionReceipt(
        **{
            **receipt.to_dict(),
            "signature": receipt.compute_digest(),
        }
    )


def verify_chain(receipts: list[ActionReceipt]) -> tuple[bool, str]:
    """Verify the integrity of a receipt chain.

    Returns (valid, reason). Each receipt's previous_receipt_digest must
    match the previous receipt's signature.
    """
    for i, receipt in enumerate(receipts):
        if receipt.signature != receipt.compute_digest():
            return False, f"receipt {i}: signature mismatch"
        if i > 0:
            prev = receipts[i - 1]
            if receipt.previous_receipt_digest != prev.signature:
                return False, f"receipt {i}: chain broken"
    return True, "chain valid"
