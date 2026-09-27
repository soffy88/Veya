import dataclasses
from typing import Any


@dataclasses.dataclass
class ApprovalRecord:
    request_id: str
    tool: str
    tool_args: dict[str, Any]
    session_id: str
    status: str  # "pending", "approved", "denied"
    created_at: float
    updated_at: float
    decision_reason: str | None = None
    request_hash: str | None = None


@dataclasses.dataclass
class SessionGovernanceState:
    session_id: str
    mode: str
    require_approval: bool
    freeze_root: str | None
    freeze_allow: str | None
    revision: int
    updated_at: float
