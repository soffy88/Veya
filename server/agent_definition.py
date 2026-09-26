"""Agent definition / deployment / run-identity authority (D8, Veya project layer).

ONE authority separating four frozen identities:

- ``AgentDefinition`` — what an agent IS (versioned, immutable per version)
- ``AgentDeployment`` — how a definition is deployed (pins definition
  version + runtime id + config/secret/policy refs)
- Runtime — D1 obase provider specs / registry for provider
  runtimes, oservi agent registry ``runtime`` entries for engines.
  Only ``runtime_id`` refs are stored here, never descriptor state.
- Run — existing GoalRun / durable execution (identity snapshot only,
  never a second run store).
- AgentSession — existing session paths (referenced, never equated).

Frozen splits: resume-vs-fresh stays D3; file continuity D4; skill
lifecycle D6; capability authoring D7; instance lifecycle D12 (only an
optional ref is carried); task selection stays D9 (no projection here).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "AGENT_EVENT_TOPICS",
    "AgentDefinition",
    "AgentDefinitionStore",
    "AgentDeployment",
    "AgentDeploymentStatus",
    "AgentRunIdentity",
    "RuntimeResolution",
    "activate_deployment",
    "check_runtime_available",
    "create_definition",
    "create_deployment",
    "disable_deployment",
    "resolve_agent_deployment",
    "retire_deployment",
    "stamp_run_identity",
]

AGENT_EVENT_TOPICS = (
    "agent.definition_created",
    "agent.definition_version_created",
    "agent.deployment_created",
    "agent.deployment_activated",
    "agent.deployment_disabled",
    "agent.deployment_retired",
    "agent.run_bound",
)

_REF_RE = re.compile(r"^[A-Za-z0-9_.\-/:]{1,128}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _check_refs(values: list[str] | tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    items = tuple(values or ())
    for item in items:
        if not isinstance(item, str) or not _REF_RE.match(item):
            raise ValueError(f"{field_name} must hold reference strings, got {item!r}")
    return items


class AgentDeploymentStatus(StrEnum):
    """Deployment lifecycle states (single authority)."""

    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"

    @classmethod
    def coerce(cls, value: AgentDeploymentStatus | str) -> AgentDeploymentStatus:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        valid = sorted(member.value for member in cls)
        raise ValueError(f"unknown deployment status {value!r}; expected one of {valid}")


@dataclass(frozen=True)
class AgentDefinition:
    """What an agent IS (immutable per version; refs only, no secrets)."""

    definition_id: str
    version: int
    name: str
    description: str = ""
    instruction_refs: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    tool_refs: tuple[str, ...] = ()
    runtime_requirements: Mapping[str, Any] = field(default_factory=dict)
    policy_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now_iso)
    source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.definition_id.strip() or not self.name.strip():
            raise ValueError("AgentDefinition needs definition_id and name")
        if self.version < 1:
            raise ValueError("AgentDefinition.version must be >= 1")
        object.__setattr__(
            self,
            "instruction_refs",
            _check_refs(self.instruction_refs, field_name="instruction_refs"),
        )
        object.__setattr__(
            self, "capability_refs", _check_refs(self.capability_refs, field_name="capability_refs")
        )
        object.__setattr__(
            self, "skill_refs", _check_refs(self.skill_refs, field_name="skill_refs")
        )
        object.__setattr__(self, "tool_refs", _check_refs(self.tool_refs, field_name="tool_refs"))
        object.__setattr__(
            self, "policy_refs", _check_refs(self.policy_refs, field_name="policy_refs")
        )
        object.__setattr__(self, "runtime_requirements", dict(self.runtime_requirements))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def _canonical(self) -> dict[str, Any]:
        # created_at excluded: identity is content, not wall-clock.
        return {
            "definition_id": self.definition_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "instruction_refs": list(self.instruction_refs),
            "capability_refs": list(self.capability_refs),
            "skill_refs": list(self.skill_refs),
            "tool_refs": list(self.tool_refs),
            "runtime_requirements": dict(self.runtime_requirements),
            "policy_refs": list(self.policy_refs),
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    def key(self) -> str:
        raw = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {**self._canonical(), "key": self.key()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentDefinition:
        raw = dict(data)
        raw.pop("key", None)
        return cls(**raw)


@dataclass(frozen=True)
class AgentDeployment:
    """How a definition is deployed (pins definition version + runtime id)."""

    deployment_id: str
    definition_id: str
    definition_version: int
    revision: int
    runtime_id: str
    status: AgentDeploymentStatus = AgentDeploymentStatus.DRAFT
    config_refs: tuple[str, ...] = ()
    secret_refs: tuple[str, ...] = ()
    policy_refs: tuple[str, ...] = ()
    workspace_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.deployment_id.strip() or not self.definition_id.strip():
            raise ValueError("AgentDeployment needs deployment_id and definition_id")
        if self.definition_version < 1 or self.revision < 1:
            raise ValueError("AgentDeployment definition_version/revision must be >= 1")
        if not self.runtime_id.strip():
            raise ValueError("AgentDeployment needs runtime_id")
        object.__setattr__(self, "status", AgentDeploymentStatus.coerce(self.status))
        object.__setattr__(
            self, "config_refs", _check_refs(self.config_refs, field_name="config_refs")
        )
        object.__setattr__(
            self, "secret_refs", _check_refs(self.secret_refs, field_name="secret_refs")
        )
        object.__setattr__(
            self, "policy_refs", _check_refs(self.policy_refs, field_name="policy_refs")
        )
        object.__setattr__(
            self, "workspace_refs", _check_refs(self.workspace_refs, field_name="workspace_refs")
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def _canonical(self) -> dict[str, Any]:
        # created_at excluded from identity (see definition above).
        return {
            "deployment_id": self.deployment_id,
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "revision": self.revision,
            "runtime_id": self.runtime_id,
            "status": self.status.value,
            "config_refs": list(self.config_refs),
            "secret_refs": list(self.secret_refs),
            "policy_refs": list(self.policy_refs),
            "workspace_refs": list(self.workspace_refs),
            "metadata": dict(self.metadata),
        }

    def _content_key(self) -> str:
        """Identity minus mutable status (pins + refs)."""
        data = dict(self._canonical())
        data.pop("status", None)
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def key(self) -> str:
        raw = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {**self._canonical(), "key": self.key()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentDeployment:
        raw = dict(data)
        raw.pop("key", None)
        return cls(**raw)


@dataclass(frozen=True)
class AgentRunIdentity:
    """Frozen identity snapshot carried by one run (refs only)."""

    definition_id: str
    definition_version: int
    deployment_id: str
    deployment_revision: int
    runtime_id: str
    session_id: str | None = None
    agent_instance_id: str | None = None  # D12 owns the lifecycle; carried as ref only

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "deployment_id": self.deployment_id,
            "deployment_revision": self.deployment_revision,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "agent_instance_id": self.agent_instance_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentRunIdentity:
        _session: str | None = data.get("session_id")
        _instance: str | None = data.get("agent_instance_id")
        return cls(
            definition_id=str(data.get("definition_id")),
            definition_version=int(data.get("definition_version") or 0),
            deployment_id=str(data.get("deployment_id")),
            deployment_revision=int(data.get("deployment_revision") or 0),
            runtime_id=str(data.get("runtime_id")),
            session_id=_session,
            agent_instance_id=_instance,
        )


@dataclass(frozen=True)
class RuntimeResolution:
    """Deterministic resolution output (pure value, no side effects)."""

    definition_id: str
    definition_version: int
    deployment_id: str
    deployment_revision: int
    runtime_id: str
    runtime_kind: str  # "provider" | "engine"
    runtime_available: bool
    runtime_capabilities: tuple[str, ...] = ()
    runtime_lifecycle: Mapping[str, Any] = field(default_factory=dict)
    config_refs: tuple[str, ...] = ()
    policy_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "deployment_id": self.deployment_id,
            "deployment_revision": self.deployment_revision,
            "runtime_id": self.runtime_id,
            "runtime_kind": self.runtime_kind,
            "runtime_available": self.runtime_available,
            "runtime_capabilities": list(self.runtime_capabilities),
            "runtime_lifecycle": dict(self.runtime_lifecycle),
            "config_refs": list(self.config_refs),
            "policy_refs": list(self.policy_refs),
            "reasons": list(self.reasons),
        }


class AgentDefinitionStore:
    """Single definition+deployment state authority (one JSON substrate).

    Holds versioned definitions and pinned deployments — never Run/Session
    state, never a second AgentRegistry. Atomic writes, restart-safe,
    version conflicts fail closed.
    """

    FILENAME = "agent_definitions.json"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / self.FILENAME

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"definitions": {}, "deployments": {}}
        if not isinstance(data, dict):
            return {"definitions": {}, "deployments": {}}
        return {
            "definitions": dict(data.get("definitions") or {}),
            "deployments": dict(data.get("deployments") or {}),
        }

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # -- definitions ------------------------------------------------------
    def put_definition(self, definition: AgentDefinition) -> AgentDefinition:
        """Store one immutable version; same content is idempotent."""
        data = self._load()
        bucket = data["definitions"].setdefault(definition.definition_id, {})
        existing = bucket.get(str(definition.version))
        if existing is not None:
            if existing.get("key") != definition.key():
                raise ValueError(
                    f"definition version conflict: {definition.definition_id} "
                    f"v{definition.version} already stored with different content"
                )
            return AgentDefinition.from_dict(existing)
        bucket[str(definition.version)] = definition.to_dict()
        self._save(data)
        return definition

    def get_definition(
        self, definition_id: str, version: int | None = None
    ) -> AgentDefinition | None:
        bucket = self._load()["definitions"].get(definition_id, {})
        if version is None:
            if not bucket:
                return None
            version = max(int(item) for item in bucket)
        raw = bucket.get(str(version))
        return AgentDefinition.from_dict(raw) if raw else None

    def list_definitions(self) -> list[AgentDefinition]:
        out: list[AgentDefinition] = []
        for bucket in self._load()["definitions"].values():
            for raw in bucket.values():
                out.append(AgentDefinition.from_dict(raw))
        return sorted(out, key=lambda item: (item.definition_id, item.version))

    # -- deployments --------------------------------------------------------
    def put_deployment(self, deployment: AgentDeployment) -> AgentDeployment:
        """Store one deployment revision; same content is idempotent.

        Status-only transitions (draft -> active/disabled/retired) on the
        same revision are allowed; any other content change conflicts.
        """
        data = self._load()
        bucket = data["deployments"].setdefault(deployment.deployment_id, {})
        existing = bucket.get(str(deployment.revision))
        if existing is not None:
            if existing.get("key") != deployment.key():
                _old = AgentDeployment.from_dict(existing)
                if _old._content_key() != deployment._content_key():
                    raise ValueError(
                        f"deployment revision conflict: {deployment.deployment_id} "
                        f"r{deployment.revision} already stored with different content"
                    )
                # status-only transition: fall through to overwrite below
            else:
                return AgentDeployment.from_dict(existing)
        bucket[str(deployment.revision)] = deployment.to_dict()
        self._save(data)
        return deployment

    def get_deployment(
        self, deployment_id: str, revision: int | None = None
    ) -> AgentDeployment | None:
        bucket = self._load()["deployments"].get(deployment_id, {})
        if revision is None:
            if not bucket:
                return None
            revision = max(int(item) for item in bucket)
        raw = bucket.get(str(revision))
        return AgentDeployment.from_dict(raw) if raw else None


def _emit(
    emit: Callable[[str, dict[str, Any]], Any] | None,
    topic: str,
    payload: Mapping[str, Any],
) -> None:
    if emit is None:
        return
    emit(topic, dict(payload))


def create_definition(
    store: AgentDefinitionStore,
    *,
    definition_id: str,
    name: str,
    version: int = 1,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    **fields: Any,
) -> AgentDefinition:
    """Create (or idempotently return) one immutable definition version."""
    preexisting_versions = [
        item for item in [store.get_definition(definition_id)] if item is not None
    ]
    existed = store.get_definition(definition_id, version)
    created = store.put_definition(
        AgentDefinition(definition_id=definition_id, version=version, name=name, **fields)
    )
    if existed is None:
        _emit(
            emit,
            "agent.definition_version_created"
            if preexisting_versions
            else "agent.definition_created",
            created.to_dict(),
        )
    return created


def create_deployment(
    store: AgentDefinitionStore,
    *,
    deployment_id: str,
    definition_id: str,
    definition_version: int,
    runtime_id: str,
    revision: int = 1,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
    **fields: Any,
) -> AgentDeployment:
    """Create a draft deployment pinning an existing definition version."""
    if store.get_definition(definition_id, definition_version) is None:
        raise ValueError(f"cannot deploy missing definition: {definition_id} v{definition_version}")
    created = store.put_deployment(
        AgentDeployment(
            deployment_id=deployment_id,
            definition_id=definition_id,
            definition_version=definition_version,
            revision=revision,
            runtime_id=runtime_id,
            status=AgentDeploymentStatus.DRAFT,
            **fields,
        )
    )
    _emit(emit, "agent.deployment_created", created.to_dict())
    return created


def _check_activation(store: AgentDefinitionStore, deployment: AgentDeployment) -> list[str]:
    """Gate reasons for draft -> active (empty means activatable).

    Only facts verifiable from the stores themselves are checked here:
    the pinned definition version must exist. Skill/capability gates run
    at run start (fresh D6/D7 verification), and runtime availability is
    checked by the caller right after.
    """
    if store.get_definition(deployment.definition_id, deployment.definition_version) is None:
        return ["definition version missing"]
    return []


def activate_deployment(
    store: AgentDefinitionStore,
    deployment_id: str,
    *,
    revision: int | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
) -> AgentDeployment:
    """Activate a draft deployment after gates pass (explicit only)."""
    deployment = store.get_deployment(deployment_id, revision)
    if deployment is None:
        raise ValueError(f"unknown deployment: {deployment_id!r}")
    if deployment.status is AgentDeploymentStatus.ACTIVE:
        return deployment
    if deployment.status is not AgentDeploymentStatus.DRAFT:
        raise ValueError(f"only draft deployments activate (status={deployment.status.value})")
    problems = _check_activation(store, deployment)
    real_problems = [item for item in problems if item]
    if real_problems:
        raise ValueError(f"activation denied: {'; '.join(real_problems)}")
    runtime = check_runtime_available(deployment.runtime_id)
    if not runtime["available"]:
        raise ValueError(f"activation denied: runtime unavailable ({runtime['reason']})")
    import dataclasses as _dataclasses

    activated = _dataclasses.replace(deployment, status=AgentDeploymentStatus.ACTIVE)
    stored = store.put_deployment(activated)
    _emit(emit, "agent.deployment_activated", stored.to_dict())
    return stored


def disable_deployment(
    store: AgentDefinitionStore,
    deployment_id: str,
    *,
    revision: int | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
) -> AgentDeployment:
    """Disable a deployment (kept for audit; active runs unaffected Retro)."""
    deployment = store.get_deployment(deployment_id, revision)
    if deployment is None:
        raise ValueError(f"unknown deployment: {deployment_id!r}")
    if deployment.status is AgentDeploymentStatus.DISABLED:
        return deployment
    import dataclasses as _dataclasses

    stored = store.put_deployment(
        _dataclasses.replace(deployment, status=AgentDeploymentStatus.DISABLED)
    )
    _emit(emit, "agent.deployment_disabled", stored.to_dict())
    return stored


def retire_deployment(
    store: AgentDefinitionStore,
    deployment_id: str,
    *,
    revision: int | None = None,
    emit: Callable[[str, dict[str, Any]], Any] | None = None,
) -> AgentDeployment:
    """Retire a deployment permanently (history retained)."""
    deployment = store.get_deployment(deployment_id, revision)
    if deployment is None:
        raise ValueError(f"unknown deployment: {deployment_id!r}")
    if deployment.status is AgentDeploymentStatus.RETIRED:
        return deployment
    import dataclasses as _dataclasses

    stored = store.put_deployment(
        _dataclasses.replace(deployment, status=AgentDeploymentStatus.RETIRED)
    )
    _emit(emit, "agent.deployment_retired", {**stored.to_dict(), "retired": True})
    return stored


def check_runtime_available(runtime_id: str) -> dict[str, Any]:
    """Resolve a runtime id against D1 + engine authorities (read-only).

    Provider runtimes resolve through the canonical obase ProviderRegistry
    (availability, family, capabilities, lifecycle). Engine runtimes resolve
    through the obase AgentRegistry ``runtime`` entries. Unknown runtimes
    fail closed; nothing here starts execution or classifies tasks.
    """
    from veya.platform import load

    if not runtime_id.strip():
        return {
            "runtime_id": runtime_id,
            "available": False,
            "kind": "unknown",
            "reason": "empty runtime id",
        }
    obase = load("obase")
    try:
        spec = obase.ProviderRegistry.get().spec(runtime_id)
    except Exception:
        spec = None
    if spec is not None:
        available = bool(getattr(spec, "available", True))
        return {
            "runtime_id": runtime_id,
            "available": available,
            "kind": "provider",
            "protocol_family": str(getattr(spec, "protocol_family", "")),
            "capabilities": sorted(getattr(spec, "capabilities", ()) or ()),
            "lifecycle": dict(getattr(spec, "lifecycle", {}) or {}),
            "reason": "" if available else "provider spec marked unavailable",
        }
    try:
        entry = obase.AgentRegistry().get("runtime", runtime_id)
    except Exception:
        entry = None
    if entry is None:
        return {
            "runtime_id": runtime_id,
            "available": False,
            "kind": "unknown",
            "reason": "unknown runtime id",
        }
    return {
        "runtime_id": runtime_id,
        "available": True,
        "kind": "engine",
        "reason": "registered engine runtime",
    }


def resolve_agent_deployment(
    store: AgentDefinitionStore,
    *,
    deployment_id: str | None = None,
    definition_id: str | None = None,
    revision: int | None = None,
) -> RuntimeResolution:
    """Deterministic definition -> deployment -> runtime resolution.

    Pure value output: refs plus the runtime descriptor projection. Creates
    no session, starts no execution, classifies no task.
    """
    deployment: AgentDeployment | None = None
    if deployment_id is not None:
        deployment = store.get_deployment(deployment_id, revision)
    elif definition_id is not None:
        candidates = [
            item
            for item in store._load()["deployments"].values()
            for item in [AgentDeployment.from_dict(raw) for raw in item.values()]
            if item.definition_id == definition_id and item.status is AgentDeploymentStatus.ACTIVE
        ]
        if candidates:
            candidates.sort(key=lambda item: item.revision)
            deployment = candidates[-1]
    if deployment is None:
        raise ValueError("no matching deployment to resolve")
    definition = store.get_definition(deployment.definition_id, deployment.definition_version)
    if definition is None:
        raise ValueError(
            f"deployment pins missing definition: {deployment.definition_id} "
            f"v{deployment.definition_version}"
        )
    runtime = check_runtime_available(deployment.runtime_id)
    return RuntimeResolution(
        definition_id=deployment.definition_id,
        definition_version=deployment.definition_version,
        deployment_id=deployment.deployment_id,
        deployment_revision=deployment.revision,
        runtime_id=deployment.runtime_id,
        runtime_kind=str(runtime.get("kind", "unknown")),
        runtime_available=bool(runtime.get("available", False)),
        runtime_capabilities=tuple(runtime.get("capabilities", ()) or ()),
        runtime_lifecycle=dict(runtime.get("lifecycle", {}) or {}),
        config_refs=deployment.config_refs,
        policy_refs=tuple(sorted(set(deployment.policy_refs) | set(definition.policy_refs))),
        reasons=() if runtime.get("available") else (str(runtime.get("reason", "")),),
    )


def stamp_run_identity(
    resolution: RuntimeResolution,
    *,
    session_id: str | None = None,
    agent_instance_id: str | None = None,
) -> AgentRunIdentity:
    """Freeze a run identity snapshot from a resolution (refs only)."""
    return AgentRunIdentity(
        definition_id=resolution.definition_id,
        definition_version=resolution.definition_version,
        deployment_id=resolution.deployment_id,
        deployment_revision=resolution.deployment_revision,
        runtime_id=resolution.runtime_id,
        session_id=session_id,
        agent_instance_id=agent_instance_id,
    )
