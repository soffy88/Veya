"""Durable Issue↔Mission identity on the existing MissionStore."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast

from veya.supervision.store import MissionStore


class BindingState(StrEnum):
    DISCOVERED = "DISCOVERED"
    BOUND = "BOUND"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    DETACHED = "DETACHED"


ACTIVE_STATES = frozenset(
    {
        BindingState.DISCOVERED,
        BindingState.BOUND,
        BindingState.RUNNING,
        BindingState.WAITING_REVIEW,
        BindingState.BLOCKED,
    }
)


class DuplicateBindingError(RuntimeError):
    """A second active binding for one provider issue was attempted."""


@dataclass(frozen=True)
class IssueMissionBinding:
    provider: str
    instance_id: str
    workspace_id: str
    project_id: str
    issue_id: str
    issue_number: int | None
    mission_id: str
    issue_version_at_bind: int
    last_seen_issue_version: int
    last_event_cursor: str | None
    supervision_mode: str
    autonomy_level: str
    state: BindingState
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "instance_id": self.instance_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "issue_id": self.issue_id,
            "issue_number": self.issue_number,
            "mission_id": self.mission_id,
            "issue_version_at_bind": self.issue_version_at_bind,
            "last_seen_issue_version": self.last_seen_issue_version,
            "last_event_cursor": self.last_event_cursor,
            "supervision_mode": self.supervision_mode,
            "autonomy_level": self.autonomy_level,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IssueMissionBinding:
        return cls(
            provider=str(data["provider"]),
            instance_id=str(data["instance_id"]),
            workspace_id=str(data["workspace_id"]),
            project_id=str(data["project_id"]),
            issue_id=str(data["issue_id"]),
            issue_number=data.get("issue_number"),
            mission_id=str(data["mission_id"]),
            issue_version_at_bind=int(data["issue_version_at_bind"]),
            last_seen_issue_version=int(data["last_seen_issue_version"]),
            last_event_cursor=data.get("last_event_cursor"),
            supervision_mode=str(data["supervision_mode"]),
            autonomy_level=str(data["autonomy_level"]),
            state=BindingState(data["state"]),
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
        )


def stable_mission_id(instance_id: str, issue_id: str) -> str:
    digest = hashlib.sha256(f"cfkanban\x1f{instance_id}\x1f{issue_id}".encode()).hexdigest()[:32]
    return f"mission-cfk-{digest}"


class CfKanbanBindingStore:
    """Binding repository using MissionStore's append-only event authority."""

    _TOPIC = "CFKANBAN_BINDING_SNAPSHOT"
    _INTAKE_PREFIX = "__cfkanban_intake__"

    def __init__(self, store: MissionStore) -> None:
        self.store = store

    def _snapshots(self) -> list[IssueMissionBinding]:
        snapshots: list[IssueMissionBinding] = []
        for mission in self.store.list():
            events = cast(list[dict[str, Any]], self.store.events(mission.mission_id))
            for event in events:
                if event.get("topic") == self._TOPIC and isinstance(event.get("binding"), dict):
                    snapshots.append(IssueMissionBinding.from_dict(event["binding"]))
        return snapshots

    def find(self, instance_id: str, issue_id: str) -> IssueMissionBinding | None:
        matches = [
            item
            for item in self._snapshots()
            if item.instance_id == instance_id and item.issue_id == issue_id
        ]
        return max(matches, key=lambda item: item.updated_at) if matches else None

    def find_active(self, instance_id: str, issue_id: str) -> IssueMissionBinding | None:
        item = self.find(instance_id, issue_id)
        return item if item is not None and item.state in ACTIVE_STATES else None

    def by_mission(self, mission_id: str) -> IssueMissionBinding | None:
        matches = [item for item in self._snapshots() if item.mission_id == mission_id]
        return max(matches, key=lambda item: item.updated_at) if matches else None

    def save(self, binding: IssueMissionBinding) -> IssueMissionBinding:
        if binding.provider != "cfkanban":
            raise ValueError("binding provider must be cfkanban")
        active = self.find_active(binding.instance_id, binding.issue_id)
        if (
            active is not None
            and active.mission_id != binding.mission_id
            and binding.state in ACTIVE_STATES
        ):
            raise DuplicateBindingError(
                f"active binding already exists for {binding.instance_id}/{binding.issue_id}"
            )
        self.store.append_event(
            binding.mission_id,
            self._TOPIC,
            {"binding": binding.to_dict()},
        )
        return binding

    def transition(
        self, binding: IssueMissionBinding, state: BindingState, **changes: Any
    ) -> IssueMissionBinding:
        updated = replace(binding, state=state, updated_at=time.time(), **changes)
        return self.save(updated)

    def event_committed(self, scope_id: str, event_id: str) -> bool:
        events = cast(list[dict[str, Any]], self.store.events(f"{self._INTAKE_PREFIX}:{scope_id}"))
        return any(
            event.get("topic") == "CFKANBAN_EVENT_COMMITTED" and event.get("event_id") == event_id
            for event in events
        )

    def commit_event(self, scope_id: str, event_id: str, cursor: str | None) -> None:
        self.store.append_event(
            f"{self._INTAKE_PREFIX}:{scope_id}",
            "CFKANBAN_EVENT_COMMITTED",
            {"event_id": event_id, "cursor": cursor},
        )

    def last_cursor(self, scope_id: str) -> str | None:
        cursor: str | None = None
        events = cast(list[dict[str, Any]], self.store.events(f"{self._INTAKE_PREFIX}:{scope_id}"))
        for event in events:
            if event.get("topic") == "CFKANBAN_EVENT_COMMITTED":
                cursor = event.get("cursor")
        return cursor
