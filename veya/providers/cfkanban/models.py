"""Typed, provider-owned cfKanban representations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

CFKANBAN_PROVIDER_ID = "cfkanban"
CFKANBAN_CONTRACT_VERSION = "0.1.0"
CFKANBAN_CONTRACT_PIN = "aff6b5ac21ccd3b5ce580795743157922518567b"
CFKANBAN_OPENAPI_BLOB = "01ca57795e444b4400cb757c94f581b846eedd7b"


@dataclass(frozen=True)
class CfKanbanProviderConfig:
    provider_id: str
    base_url: str
    credential_ref: str
    request_timeout: float = 30.0
    read_timeout: float = 30.0
    connect_timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.provider_id != CFKANBAN_PROVIDER_ID:
            raise ValueError("provider_id must be cfkanban")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) origin")
        if not self.credential_ref or any(
            marker in self.credential_ref.lower() for marker in ("token", "cookie", "bearer")
        ):
            raise ValueError("credential_ref must reference a secret, not contain one")
        if min(self.request_timeout, self.read_timeout, self.connect_timeout) <= 0:
            raise ValueError("timeouts must be positive")

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "credential_ref": self.credential_ref,
            "request_timeout": self.request_timeout,
            "read_timeout": self.read_timeout,
            "connect_timeout": self.connect_timeout,
        }


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class CfKanbanTaskRef:
    mission_id: str
    provider_project_id: str
    provider_task_id: str
    provider_revision: int
    provider_id: str = CFKANBAN_PROVIDER_ID
    provider_board_id: str | None = None

    @property
    def mission_id_is_provider_task_id(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "provider_id": self.provider_id,
            "provider_project_id": self.provider_project_id,
            "provider_board_id": self.provider_board_id,
            "provider_task_id": self.provider_task_id,
            "provider_revision": self.provider_revision,
        }


@dataclass(frozen=True)
class CfKanbanTask:
    identifier: str
    task_id: str | None
    number: int | None
    project_id: str | None
    workspace_id: str | None
    title: str | None
    body: str | None
    state: str | None
    version: int
    blocked: bool = False
    assignee_principal_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CfKanbanTask:
        identifier = str(payload.get("identifier") or payload.get("key") or "")
        if not identifier:
            raise ValueError("cfKanban task response lacks identifier")
        version = payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise ValueError("cfKanban task response lacks valid version")
        status = payload.get("status")
        state = payload.get("status_key")
        if isinstance(status, Mapping):
            state = status.get("key") or status.get("status_key")
        return cls(
            identifier=identifier,
            task_id=_text(payload.get("id")),
            number=payload.get("number") if isinstance(payload.get("number"), int) else None,
            project_id=_text(payload.get("project_id")),
            workspace_id=_text(payload.get("workspace_id")),
            title=_text(payload.get("title")),
            body=_text(payload.get("body")),
            state=_text(state),
            version=version,
            blocked=bool(payload.get("blocked", False)),
            assignee_principal_id=_text(payload.get("assignee_principal_id")),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class CfKanbanCursor:
    value: str
    kind: str

    def __post_init__(self) -> None:
        if not self.value or self.kind not in {"page", "event"}:
            raise ValueError("cursor must be opaque and typed")


@dataclass(frozen=True)
class CfKanbanEvent:
    event_id: str
    event_type: str
    subject_id: str | None
    cursor: str | None
    operation_id: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CfKanbanEvent:
        event_id = str(payload.get("id") or payload.get("event_id") or "")
        if not event_id:
            raise ValueError("cfKanban event response lacks id")
        return cls(
            event_id=event_id,
            event_type=str(payload.get("type") or payload.get("event_type") or ""),
            subject_id=_text(payload.get("subject_id") or payload.get("issue_id")),
            cursor=_text(payload.get("cursor")),
            operation_id=_text(payload.get("operation_id")),
            payload=dict(payload),
        )


@dataclass(frozen=True)
class CfKanbanMutationResult:
    task: CfKanbanTask | None
    event_cursor: str | None
    idempotent_replay: bool
    request_id: str | None
    operation_id: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> CfKanbanMutationResult:
        resource = payload.get("resource")
        task = CfKanbanTask.from_payload(resource) if isinstance(resource, Mapping) else None
        return cls(
            task=task,
            event_cursor=_text(payload.get("event_cursor")),
            idempotent_replay=bool(payload.get("idempotent_replay", False)),
            request_id=request_id,
            operation_id=_text(payload.get("operation_id")),
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_cursor": self.event_cursor,
            "idempotent_replay": self.idempotent_replay,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "resource": dict(self.task.raw) if self.task is not None else None,
        }
