"""AgentInstance — runtime identity for one live Bot agent instance.

An AgentInstance is the runtime identity of one running Bot agent. It is NOT
a second execution store; it is a ref-only identity snapshot that GoalRun
carries as ``agent_instance_id``. The instance lifecycle is owned by the
D12 instance lifecycle authority; this module only defines the identity
dataclass and status enum.

Authority boundary:
    AgentInstance     = runtime identity (ref only)
    GoalRun           = durable execution authority (carries agent_instance_id)
    BotRuntime        = lifecycle caller (creates/destroys instances)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class AgentInstanceStatus(StrEnum):
    """AgentInstance lifecycle states."""

    CREATED = "created"
    ADMITTED = "admitted"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"

    @classmethod
    def coerce(cls, value: AgentInstanceStatus | str) -> AgentInstanceStatus:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        valid = sorted(member.value for member in cls)
        raise ValueError(f"unknown instance status {value!r}; expected one of {valid}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AgentInstance:
    """Runtime identity of one live Bot agent instance.

    Fields are refs only — never execution state, never task graph, never
    session state. GoalRun carries ``agent_instance_id`` as the immutable
    run identity snapshot; the instance lifecycle is owned by D12.
    """

    instance_id: str
    bot_id: str
    definition_id: str
    definition_version: int
    deployment_id: str
    deployment_revision: int
    runtime_id: str
    status: AgentInstanceStatus = AgentInstanceStatus.CREATED
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("AgentInstance needs instance_id")
        if not self.bot_id.strip():
            raise ValueError("AgentInstance needs bot_id")
        if not self.definition_id.strip():
            raise ValueError("AgentInstance needs definition_id")
        if not self.deployment_id.strip():
            raise ValueError("AgentInstance needs deployment_id")
        if not self.runtime_id.strip():
            raise ValueError("AgentInstance needs runtime_id")
        if self.definition_version < 1:
            raise ValueError("AgentInstance definition_version must be >= 1")
        if self.deployment_revision < 1:
            raise ValueError("AgentInstance deployment_revision must be >= 1")
        object.__setattr__(self, "status", AgentInstanceStatus.coerce(self.status))

    @property
    def is_terminal(self) -> bool:
        """Whether the instance is in a terminal state (cannot resume)."""
        return self.status in (AgentInstanceStatus.STOPPED, AgentInstanceStatus.FAILED)

    @property
    def is_running(self) -> bool:
        """Whether the instance is actively running."""
        return self.status is AgentInstanceStatus.RUNNING

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentInstance:
        return cls(
            instance_id=str(value.get("instance_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            definition_id=str(value.get("definition_id") or ""),
            definition_version=int(value.get("definition_version") or 0),
            deployment_id=str(value.get("deployment_id") or ""),
            deployment_revision=int(value.get("deployment_revision") or 0),
            runtime_id=str(value.get("runtime_id") or ""),
            status=AgentInstanceStatus.coerce(value.get("status", "created")),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )


class AgentInstanceStore:
    """AgentInstance catalog with optional JSON file persistence.

    Mirrors BotRegistry / AgentDefinitionStore pattern. Instances are
    identity refs only; execution state stays in GoalRun.
    """

    FILENAME = "agent_instances.json"

    def __init__(self, root: str | Path | None = None):
        if root is not None:
            self.path = Path(root) / self.FILENAME
        else:
            self.path = Path.home() / ".veya" / self.FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def put(self, instance: AgentInstance) -> AgentInstance:
        """Store one instance; same content is idempotent."""
        data = self._load()
        existing = data.get(instance.instance_id)
        if existing is not None and existing.get("status") == instance.status.value:
            return instance  # no change
        data[instance.instance_id] = instance.to_dict()
        self._save(data)
        return instance

    def get(self, instance_id: str) -> AgentInstance | None:
        raw = self._load().get(instance_id)
        return AgentInstance.from_dict(raw) if raw else None

    def list_by_bot(self, bot_id: str) -> list[AgentInstance]:
        out: list[AgentInstance] = []
        for raw in self._load().values():
            if isinstance(raw, dict) and raw.get("bot_id") == bot_id:
                out.append(AgentInstance.from_dict(raw))
        return sorted(out, key=lambda item: item.created_at)

    def list_active(self, bot_id: str) -> AgentInstance | None:
        """Return the most recent non-terminal instance for a bot."""
        instances = self.list_by_bot(bot_id)
        for inst in reversed(instances):
            if not inst.is_terminal:
                return inst
        return None


__all__ = [
    "AgentInstance",
    "AgentInstanceStatus",
    "AgentInstanceStore",
]
