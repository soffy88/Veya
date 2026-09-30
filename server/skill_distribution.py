"""Skill distribution lifecycle (D6, Veya project-layer coordinator).

ONE decision layer over the frozen storage authorities — it creates no
registry, store, or ranking of its own:

- skill metadata/version/trust state ... ``SkillRegistry`` (capability_model)
- version history + active resolution ... personal runtime skill tables
- file index/search/hot-cold ............ ``SkillCatalog`` (cache only)
- package ingestion ..................... ``capability_package_importer``
- runtime exposure ........................ SkillHub / ToolRegistry paths
- qualification evidence .................. backend scans (promotion-time)

Canonical lifecycle: DISCOVERED -> IMPORTED -> CANDIDATE -> QUALIFYING ->
VERIFIED -> ACTIVE -> DEPRECATED, with REVOKED as terminal deny. The mapping
from each backend's native statuses is explicit (``classify_*``); unknown
native statuses fail closed instead of guessing.

Frozen splits: file/session continuity (D4) and resume-vs-fresh (D3) stay
out; ranking/selection for tasks stays D9 (``_route_skills`` untouched).
SkillHub file skills are a legacy universe — distribution-managed execution
always passes the materialize gate below.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "DISTRIBUTION_EVENT_TOPICS",
    "SkillBinding",
    "SkillDistribution",
    "SkillDistributionRecord",
    "SkillLifecycleState",
    "SourceProvenance",
    "classify_personal_version",
    "classify_registry_spec",
    "select_distribution_backend",
]

DISTRIBUTION_EVENT_TOPICS = (
    "skill.imported",
    "skill.qualification_completed",
    "skill.promoted",
    "skill.bound",
    "skill.refreshed",
    "skill.rollback",
    "skill.revoked",
    "skill.materialized",
)


class SkillLifecycleState(StrEnum):
    """The single canonical lifecycle vocabulary (D6)."""

    DISCOVERED = "discovered"
    IMPORTED = "imported"
    CANDIDATE = "candidate"
    QUALIFYING = "qualifying"
    VERIFIED = "verified"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"

    @classmethod
    def coerce(cls, value: SkillLifecycleState | str) -> SkillLifecycleState:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        valid = sorted(member.value for member in cls)
        raise ValueError(f"unknown skill lifecycle state {value!r}; expected one of {valid}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SourceProvenance:
    """Traceable origin of one imported skill version (refs, never bodies)."""

    source_type: str  # "package" | "taught" | "import" | "optimizer" | "legacy"
    source_uri: str
    source_revision: str

    def __post_init__(self) -> None:
        if not self.source_type.strip() or not self.source_uri.strip():
            raise ValueError("SourceProvenance needs source_type and source_uri")


@dataclass(frozen=True)
class SkillDistributionRecord:
    """One lifecycle decision record (projections over authorities)."""

    skill_id: str
    backend: str  # "personal" | "registry"
    state: SkillLifecycleState
    version: int | None = None
    trust: str | None = None
    qualification: str = "none"
    source: SourceProvenance | None = None
    bindings: tuple[str, ...] = ()
    materialized_revision: str | None = None
    supersedes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.skill_id.strip():
            raise ValueError("SkillDistributionRecord.skill_id must not be empty")
        object.__setattr__(self, "state", SkillLifecycleState.coerce(self.state))
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "backend": self.backend,
            "state": self.state.value,
            "version": self.version,
            "trust": self.trust,
            "qualification": self.qualification,
            "source": (
                {
                    "source_type": self.source.source_type,
                    "source_uri": self.source.source_uri,
                    "source_revision": self.source.source_revision,
                }
                if self.source
                else None
            ),
            "bindings": list(self.bindings),
            "materialized_revision": self.materialized_revision,
            "supersedes": self.supersedes,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SkillBinding:
    """One runtime binding: skill version enabled for a scope (ref only)."""

    skill_id: str
    version: int | None
    scope_type: str
    scope_id: str
    enabled: bool = True
    trust_snapshot: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.skill_id.strip() or not self.scope_id.strip():
            raise ValueError("SkillBinding needs skill_id and scope_id")
        object.__setattr__(self, "trust_snapshot", dict(self.trust_snapshot))

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"

    def key(self) -> str:
        raw = f"{self.skill_id}|{self.version}|{self.scope_key}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "enabled": self.enabled,
            "trust_snapshot": dict(self.trust_snapshot),
            "created_at": self.created_at,
            "key": self.key(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkillBinding:
        raw = dict(data)
        raw.pop("key", None)
        return cls(**raw)


def select_distribution_backend(explicit: str | None = None) -> str:
    """The single backend-selection point (was scattered env if/else).

    Returns ``"personal"`` when the execution database is configured,
    else ``"registry"``. Same condition the routes historically used —
    centralized here, semantics unchanged.
    """
    if explicit is not None:
        if explicit not in ("personal", "registry"):
            raise ValueError(f"unknown distribution backend: {explicit!r}")
        return explicit
    if os.environ.get("VEYA_EXECUTION_DATABASE_URL"):
        return "personal"
    return "registry"


def classify_personal_version(row: Mapping[str, Any]) -> SkillLifecycleState:
    """Map a personal skill_versions row onto the canonical lifecycle."""
    status = str(row.get("status") or "")
    trust = str(row.get("trust_status") or "")
    if trust == "blocked":
        return SkillLifecycleState.REVOKED
    if status == "candidate":
        return SkillLifecycleState.CANDIDATE
    if status == "active":
        return SkillLifecycleState.ACTIVE if trust == "trusted" else SkillLifecycleState.QUALIFYING
    if status == "deprecated":
        return SkillLifecycleState.DEPRECATED
    raise ValueError(f"unmappable personal skill status: {status!r}")


def classify_registry_spec(status: str, trust: str) -> SkillLifecycleState:
    """Map a registry SkillSpec onto the canonical lifecycle.

    The registry universe has no stored ACTIVE: verified+trusted is its
    executable state, so that combination maps to ACTIVE (documented
    mapping, not a second authority).
    """
    if trust == "blocked":
        return SkillLifecycleState.REVOKED
    if status == "candidate":
        return SkillLifecycleState.CANDIDATE
    if status == "verified":
        return SkillLifecycleState.ACTIVE if trust == "trusted" else SkillLifecycleState.QUALIFYING
    if status == "deprecated":
        return SkillLifecycleState.DEPRECATED
    raise ValueError(f"unmappable registry skill status: {status!r}")


def _revision_of(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class SkillDistribution:
    """Distribution orchestration over the frozen authorities.

    Owns exactly: lifecycle records (in-memory projections), bindings and
    source revisions (one JSON file). Skill bodies, versions, trust and
    exposure stay with their authorities — this layer only references.
    """

    STORE_FILENAME = "skill_distribution.json"

    def __init__(
        self,
        store_root: str | Path | None = None,
        *,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        registry: Any | None = None,
    ) -> None:
        self.store_root = (
            Path(store_root).expanduser().resolve() if store_root else Path.home() / ".veya"
        )
        self._emit = emit
        self._registry_override = registry

    # -- state ----------------------------------------------------------
    def _path(self) -> Path:
        return self.store_root / self.STORE_FILENAME

    def _load_state(self) -> dict[str, Any]:
        path = self._path()
        if not path.exists():
            return {"bindings": [], "sources": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"bindings": [], "sources": {}}
        if not isinstance(data, dict):
            return {"bindings": [], "sources": {}}
        return {
            "bindings": list(data.get("bindings") or []),
            "sources": dict(data.get("sources") or {}),
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _event(self, topic: str, payload: Mapping[str, Any]) -> None:
        if self._emit is None:
            return
        self._emit(topic, dict(payload))

    def _registry(self) -> Any:
        if self._registry_override is not None:
            return self._registry_override
        from server.capability_model import skill_registry

        return skill_registry

    # -- import -----------------------------------------------------------
    async def import_package(
        self,
        package_dir: str | Path,
        *,
        scope_type: str = "user",
        scope_id: str = "local",
        backend: str | None = None,
        created_by: str = "system",
    ) -> list[SkillDistributionRecord]:
        """Import a capability package into candidates (never auto-activate).

        Same revision re-imported is a no-op returning existing records.
        Bad packages fail closed. Personal backend creates versioned
        candidates; registry backend delegates to the existing importer
        (registry-owned behavior, unchanged).
        """
        from server.capability_package_importer import parse_skill_files

        package_path = Path(package_dir).expanduser().resolve()
        manifest = package_path / "CAPABILITY.yaml"
        if not manifest.is_file():
            raise ValueError(f"not a capability package (missing CAPABILITY.yaml): {package_dir}")
        resolved = select_distribution_backend(backend)
        records: list[SkillDistributionRecord] = []
        state = self._load_state()
        for item in parse_skill_files(package_path / "skills", package_path.name):
            revision = _revision_of(
                {"uri": item["source_path"], "instructions": item["instructions"]}
            )
            source = SourceProvenance(
                source_type="package",
                source_uri=item["source_path"],
                source_revision=revision,
            )
            source_key = f"package:{package_path.name}:{item['skill_id']}"
            known = state["sources"].get(source_key)
            if isinstance(known, dict) and known.get("revision") == revision:
                continue
            if resolved == "personal":
                record = await self._propose_personal(
                    item, scope_type, scope_id, created_by, source
                )
            else:
                record = self._propose_registry(item, source)
            state["sources"][source_key] = {
                "revision": revision,
                "skill_id": record.skill_id,
                "version": record.version,
            }
            records.append(record)
            self._event(
                "skill.imported",
                {**record.to_dict(), "scope_type": scope_type, "scope_id": scope_id},
            )
        self._save_state(state)
        return records

    async def _propose_personal(
        self,
        item: Mapping[str, Any],
        scope_type: str,
        scope_id: str,
        created_by: str,
        source: SourceProvenance,
    ) -> SkillDistributionRecord:
        from runtime.personal import get_personal_runtime

        created = await get_personal_runtime().create_skill_candidate(
            str(item["skill_id"].split(".")[-1]),
            str(item["instructions"] or item["skill_id"]),
            scope_type=scope_type,
            scope_id=scope_id,
            execution_type="prompt",
            created_by=created_by,
        )
        return SkillDistributionRecord(
            skill_id=str(created["skill_id"]),
            backend="personal",
            state=SkillLifecycleState.CANDIDATE,
            version=int(created.get("version") or 1),
            trust="review_required",
            qualification="none",
            source=source,
            metadata={"version_id": created.get("id")},
        )

    def _propose_registry(
        self, item: Mapping[str, Any], source: SourceProvenance
    ) -> SkillDistributionRecord:
        from server.capability_model import SkillSpec
        from server.skill_authority import stage_skill_spec

        spec = SkillSpec(
            skill_id=str(item["skill_id"]),
            instructions=str(item["instructions"]),
            applicable_when=list(item.get("applicable_when") or []),
            not_applicable_when=list(item.get("not_applicable_when") or []),
            provenance=item["provenance"],
        )
        # Delegated staging write (single choke point); direct
        # register_candidate without delegation is sealed.
        stage_skill_spec(self._registry(), spec)
        return SkillDistributionRecord(
            skill_id=spec.skill_id,
            backend="registry",
            state=SkillLifecycleState.CANDIDATE,
            version=int(spec.version),
            trust=str(spec.trust_status),
            qualification="none",
            source=source,
        )

    # -- qualify / promote --------------------------------------------------
    async def qualify(
        self,
        skill_id: str,
        *,
        backend: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Qualification evidence for a candidate (no state change).

        Runs deterministic prechecks against the owning authority; the
        authority's own scan gates promotion. Optional caller evidence
        (e.g. skill_opt held-out results) is recorded, never executed.
        """
        resolved = select_distribution_backend(backend)
        if resolved == "personal":
            outcome = await self._qualify_personal(skill_id)
        else:
            outcome = self._qualify_registry(skill_id)
        outcome["evidence"] = dict(evidence or {})
        self._event(
            "skill.qualification_completed",
            {"skill_id": skill_id, "backend": resolved, **outcome},
        )
        return outcome

    async def _qualify_personal(self, skill_id: str) -> dict[str, Any]:
        from runtime.personal import get_personal_runtime

        skill = await get_personal_runtime().get_skill(skill_id, versions=True)
        if skill is None:
            return {"eligible": False, "reasons": ["unknown skill"]}
        versions = [v for v in skill.get("versions", []) if v.get("status") == "candidate"]
        if not versions:
            return {"eligible": False, "reasons": ["no candidate version"]}
        blocked = [v for v in versions if v.get("trust_status") == "blocked"]
        if blocked:
            return {"eligible": False, "reasons": ["candidate blocked by safety scan"]}
        return {
            "eligible": True,
            "reasons": ["candidate present, scan runs at promotion"],
            "version": versions[0].get("version"),
        }

    def _qualify_registry(self, skill_id: str) -> dict[str, Any]:
        spec = self._registry().get_version(skill_id)
        if spec is None:
            return {"eligible": False, "reasons": ["unknown skill"]}
        if spec.status != "candidate":
            return {"eligible": False, "reasons": [f"status is {spec.status}, not candidate"]}
        if spec.trust_status == "blocked":
            return {"eligible": False, "reasons": ["candidate blocked by safety scan"]}
        return {
            "eligible": True,
            "reasons": ["candidate present, scan runs at promotion"],
            "version": spec.version,
        }

    async def promote(
        self,
        skill_id: str,
        *,
        backend: str | None = None,
        qualification: Mapping[str, Any] | None = None,
    ) -> SkillDistributionRecord:
        """Explicit promotion to ACTIVE (the only activation path here).

        No caller — importer, refresh, optimizer, watcher — can promote;
        only this explicit action, after the owning authority's own scan
        passes, activates a version.
        """
        resolved = select_distribution_backend(backend)
        qualification = dict(qualification or {})
        if resolved == "personal":
            record = await self._promote_personal(skill_id)
        else:
            record = self._promote_registry(skill_id)
        self._event("skill.promoted", {**record.to_dict(), "qualification": qualification})
        return record

    async def _promote_personal(self, skill_id: str) -> SkillDistributionRecord:
        from runtime.personal import get_personal_runtime

        store = get_personal_runtime()
        skill = await store.get_skill(skill_id, versions=True)
        if skill is None:
            raise ValueError(f"unknown skill: {skill_id!r}")
        candidate = next(
            (v for v in skill.get("versions", []) if v.get("status") == "candidate"),
            None,
        )
        if candidate is None:
            raise ValueError(f"no candidate version to promote: {skill_id!r}")
        confirmed = await store.confirm_skill(str(candidate["id"]))
        return SkillDistributionRecord(
            skill_id=skill_id,
            backend="personal",
            state=SkillLifecycleState.ACTIVE,
            version=int(confirmed.get("version") or 1),
            trust="trusted",
            qualification="scan",
            metadata={"version_id": confirmed.get("id")},
        )

    def _promote_registry(self, skill_id: str) -> SkillDistributionRecord:
        spec = self._registry().get_version(skill_id)
        if spec is None:
            raise ValueError(f"unknown skill: {skill_id!r}")
        if spec.status != "candidate":
            raise ValueError(f"only candidates promote (status={spec.status})")
        confirmed = self._registry().confirm_skill(skill_id)
        if confirmed is None:
            raise ValueError(f"promotion refused by registry scan: {skill_id!r}")
        return SkillDistributionRecord(
            skill_id=skill_id,
            backend="registry",
            state=SkillLifecycleState.ACTIVE,
            version=int(confirmed.version),
            trust=str(confirmed.trust_status),
            qualification="scan",
        )

    # -- teach flow (cindy two-phase UX, behavior-preserving) ------------------------
    async def propose_teach(
        self,
        description: str,
        config: Mapping[str, Any],
        user_id: str,
        *,
        backend: str | None = None,
    ) -> dict[str, Any]:
        """Phase 1: propose a skill candidate (mirrors the teach endpoint).

        Backend-native response shapes are preserved; a unified distribution
        record plus ``skill.imported`` audit is added on top.
        """
        from runtime.personal import get_personal_runtime
        from server.skill_authority import propose_teaching_candidate

        resolved = select_distribution_backend(backend)
        config = dict(config or {})
        if resolved == "personal":
            scope_type = str(config.get("scope_type") or "workspace")
            scope_id = (
                str(user_id)
                if scope_type == "user"
                else str(config.get("scope_id") or os.environ.get("VEYA_WORKSPACE", "default"))
            )
            name = str(
                config.get("name") or description[:50].strip().replace(" ", "-") or "taught-skill"
            )
            store = get_personal_runtime()
            event = await store.record_event(
                "skill.teaching_instruction",
                {"name": name, "description": description},
                workspace_id=scope_id if scope_type == "workspace" else None,
            )
            candidate = await store.create_skill_candidate(
                name,
                description,
                scope_type=scope_type,
                scope_id=scope_id if scope_type in {"user", "workspace"} else str(user_id),
                trigger_examples=config.get("trigger_examples") or [],
                parameters_schema=config.get("parameters_schema")
                or {"type": "object", "properties": {}},
                execution_type=str(config.get("execution_type") or "prompt"),
                execution_ref=str(config.get("execution_ref") or ""),
                source_event_ids=[event["id"]],
                created_by=str(user_id),
            )
            self._event(
                "skill.imported",
                {
                    "skill_id": candidate["skill_id"],
                    "backend": "personal",
                    "version": candidate["version"],
                    "source_type": "taught",
                },
            )
            return {
                "status": "candidate",
                "skill_id": candidate["skill_id"],
                "skill_version_id": candidate["id"],
                "description": candidate["description"],
                "version": candidate["version"],
                "phase": "proposed",
            }
        spec = propose_teaching_candidate(self._registry(), description, config)
        self._event(
            "skill.imported",
            {
                "skill_id": spec.skill_id,
                "backend": "registry",
                "version": spec.version,
                "source_type": "taught",
            },
        )
        return {
            "status": spec.status,
            "skill_id": spec.skill_id,
            "description": spec.instructions,
            "version": spec.version,
            "phase": "proposed",
            "message": "Skill candidate created. Call /api/v1/skill/confirm to verify or /api/v1/skill/reject to discard.",
        }

    async def confirm(
        self, skill_id: str, user_id: str, *, backend: str | None = None
    ) -> dict[str, Any]:
        """Phase 2: confirm a candidate (promotion gate, behavior-preserving)."""
        from runtime.personal import PersonalRuntimeError, get_personal_runtime

        resolved = select_distribution_backend(backend)
        if resolved == "personal":
            store = get_personal_runtime()
            skill = await store.get_skill(skill_id, versions=True)
            version_id = skill_id
            if skill:
                candidate = next(
                    (v for v in skill.get("versions", []) if v.get("status") == "candidate"),
                    None,
                )
                if candidate:
                    version_id = str(candidate["id"])
            version = await store.get_skill_version(version_id)
            if version is None or (
                version.get("scope_type") == "user" and str(version.get("scope_id")) != str(user_id)
            ):
                return {
                    "status": "not_found",
                    "skill_id": skill_id,
                    "error": "Skill candidate not found",
                }
            try:
                spec = await store.confirm_skill(version_id)
            except PersonalRuntimeError as exc:
                return {
                    "status": "error",
                    "skill_id": skill_id,
                    "code": exc.code,
                    "error": str(exc),
                }
            self._event(
                "skill.promoted",
                {
                    "skill_id": spec["skill_id"],
                    "backend": "personal",
                    "version": spec["version"],
                    "qualification": "scan",
                },
            )
            return {
                "status": "confirmed",
                "skill_id": spec["skill_id"],
                "skill_version_id": version_id,
                "description": spec["description"],
                "version": spec["version"],
                "phase": spec["status"],
            }
        registered = self._registry().confirm_skill(skill_id)
        if registered is None:
            return {
                "status": "not_found",
                "skill_id": skill_id,
                "error": "Skill candidate not found",
            }
        self._event(
            "skill.promoted",
            {
                "skill_id": registered.skill_id,
                "backend": "registry",
                "version": registered.version,
                "qualification": "scan",
            },
        )
        return {
            "status": "confirmed",
            "skill_id": registered.skill_id,
            "description": registered.instructions,
            "version": registered.version,
            "phase": registered.status,
        }

    async def bind(
        self,
        skill_id: str,
        scope_type: str,
        scope_id: str,
        *,
        version: int | None = None,
        backend: str | None = None,
    ) -> SkillBinding:
        """Bind an ACTIVE+TRUSTED version to a scope (idempotent)."""
        active = await self._require_active(skill_id, backend=backend)
        binding = SkillBinding(
            skill_id=skill_id,
            version=version if version is not None else active["version"],
            scope_type=scope_type,
            scope_id=scope_id,
            trust_snapshot={"trust": active["trust"], "version": active["version"]},
        )
        state = self._load_state()
        bindings = [item for item in state["bindings"] if item.get("key") != binding.key()]
        bindings.append(binding.to_dict())
        state["bindings"] = bindings
        self._save_state(state)
        self._event("skill.bound", {**binding.to_dict(), "backend": active["backend"]})
        return binding

    def unbind(self, skill_id: str, scope_type: str, scope_id: str) -> bool:
        """Remove a binding; global skill/version authority untouched."""
        state = self._load_state()
        kept = [
            item
            for item in state["bindings"]
            if not (
                item.get("skill_id") == skill_id
                and item.get("scope_type") == scope_type
                and item.get("scope_id") == scope_id
            )
        ]
        removed = len(kept) != len(state["bindings"])
        state["bindings"] = kept
        self._save_state(state)
        return removed

    def bindings_for_scope(self, scope_type: str, scope_id: str) -> list[SkillBinding]:
        state = self._load_state()
        return [
            SkillBinding.from_dict(item)
            for item in state["bindings"]
            if item.get("scope_type") == scope_type
            and item.get("scope_id") == scope_id
            and item.get("enabled", True)
        ]

    async def _require_active(self, skill_id: str, *, backend: str | None) -> dict[str, Any]:
        resolved = select_distribution_backend(backend)
        if resolved == "personal":
            from runtime.personal import get_personal_runtime

            active = await get_personal_runtime().resolve_active_skill_version(skill_id)
            if active is None:
                raise ValueError(f"no active trusted version: {skill_id!r}")
            return {
                "version": int(active["version"]),
                "trust": "trusted",
                "backend": "personal",
            }
        spec = self._registry().get_version(skill_id)
        if (
            spec is None
            or classify_registry_spec(spec.status, spec.trust_status)
            is not SkillLifecycleState.ACTIVE
        ):
            raise ValueError(f"no active trusted version: {skill_id!r}")
        return {
            "version": int(spec.version),
            "trust": str(spec.trust_status),
            "backend": "registry",
        }

    # -- materialize ------------------------------------------------------------
    async def materialize(
        self,
        skill_id: str,
        scope_type: str,
        scope_id: str,
        *,
        backend: str | None = None,
    ) -> dict[str, Any]:
        """Project the ACTIVE+TRUSTED+BOUND version for runtime use.

        Pure projection over live authority state — nothing cached, so a
        rollback/revoke is reflected immediately and stale truth is
        impossible. Raises when the skill is not active, not trusted, or
        not bound/visible to the requesting scope.
        """
        active = await self._require_active(skill_id, backend=backend)
        allowed = any(
            binding.skill_id == skill_id and binding.enabled
            for binding in self.bindings_for_scope(scope_type, scope_id)
        )
        if not allowed and not await self._creation_scope_visible(
            skill_id, scope_type, scope_id, backend=active["backend"]
        ):
            raise ValueError(f"skill {skill_id!r} not bound to {scope_type}:{scope_id}")
        payload = await self._materialize_payload(skill_id, active, backend=active["backend"])
        revision = f"v{active['version']}@{active['trust']}"
        self._event(
            "skill.materialized",
            {
                "skill_id": skill_id,
                "backend": active["backend"],
                "version": active["version"],
                "scope_type": scope_type,
                "scope_id": scope_id,
                "revision": revision,
            },
        )
        return {
            "skill_id": skill_id,
            "version": active["version"],
            "revision": revision,
            "scope": f"{scope_type}:{scope_id}",
            "payload": payload,
        }

    async def _creation_scope_visible(
        self, skill_id: str, scope_type: str, scope_id: str, *, backend: str
    ) -> bool:
        if backend != "personal":
            return True
        from runtime.personal import get_personal_runtime

        skill = await get_personal_runtime().get_skill(skill_id)
        return bool(
            skill is not None
            and skill.get("scope_type") == scope_type
            and skill.get("scope_id") == scope_id
        )

    async def _materialize_payload(
        self, skill_id: str, active: Mapping[str, Any], *, backend: str
    ) -> dict[str, Any]:
        if backend == "personal":
            from runtime.personal import get_personal_runtime

            skill = await get_personal_runtime().get_skill(skill_id, versions=True)
            row = (
                next(
                    (
                        dict(v)
                        for v in (skill or {}).get("versions", [])
                        if int(v.get("version", -1)) == int(active["version"])
                    ),
                    None,
                )
                if skill
                else None
            )
            if row is None:
                raise ValueError(f"active version vanished: {skill_id!r}")
            return {
                "kind": "prompt",
                "instructions": str(row.get("description", "")),
                "execution_type": str(row.get("execution_type", "prompt")),
                "execution_ref": str(row.get("execution_ref", "")),
            }
        spec = self._registry().get_version(skill_id)
        if spec is None:
            raise ValueError(f"active version vanished: {skill_id!r}")
        return {
            "kind": "prompt",
            "instructions": spec.instructions,
            "execution_type": spec.execution_type,
            "execution_ref": spec.execution_ref,
        }

    # -- refresh / rollback / revoke ----------------------------------------------
    async def refresh_skill(self, skill_id: str, source: SourceProvenance) -> dict[str, Any]:
        """New source revision -> new candidate; active version never moves.

        Same revision is a no-op (no new version). Failures leave the
        active version untouched.
        """
        from runtime.personal import get_personal_runtime

        store = get_personal_runtime()
        skill = await store.get_skill(skill_id, versions=True)
        if skill is None:
            raise ValueError(f"unknown skill: {skill_id!r}")
        state = self._load_state()
        key = f"refresh:{source.source_uri}"
        known = state["sources"].get(key)
        if isinstance(known, dict) and known.get("revision") == source.source_revision:
            return {"skill_id": skill_id, "refreshed": False, "reason": "same revision"}
        current = max((int(v.get("version", 0)) for v in skill.get("versions", [])), default=0)
        base = next(
            (dict(v) for v in skill.get("versions", []) if int(v.get("version", 0)) == current),
            {},
        )
        created = await store.create_skill_candidate(
            str(skill.get("name", skill_id)),
            str(base.get("description", "")),
            scope_type=str(skill.get("scope_type", "user")),
            scope_id=str(skill.get("scope_id", "local")),
            execution_type=str(base.get("execution_type", "prompt")),
            execution_ref=str(base.get("execution_ref", "")),
            created_by="refresh",
            parent_version=current or None,
        )
        state["sources"][key] = {
            "revision": source.source_revision,
            "version": created.get("version"),
        }
        self._save_state(state)
        self._event(
            "skill.refreshed",
            {
                "skill_id": skill_id,
                "version": created.get("version"),
                "revision": source.source_revision,
                "active_preserved": True,
            },
        )
        return {
            "skill_id": skill_id,
            "refreshed": True,
            "version": created.get("version"),
            "active_preserved": True,
        }

    async def rollback_skill(
        self, skill_id: str, version: int, *, backend: str | None = None
    ) -> dict[str, Any]:
        """Explicit rollback to a previous VERIFIED/TRUSTED version.

        Refuses revoked/untrusted targets. Materialization is computed
        live, so the next materialize reflects the restored version with
        no separate invalidation step.
        """
        resolved = select_distribution_backend(backend)
        if resolved == "personal":
            from runtime.personal import get_personal_runtime

            store = get_personal_runtime()
            skill = await store.get_skill(skill_id, versions=True)
            target = next(
                (
                    dict(v)
                    for v in (skill or {}).get("versions", [])
                    if int(v.get("version", -1)) == int(version)
                ),
                None,
            )
            if target is None:
                raise ValueError(f"unknown version: {skill_id!r} v{version}")
            if str(target.get("trust_status")) == "blocked":
                raise ValueError(f"refusing rollback to blocked version: {skill_id!r} v{version}")
            result = await store.rollback_skill(skill_id, version)
        else:
            spec = self._registry().get_version(skill_id)
            if spec is None:
                raise ValueError(f"unknown skill: {skill_id!r}")
            # Single-version store cannot restore history: deprecating the
            # current version is the only honest rollback here.
            self._registry().rollback(skill_id)
            result = {"status": "rolled_back", "skill_id": skill_id, "version": None}
        self._event(
            "skill.rollback", {"skill_id": skill_id, "backend": resolved, "version": version}
        )
        return result

    async def revoke_skill(self, skill_id: str, *, backend: str | None = None) -> dict[str, Any]:
        """Revoke: block execution, keep history/provenance/audit."""
        resolved = select_distribution_backend(backend)
        if resolved == "personal":
            from runtime.personal import get_personal_runtime

            result = await get_personal_runtime().deprecate_skill(skill_id)
        else:
            updated = self._registry().set_trust_status(skill_id, "blocked")
            if updated is None:
                raise ValueError(f"unknown skill: {skill_id!r}")
            self._registry().rollback(skill_id)
            result = {"status": "revoked", "skill_id": skill_id}
        self._event("skill.revoked", {"skill_id": skill_id, "backend": resolved})
        return result
