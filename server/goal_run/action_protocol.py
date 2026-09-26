"""The single semantic-action ABI between MasterAgent and GoalRun.

These immutable records carry one model-selected action across the existing
GoalRun execution boundary.  They deliberately contain no lifecycle,
retry/replan, or acceptance state; those authorities remain with GoalRun and
the Verification OS respectively.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from runtime.bot_scope import DEFAULT_BOT_ID


@dataclass(frozen=True)
class CanonicalActionRequest:
    """A semantic action selected by MasterAgent for GoalRun to execute."""

    action_id: str
    goal_run_id: str
    task_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    capability: str | None = None
    computer_ref: str | None = None
    context_ref: str | None = None
    approval: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    evidence_refs: tuple[str, ...] = ()
    # P3-A: the bot that owns this action. Must match the executing GoalRun.
    bot_id: str = DEFAULT_BOT_ID

    def __post_init__(self) -> None:
        if not self.action_id or not self.goal_run_id or not self.task_id or not self.tool:
            raise ValueError("action_id, goal_run_id, task_id, and tool are required")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", self.action_id)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        value["bot_id"] = self.bot_id
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CanonicalActionRequest:
        return cls(
            action_id=str(value["action_id"]),
            goal_run_id=str(value["goal_run_id"]),
            task_id=str(value["task_id"]),
            tool=str(value["tool"]),
            arguments=dict(value.get("arguments") or {}),
            capability=value.get("capability"),
            computer_ref=value.get("computer_ref"),
            context_ref=value.get("context_ref"),
            approval=dict(value.get("approval") or {}),
            idempotency_key=str(value.get("idempotency_key") or ""),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


@dataclass(frozen=True)
class CanonicalActionResult:
    """The physical execution result returned by GoalRun to MasterAgent."""

    action_id: str
    status: str
    attempted: bool
    executed: bool
    result: Any = None
    failure_evidence: tuple[dict[str, Any], ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    approval: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action_id or not self.status:
            raise ValueError("action_id and status are required")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["failure_evidence"] = [dict(item) for item in self.failure_evidence]
        value["artifact_refs"] = list(self.artifact_refs)
        value["evidence_refs"] = list(self.evidence_refs)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CanonicalActionResult:
        return cls(
            action_id=str(value["action_id"]),
            status=str(value["status"]),
            attempted=bool(value.get("attempted", False)),
            executed=bool(value.get("executed", False)),
            result=value.get("result"),
            failure_evidence=tuple(dict(item) for item in value.get("failure_evidence") or ()),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            approval=dict(value.get("approval") or {}),
        )


__all__ = ["CanonicalActionRequest", "CanonicalActionResult"]
